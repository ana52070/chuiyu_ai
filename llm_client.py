import os
import requests
from typing import List, Any, Dict, Optional
from dotenv import load_dotenv  # 新增：导入dotenv库

load_dotenv()  # 自动查找并加载当前目录下的.env文件

def _env(key, default=None):
    """获取环境变量，若不存在且无默认值则抛出异常"""
    value = os.environ.get(key, default)
    if value is None:
        raise RuntimeError(f"Missing env var: {key}")
    return value

# 读取环境变量
BASE_URL = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_KEY = _env("OPENAI_API_KEY")
CHAT_MODEL = _env("CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL = _env("EMBED_MODEL", "text-embedding-3-small")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

def chat_completions(messages: List[Dict[str, str]], temperature: float = 0.8, max_tokens: int = 300) -> str:
    url = f"{BASE_URL}/chat/completions"
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

def embeddings(texts: List[str]) -> List[List[float]]:
    url = f"{BASE_URL}/embeddings"
    payload = {"model": EMBED_MODEL, "input": texts}
    r = requests.post(url, headers=HEADERS, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    # keep input order
    out = [None] * len(data["data"])
    for item in data["data"]:
        out[item["index"]] = item["embedding"]
    return out