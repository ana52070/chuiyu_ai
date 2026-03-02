import json
import random
from typing import List, Dict, Any
import numpy as np
import hnswlib

from data_io import read_jsonl
from llm_client import embeddings, chat_completions
from scorer import score_candidate

TURN_PAIRS = "turn_pairs.jsonl"


# INDEX_PATH = "style_index/hnsw.index"
# META_PATH = "style_index/meta.npy"

INDEX_PATH = "reply_index/hnsw.index"
META_PATH = "reply_index/meta.npy"

BAD_TOKENS = {"[动画表情]", "[表情]", "[图片]", "[语音]", "[视频]","[文件]"}

def filter_bubbles(msgs):
    out = []
    for m in msgs:
        m = (m or "").strip()
        if not m:
            continue
        if m in BAD_TOKENS:
            continue
        # 把“只包含占位符”的句子去掉
        if m.startswith("[") and m.endswith("]") and len(m) <= 10:
            continue
        out.append(m)
    return out


def load_index():
    metas = np.load(META_PATH, allow_pickle=True).tolist()
    # infer dim by loading one embedding from saved index? easiest: store dim in metas is not present
    # hnswlib needs dim at init; we can read it from first meta by creating a temporary guess:
    # We'll load dim by reading index with known dim using a trick: store dim inside index on build time isn't exposed.
    # So: just compute one embedding now to get dim.
    dim = len(embeddings(["test"])[0])
    index = hnswlib.Index(space="cosine", dim=dim)
    index.load_index(INDEX_PATH)
    index.set_ef(64)
    return index, metas

def parse_incoming(row: Dict[str, Any]) -> str:
    inc = row.get("incoming") or row.get("input") or row.get("other") or []
    # 兼容：incoming 可能是字符串、也可能是列表
    if isinstance(inc, str):
        return inc.strip()
    parts = []
    for m in inc:
        if isinstance(m, str):
            parts.append(m)
        elif isinstance(m, dict):
            t = m.get("text") or m.get("content") or ""
            if t:
                parts.append(t)
    return "\n".join([p.strip() for p in parts if p and str(p).strip()])

def retrieve_exemplars(index, metas, query_text: str, k: int = 8) -> List[str]:
    qv = np.array(embeddings([query_text])[0], dtype=np.float32)
    labels, dists = index.knn_query(qv, k=k)
    ids = labels[0].tolist()
    out = []
    for i in ids:
        out.append(metas[i]["text"])
    return out

def extract_messages_json(s: str) -> List[str]:
    # 模型必须输出 {"messages":[...]}，但防御一下
    s = s.strip()
    try:
        obj = json.loads(s)
        msgs = obj.get("messages", [])
        if isinstance(msgs, list):
            return filter_bubbles([str(x) for x in msgs])
    except Exception:
        pass
    # fallback: 按行切
    lines = [x.strip() for x in s.splitlines() if x.strip()]
    return filter_bubbles(lines[:4])

def build_prompt(incoming: str, exemplars: List[str]) -> List[Dict[str, str]]:
    # 关键：强制“微信气泡数组”，禁止报告体
    sys = (
        "你在微信上扮演用户本人聊天。输出必须是严格JSON："
        "{\"messages\":[\"...\",\"...\",...]}\n"
        "规则：\n"
        "1) 用口语、别太正式，别讲大道理，别写长文。\n"
        "2) 默认1-3条消息气泡，每条尽量短（5-20字常见）。\n"
        "3) 不要使用：首先/其次/总的来说/建议/综上/步骤/方案/总结。\n"
        "4) 不确定就直接说不知道/不太确定，不要装懂。\n"
        "5) 不要输出除JSON以外的任何内容。"
        "6) 绝对不要输出任何占位符：如[动画表情]/[图片]/[语音] 等。"
    )
    # few-shot exemplars：给模型“你平时怎么说”
    # 用“示例”而不是“要求”，更像你
    ex = "\n".join([f"- {t}" for t in exemplars[:8]])
    user = (
        f"对方发来：\n{incoming}\n\n"
        f"你平时的说话示例（只看语气，不要照抄内容）：\n{ex}\n\n"
        "现在请按你的微信风格回复（JSON messages 数组）。"
    )
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]

def generate_candidates(prompt_msgs, n: int = 8) -> List[List[str]]:
    # 生成多候选：用不同temperature
    temps = [0.4, 0.6, 0.8, 0.9, 1.0]
    outs = []
    for i in range(n):
        t = temps[i % len(temps)]
        raw = chat_completions(prompt_msgs, temperature=t, max_tokens=220)
        outs.append(extract_messages_json(raw))
    return outs

def main(sample_n: int = 30, k_ex: int = 8, n_cand: int = 10, seed: int = 7):
    random.seed(seed)

    index, metas = load_index()
    pairs = list(read_jsonl(TURN_PAIRS))
    random.shuffle(pairs)
    pairs = pairs[:sample_n]

    for idx, row in enumerate(pairs, 1):
        incoming = parse_incoming(row)
        if not incoming:
            continue

        exemplars = retrieve_exemplars(index, metas, incoming, k=k_ex)
        prompt = build_prompt(incoming, exemplars)
        cands = generate_candidates(prompt, n=n_cand)

        best = None
        best_s = -1e9
        best_detail = None
        for msgs in cands:
            s, detail = score_candidate(msgs, exemplars)
            if s > best_s:
                best_s, best, best_detail = s, msgs, detail

        # 真实你当时怎么回（如果 turn_pairs 里有 reply 字段）
        true_reply = row.get("reply") or []
        if isinstance(true_reply, list):
            true_text = " | ".join([(m.get("text") if isinstance(m, dict) else str(m)) for m in true_reply][:6])
        else:
            true_text = str(true_reply)[:200]

        print("=" * 80)
        print(f"[{idx}] 对方：\n{incoming}")
        print("\n检索到的你的示例(前5)：")
        for t in exemplars[:5]:
            print("  -", t)
        print("\n✅ 赛博你（准备发的气泡）：")
        for m in best:
            print("  >", m)
        print("\nscore detail:", best_detail)
        if true_text.strip():
            print("\n📌 真实你当时回：", true_text)

if __name__ == "__main__":
    main()