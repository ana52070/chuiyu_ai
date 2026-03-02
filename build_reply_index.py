import os, re
import numpy as np
import hnswlib
from tqdm import tqdm
from data_io import read_jsonl
from llm_client import embeddings

TURN_PAIRS = "turn_pairs.jsonl"
INDEX_DIR = "reply_index"
INDEX_PATH = os.path.join(INDEX_DIR, "hnsw.index")
META_PATH = os.path.join(INDEX_DIR, "meta.npy")



def clean(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""
    # 去掉占位符/方括号表情
    t = re.sub(r"\[[^\]]{1,12}\]", "", t).strip()
    # 过滤纯链接
    if t.startswith("http") and len(t) < 80:
        return ""
    # 截断
    if len(t) > 350:
        t = t[:350]
    # 过滤只剩标点
    if re.fullmatch(r"[\s\W_]+", t):
        return ""
    return t

def get_text_from_msg(m):
    """兼容 turn_pairs 里 reply 的多种结构"""
    if m is None:
        return ""
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        return (
            m.get("text") or
            m.get("content") or
            m.get("msg") or
            m.get("message") or
            ""
        )
    return str(m)


def extract_reply_bubbles(row):
    # 你的 turn_pairs 用的是 response 字段
    if "response" in row and row["response"] is not None:
        val = row["response"]
        if isinstance(val, list):
            return [get_text_from_msg(x) for x in val]
        if isinstance(val, dict):
            if "messages" in val and isinstance(val["messages"], list):
                return [get_text_from_msg(x) for x in val["messages"]]
            return [get_text_from_msg(val)]
        if isinstance(val, str):
            return [val]
        return [str(val)]

    # 兜底：兼容其它字段名
    cand_fields = ["reply", "replies", "answer", "output", "messages", "me", "my_reply"]
    for f in cand_fields:
        if f in row and row[f] is not None:
            val = row[f]
            if isinstance(val, list):
                return [get_text_from_msg(x) for x in val]
            if isinstance(val, dict):
                if "messages" in val and isinstance(val["messages"], list):
                    return [get_text_from_msg(x) for x in val["messages"]]
                return [get_text_from_msg(val)]
            if isinstance(val, str):
                return [val]
            return [str(val)]
    return []

def main():
    os.makedirs(INDEX_DIR, exist_ok=True)
    texts, metas = [], []

    # ——诊断：先看看前几条长啥样——
    preview = []
    for i, row in enumerate(read_jsonl(TURN_PAIRS)):
        preview.append(row)
        if i >= 2:
            break
    if preview:
        print("🔎 turn_pairs sample keys:", [list(preview[0].keys())])
        # 不打印太长，只提示字段
    else:
        raise RuntimeError("turn_pairs.jsonl is empty?")

    # ——正式抽取——
    total = 0
    kept = 0
    for row in read_jsonl(TURN_PAIRS):
        total += 1
        bubbles = extract_reply_bubbles(row)
        bubbles = [clean(x) for x in bubbles]
        bubbles = [x for x in bubbles if x]
        if not bubbles:
            continue

        exemplar = "\n".join(bubbles[:4])  # 一轮最多4条
        if len(exemplar) < 2:
            continue

        texts.append(exemplar)
        metas.append({"text": exemplar})
        kept += 1

    print(f"✅ extracted reply exemplars: kept={kept} / total={total}")

    if kept == 0:
        raise RuntimeError(
            "No reply exemplars extracted. "
            "Likely field names mismatch or everything got filtered. "
            "Open turn_pairs.jsonl and check what the reply field is called."
        )

    # ——做embedding——
    batch = 8
    vecs = []
    for i in tqdm(range(0, len(texts), batch), desc="Embedding replies"):
        vecs.extend(embeddings(texts[i:i+batch]))

    X = np.array(vecs, dtype=np.float32)
    if X.ndim != 2 or X.shape[0] == 0:
        raise RuntimeError(f"Bad embedding matrix shape: {X.shape}")

    dim = X.shape[1]

    index = hnswlib.Index(space="cosine", dim=dim)
    index.init_index(max_elements=len(texts), ef_construction=200, M=16)
    index.add_items(X, np.arange(len(texts)))
    index.set_ef(64)
    index.save_index(INDEX_PATH)

    np.save(META_PATH, np.array(metas, dtype=object), allow_pickle=True)

    print("✅ built reply index:", INDEX_PATH)
    print("count=", len(texts), "dim=", dim)

if __name__ == "__main__":
    main()