import os
import math
import numpy as np
import hnswlib
from tqdm import tqdm
import re
from data_io import read_jsonl
from llm_client import embeddings

# 你已有的文件
USER_BUBBLES = "user_bubbles.jsonl"

# 输出
INDEX_DIR = "style_index"
INDEX_PATH = os.path.join(INDEX_DIR, "hnsw.index")
META_PATH = os.path.join(INDEX_DIR, "meta.npy")



EMOJI_PLACEHOLDERS = [
    "[动画表情]", "[表情]", "[图片]", "[语音]", "[视频]", "[文件]",
    "[位置]", "[名片]", "[链接]", "[红包]", "[转账]"
]

def clean_text(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return ""

    # 1) 过滤各种占位符（核心）
    if t in EMOJI_PLACEHOLDERS:
        return ""
    # 有些导出是类似“[动画表情]xxxx”也直接干掉
    if t.startswith("[") and t.endswith("]") and len(t) <= 10:
        return ""

    # 2) 过滤纯链接
    if t.startswith("http") and len(t) < 60:
        return ""

    # 3) 过滤超长文（你是做口吻，不需要）
    if len(t) > 5000:
        return ""

    # 4) 截断（embedding 防 413）
    if len(t) > 300:
        t = t[:300]

    # 5) 过滤只剩标点/空白的
    if re.fullmatch(r"[\s\W_]+", t):
        return ""

    return t

def main():
    os.makedirs(INDEX_DIR, exist_ok=True)

    texts = []
    metas = []  # store minimal metadata
    for row in read_jsonl(USER_BUBBLES):
        # 兼容你导出的格式：通常有 text/content/type 等字段
        t = row.get("text") or row.get("content") or ""
        t = clean_text(t)
        if not t:
            continue
        # 只索引文本消息（如果你想把表情也纳入，可以扩展）
        typ = row.get("type", "")
        if typ and "文本" not in typ and "text" not in typ.lower():
            # 仍然允许短的“引用消息”文本等
            pass
        texts.append(t)
        metas.append({
            "t": row.get("createTime") or row.get("t"),
            "text": t,
            "type": typ,
        })

    if not texts:
        raise RuntimeError("No usable texts found in user_bubbles.jsonl")

    # 分批做embedding
    batch = 8
    vecs = []
    for i in tqdm(range(0, len(texts), batch), desc="Embedding user bubbles"):
        chunk = texts[i:i+batch]
        emb = embeddings(chunk)
        vecs.extend(emb)

    X = np.array(vecs, dtype=np.float32)
    dim = X.shape[1]

    # 建 HNSW 索引
    index = hnswlib.Index(space="cosine", dim=dim)
    index.init_index(max_elements=len(texts), ef_construction=200, M=16)
    ids = np.arange(len(texts))
    index.add_items(X, ids)
    index.set_ef(64)

    index.save_index(INDEX_PATH)
    np.save(META_PATH, np.array(metas, dtype=object), allow_pickle=True)

    print(f"✅ built style index: {INDEX_PATH}")
    print(f"✅ meta saved: {META_PATH}")
    print(f"count={len(texts)}, dim={dim}")

if __name__ == "__main__":
    main()