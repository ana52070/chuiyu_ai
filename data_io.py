import json
from typing import Iterator, Dict, Any, Optional

try:
    import orjson
    def loads(s: str):
        return orjson.loads(s)
    def dumps(obj) -> str:
        return orjson.dumps(obj).decode("utf-8")
except Exception:
    def loads(s: str):
        return json.loads(s)
    def dumps(obj) -> str:
        return json.dumps(obj, ensure_ascii=False)

def read_jsonl(path: str) -> Iterator[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield loads(line)

def write_jsonl(path: str, rows: Iterator[Dict[str, Any]]):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(dumps(r) + "\n")