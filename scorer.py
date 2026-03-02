import re
from typing import List, Tuple

ANTI_AI_PATTERNS = [
    r"首先", r"其次", r"总的来说", r"综上", r"因此", r"与此同时",
    r"建议", r"可以从以下", r"你可以尝试", r"需要注意", r"从.*角度",
    r"分(析|点)", r"总结", r"步骤", r"方案",
]

CASUAL_BONUS = ["哈哈", "hh", "嗯", "行", "卧槽", "我去", "笑死", "懂了", "行吧", "啊这", "确实", "有点"]

BAD_PLACEHOLDER = ["[动画表情]", "[表情]", "[图片]", "[语音]", "[视频]", "[文件]"]

def score_candidate(messages: List[str], retrieved_texts: List[str]) -> Tuple[float, dict]:



    # basic
    msgs = [m.strip() for m in messages if (m or "").strip()]
    total_len = sum(len(m) for m in msgs)
    max_len = max([len(m) for m in msgs], default=0)
    n = len(msgs)

    s = 0.0
    detail = {}



    # 1) length constraints
    if total_len <= 80:
        s += 25
    elif total_len <= 120:
        s += 10
    elif total_len <= 180:
        s -= 10
    else:
        s -= 100

    if max_len > 60:
        s -= 40
    if max_len > 100:
        s -= 80

    # 2) bubble count
    if 1 <= n <= 3:
        s += 20
    elif n == 4:
        s += 5
    elif n == 0:
        s -= 200
    else:
        s -= 20

    # repeat penalty
    uniq = len(set(msgs))
    if n >= 2 and uniq == 1:
        s -= 40  # 全部重复扣爆
    elif n >= 3 and uniq <= 2:
        s -= 15
    detail["uniq_bubbles"] = uniq

    # 3) anti-ai penalties
    text_all = "\n".join(msgs)
    anti_hits = 0
    for p in ANTI_AI_PATTERNS:
        if re.search(p, text_all):
            anti_hits += 1
    s -= anti_hits * 25
    detail["anti_hits"] = anti_hits

    bad_hits = 0
    for b in BAD_PLACEHOLDER:
        if b in text_all:
            bad_hits += 1
    s -= bad_hits * 200
    detail["bad_placeholder_hits"] = bad_hits

    # 4) casual bonus
    bonus_hits = 0
    for w in CASUAL_BONUS:
        if w in text_all:
            bonus_hits += 1
    s += min(20, bonus_hits * 5)
    detail["casual_hits"] = bonus_hits

    # 5) exemplar overlap bonus (cheap “像你” proxy)
    # take some common substrings from retrieved exemplars
    exemplar_join = "\n".join(retrieved_texts[:10])
    overlap = 0
    for m in msgs:
        if len(m) >= 2 and m in exemplar_join:
            overlap += 1
    s += min(15, overlap * 5)
    detail["overlap_hits"] = overlap



    detail.update({"total_len": total_len, "max_len": max_len, "bubble_count": n, "score": s})
    return s, detail