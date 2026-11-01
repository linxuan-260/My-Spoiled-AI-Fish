"""注入块 marker 规范。

唯一格式：
    [[FISHMEM:v<ver>:<layer>:begin]]
    ...正文...
    [[FISHMEM:v<ver>:<layer>:end]]

设计要点：
1. 每次注入前先 strip_all()，把历史注入块（包括半截的、上一版 marker 的）全剥掉，
   所以「重复注入」在物理上不可能堆叠。
2. strip_all() 只认 FISHMEM 前缀，绝不碰别人写的提示词、更不碰人格正文。
3. 不变量：strip_all(注入后) == strip_all(注入前)，逐字节相等。
"""

from __future__ import annotations

import re

LAYERS = ("memory", "mood", "life", "style")

# v 允许 1~8 个 词字符或点，方便以后写 v1.1 / v2
_B = r"\[\[FISHMEM:v(?P<{0}>[\w.]+):(?P<{1}>\w+):{2}\]\]"

_BEGIN = "[[FISHMEM:v{v}:{layer}:begin]]"
_END = "[[FISHMEM:v{v}:{layer}:end]]"

_PAIR = re.compile(
    r"\[\[FISHMEM:v[\w.]+:(?P<layer>\w+):begin\]\]"
    r".*?"
    r"\[\[FISHMEM:v[\w.]+:(?P=layer):end\]\]",
    re.S,
)
# 落单的 begin / end（比如上次被别的插件截断了）
_ORPHAN = re.compile(r"\[\[/?FISHMEM:v[\w.]+:\w+:(?:begin|end)\]\]")
# 连续空行收敛
_BLANK = re.compile(r"\n{3,}")
# 尾部空白
_TRAIL = re.compile(r"[ \t]+\n")


def begin(v: str, layer: str) -> str:
    return _BEGIN.format(v=v, layer=layer)


def end(v: str, layer: str) -> str:
    return _END.format(v=v, layer=layer)


def has_block(prompt: str) -> bool:
    return bool(_PAIR.search(prompt or "") or _ORPHAN.search(prompt or ""))


def strip_all(prompt: str) -> str:
    """剥掉所有 FISHMEM 注入块。幂等，且绝不改动块外的一个字节。"""
    if not prompt:
        return prompt or ""

    prev = None
    cur = prompt
    while prev != cur:
        prev = cur
        cur = _PAIR.sub("", cur)
    cur = _ORPHAN.sub("", cur)

    cur = _TRAIL.sub("\n", cur)
    cur = _BLANK.sub("\n\n", cur)
    return cur.rstrip()


def append_block(prompt: str, layer: str, body: str, v: str = "1") -> str:
    """先剥旧块，再追加一个新块。"""
    body = (body or "").strip()
    if not body:
        return strip_all(prompt)
    base = strip_all(prompt)
    return f"{base}\n{begin(v, layer)}\n{body}\n{end(v, layer)}" if base else (
        f"{begin(v, layer)}\n{body}\n{end(v, layer)}"
    )


def render(prompt: str, blocks, v: str = "1") -> str:
    """blocks: Iterable[(layer, body)]，按给定顺序一次性追加。

    不能循环调用 append_block：它每次都先 strip_all(prompt)，会把上一轮
    刚追加的块也剥掉，结果只剩最后一块。2026-10-31 修复。
    """
    base = strip_all(prompt)
    parts = []
    for layer, body in blocks:
        body = (body or "").strip()
        if not body:
            continue
        parts.append(f"{begin(v, layer)}\n{body}\n{end(v, layer)}")
    if not parts:
        return base
    return "\n".join([base] + parts) if base else "\n".join(parts)


def extract(prompt: str, layer: str) -> str | None:
    """取出某一层的正文（调试用）。"""
    pat = re.compile(
        r"\[\[FISHMEM:v[\w.]+:" + re.escape(layer) + r":begin\]\]\s*(.*?)\s*"
        r"\[\[FISHMEM:v[\w.]+:" + re.escape(layer) + r":end\]\]",
        re.S,
    )
    m = pat.search(prompt or "")
    return m.group(1) if m else None
