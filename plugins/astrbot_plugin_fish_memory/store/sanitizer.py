"""发送前文本清洗。纯函数，不碰数据库，方便单测。

四条规则（各自独立开关）：
    sanitize_cs_tone      删"程序员腔"——您/是什么/为什么/非常
    sanitize_drop_period  短句末尾的句号去掉（聊天里没人打句号）
    sanitize_merge_paras  三行以上压成两段
    sanitize_emoji_rhythm 同一行连发 3 个以上相同表情压到 1 个
    max_reply_chars > 0   硬截断（默认 0 = 关闭）
"""

from __future__ import annotations

import re

# 程序员腔 -> 口语
DEFAULT_CS_TONE = {
    "您": "你",
    "是什么呢": "是啥",
    "是什么": "是啥",
    "为什么呢": "为啥",
    "为什么": "为啥",
    "怎么样": "咋样",
    "怎么": "咋",
    "非常": "特别",
    "此外": "还有",
    "因此": "所以",
    "需要注意的是": "对了",
    "建议您": "你不如",
    "总而言之": "反正",
}

_EMOJI_RUN = re.compile(
    r"([\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF])\1{2,}")
_MULTI_BLANK = re.compile(r"\n{3,}")
_TAIL_SPACE = re.compile(r"[ \t]+$", re.M)
_SENT_SPLIT = re.compile(r"(?<=[。！？!?])\s*")
_SHORT_PUNCT = re.compile(r"^(.{1,24}?)[。.]$")


def apply_cs_tone(text: str, mapping: dict | None = None) -> str:
    m = mapping or DEFAULT_CS_TONE
    out = text or ""
    for k in sorted(m, key=len, reverse=True):
        if k and k in out:
            out = out.replace(k, m[k])
    return out


def drop_periods(text: str) -> str:
    """只去掉「短句 + 句号结尾」的那个句号，句中的保留。"""
    lines = []
    for line in (text or "").split("\n"):
        s = line.rstrip()
        if not s:
            lines.append(line)
            continue
        has_other = any(c in "！？!?…~～" for c in s)
        if not has_other:
            s2 = _SHORT_PUNCT.sub(r"\1", s)
            s = s2
        lines.append(s)
    return "\n".join(lines)


def merge_paras(text: str, keep: int = 2) -> str:
    paras = [p for p in re.split(r"\n{2,}", (text or "").strip()) if p.strip()]
    if len(paras) <= keep:
        return "\n\n".join(paras)
    head = "\n\n".join(paras[: keep - 1])
    tail = " ".join(p.strip() for p in paras[keep - 1:])
    return (head + "\n\n" + tail).strip()


def emoji_rhythm(text: str) -> str:
    return _EMOJI_RUN.sub(r"\1", text or "")


def hard_clamp(text: str, max_chars: int) -> str:
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text


def sanitize(text: str, cfg: dict | None = None, *, max_chars: int = 0,
             cs_tone_map: dict | None = None) -> tuple[str, list[str]]:
    """返回 (新文本, 触发过的规则名列表)。规则按顺序叠加。"""
    if not text:
        return text or "", []
    cfg = cfg or {}
    out = text
    hits: list[str] = []

    def on(key: str, default: bool = True) -> bool:
        v = cfg.get(key, default)
        return bool(v) if v is not None else default

    if on("sanitize_cs_tone"):
        n = apply_cs_tone(out, cs_tone_map)
        if n != out:
            out = n
            hits.append("cs_tone")
    if on("sanitize_emoji_rhythm"):
        n = emoji_rhythm(out)
        if n != out:
            out = n
            hits.append("emoji_rhythm")
    if on("sanitize_drop_period"):
        n = drop_periods(out)
        if n != out:
            out = n
            hits.append("drop_period")
    if on("sanitize_merge_paras"):
        n = merge_paras(out)
        if n != out:
            out = n
            hits.append("merge_paras")

    n = _MULTI_BLANK.sub("\n\n", out).strip()
    n = "\n".join(_TAIL_SPACE.sub("", ln) for ln in n.split("\n")).strip()
    if n != out:
        out = n
        hits.append("trim")

    if max_chars and max_chars > 0:
        n = hard_clamp(out, max_chars)
        if n != out:
            out = n
            hits.append("clamp")

    return out or text, hits
