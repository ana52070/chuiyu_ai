#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Convert a WeChat JSON export (session + messages[]) into:
1) user_bubbles.jsonl  - every "you" message as a single bubble record
2) turn_pairs.jsonl    - (incoming block -> your response burst) training / retrieval unit
3) style_stats.json    - aggregated style statistics (no raw content beyond top short texts)

Usage:
  python convert_wechat_export.py --input "xxx.json" --outdir "./out" --gap 60 --max_context 12
Notes:
- This script keeps raw text in turn_pairs.jsonl by default (because RAG needs context).
  If you want privacy-minimized files, add --redact_other (replaces other side text with "[REDACTED]").
"""

import argparse, json, os, re
from collections import Counter
from typing import Any, Dict, List, Optional

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def normalize_msg(m: Dict[str, Any], redact: bool=False) -> Dict[str, Any]:
    content = m.get("content")
    if redact and m.get("isSend") == 0 and isinstance(content, str):
        content = "[REDACTED]"
    return {
        "localId": m.get("localId"),
        "createTime": m.get("createTime"),
        "formattedTime": m.get("formattedTime"),
        "type": m.get("type"),
        "localType": m.get("localType"),
        "content": content,
        "isSend": m.get("isSend"),
        "senderDisplayName": m.get("senderDisplayName"),
        "senderUsername": m.get("senderUsername"),
        "emojiMd5": m.get("emojiMd5"),
    }

def build_turn_pairs(messages: List[Dict[str, Any]],
                     gap_threshold: int=60,
                     max_context_msgs: int=12,
                     user_isSend: int=1,
                     redact_other: bool=False) -> List[Dict[str, Any]]:
    msgs_sorted = sorted(
        [m for m in messages if m.get("type") != "系统消息" and m.get("isSend") in (0,1)],
        key=lambda x: (x.get("createTime", 0), x.get("localId", 0))
    )
    pairs: List[Dict[str, Any]] = []
    i, n = 0, len(msgs_sorted)

    while i < n:
        if msgs_sorted[i].get("isSend") == user_isSend:
            i += 1
            continue

        # incoming block (other side)
        incoming: List[Dict[str, Any]] = []
        while i < n and msgs_sorted[i].get("isSend") != user_isSend:
            incoming.append(msgs_sorted[i])
            i += 1

        if not incoming:
            continue

        # response burst (you)
        response: List[Dict[str, Any]] = []
        last_t: Optional[int] = None
        while i < n and msgs_sorted[i].get("isSend") == user_isSend:
            t = msgs_sorted[i].get("createTime", 0)
            if last_t is None or (t - last_t) <= gap_threshold:
                response.append(msgs_sorted[i])
                last_t = t
                i += 1
            else:
                break

        if not response:
            continue

        resp_start_time = response[0].get("createTime", 0)
        prev = [m for m in msgs_sorted if m.get("createTime", 0) < resp_start_time]
        context = prev[-max_context_msgs:] if max_context_msgs else []

        pairs.append({
            "t": resp_start_time,
            "incoming": [normalize_msg(x, redact=redact_other) for x in incoming],
            "response": [normalize_msg(x, redact=False) for x in response],
            "context": [normalize_msg(x, redact=redact_other) for x in context],
            "meta": {
                "incoming_count": len(incoming),
                "response_count": len(response),
            }
        })

    return pairs

def compute_style_stats(data: Dict[str, Any]) -> Dict[str, Any]:
    messages = data.get("messages", [])
    user = [m for m in messages if m.get("isSend") == 1]
    other = [m for m in messages if m.get("isSend") == 0]
    system = [m for m in messages if m.get("type") == "系统消息"]

    user_type = Counter(m.get("type") for m in user)
    other_type = Counter(m.get("type") for m in other)
    user_text = [m.get("content", "").strip() for m in user if m.get("type") == "文本消息" and m.get("content")]

    lens = [len(t) for t in user_text]
    lens_sorted = sorted(lens)
    def percentile(p: float) -> Optional[float]:
        if not lens_sorted:
            return None
        idx = int(round((p/100.0) * (len(lens_sorted)-1)))
        return float(lens_sorted[idx])

    # formatting heuristics
    with_newline = sum(1 for t in user_text if "\n" in t)
    with_bullet = sum(1 for t in user_text if re.search(r"(^|\n)\s*[-*]\s", t))
    with_numbered = sum(1 for t in user_text if re.search(r"(^|\n)\s*\d+[.、]\s", t))
    long_ge_200 = sum(1 for t in user_text if len(t) >= 200)

    short = [t for t in user_text if len(t) <= 6]
    top_short = Counter(short).most_common(30)

    return {
        "session": data.get("session", {}),
        "counts": {
            "total_messages": len(messages),
            "user_messages": len(user),
            "other_messages": len(other),
            "system_messages": len(system),
        },
        "user_type_counts": dict(user_type),
        "other_type_counts": dict(other_type),
        "user_text_len": {
            "count": len(lens),
            "mean": (sum(lens)/len(lens)) if lens else None,
            "p10": percentile(10),
            "p25": percentile(25),
            "p50": percentile(50),
            "p75": percentile(75),
            "p90": percentile(90),
            "p95": percentile(95),
            "p99": percentile(99),
        },
        "user_text_formatting": {
            "with_newline": int(with_newline),
            "with_bullet": int(with_bullet),
            "with_numbered": int(with_numbered),
            "long_ge_200": int(long_ge_200),
        },
        "user_top_short_text": top_short,
    }

def write_jsonl(path: str, records: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="WeChat export json path")
    ap.add_argument("--outdir", required=True, help="output directory")
    ap.add_argument("--gap", type=int, default=60, help="seconds threshold for grouping your burst")
    ap.add_argument("--max_context", type=int, default=12, help="how many previous messages to keep as context")
    ap.add_argument("--redact_other", action="store_true", help="replace other side content with [REDACTED]")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    data = load_json(args.input)
    messages = data.get("messages", [])

    pairs = build_turn_pairs(messages, gap_threshold=args.gap, max_context_msgs=args.max_context, redact_other=args.redact_other)
    write_jsonl(os.path.join(args.outdir, "turn_pairs.jsonl"), pairs)

    # user bubbles
    user_bubbles = [normalize_msg(m, redact=False) for m in messages if m.get("isSend")==1 and m.get("type")!="系统消息"]
    write_jsonl(os.path.join(args.outdir, "user_bubbles.jsonl"), user_bubbles)

    stats = compute_style_stats(data)
    with open(os.path.join(args.outdir, "style_stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print("Done.")
    print("turn_pairs:", len(pairs))
    print("user_bubbles:", len(user_bubbles))

if __name__ == "__main__":
    main()
