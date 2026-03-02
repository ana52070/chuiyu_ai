# save as export_reply_vectors_json.py and run: python export_reply_vectors_json.py
import json, numpy as np
from data_io import read_jsonl
from llm_client import embeddings

TURN_PAIRS = "turn_pairs.jsonl"
OUT = "reply_index/vectors.json"

def clean(s: str) -> str:
    import re
    s = (s or "").strip()
    s = re.sub(r"\[[^\]]{1,12}\]", "", s).strip()
    return s

texts = []
for row in read_jsonl(TURN_PAIRS):
    resp = row.get("response") or []
    parts = []
    if isinstance(resp, list):
        for x in resp:
            if isinstance(x, dict):
                parts.append(x.get("text") or x.get("content") or "")
            else:
                parts.append(str(x))
    else:
        parts.append(str(resp))
    parts = [clean(x) for x in parts if clean(x)]
    if not parts:
        continue
    exemplar = "\n".join(parts[:4])
    if len(exemplar) < 2:
        continue
    texts.append(exemplar)

# 去重
texts = list(dict.fromkeys(texts))

vecs = []
batch = 8
for i in range(0, len(texts), batch):
    vecs.extend(embeddings(texts[i:i+batch]))

rows = [{"text": t, "embedding": v} for t, v in zip(texts, vecs)]
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False)
print("ok", len(rows), "->", OUT)