"""注入预算控制与单轮上下文对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    """一个待注入的块。chars 就是它占的字数，超预算时整块丢弃，绝不截半句。"""

    layer: str
    body: str

    @property
    def chars(self) -> int:
        return len(self.body)


@dataclass
class TurnCtx:
    """一轮对话的上下文。layers 只读它，不碰 event。"""

    scope_id: str = "*"
    umo: str = ""
    user_text: str = ""
    sender_id: str = ""
    sender_name: str = ""
    msg_id: str = ""
    is_group: bool = False
    group_id: str = ""
    self_id: str = ""
    persona_id: str = ""
    event: Any = None  # AstrMessageEvent，仅调试用


def fit(blocks, budget_chars: int) -> list[Block]:
    """按顺序装块：装不下的整块丢掉（从尾部开始丢）。"""
    out: list[Block] = []
    used = 0
    for b in blocks:
        if b is None or not b.body:
            continue
        n = b.chars
        if budget_chars > 0 and used + n > budget_chars:
            continue  # 跳过这一块，后面的（更小的）还有机会
        out.append(b)
        used += n
    return out


def total_chars(blocks) -> int:
    return sum(b.chars for b in blocks if b)
