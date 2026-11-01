"""L2 情绪层：产出一段很短的心情描述。"""

from __future__ import annotations

import logging

from ..budget import Block
from ..config import on
from ..store import mooddb

log = logging.getLogger("fishmem.l2")

# 免责句是硬性的：情绪只影响语气，不能变成拿捏对方的筹码
FOOTER = "情绪只影响语气，不影响判断；不能拿情绪拿捏对方。"


def build(ctx, cfg) -> tuple[Block | None, list[int]]:
    try:
        if not on(cfg, "L2_emotion", "enabled", default=False):
            return None, []
        if not on(cfg, "L2_emotion", "sub", "classify", default=True):
            return None, []

        scope = ctx.scope_id or "*"
        cur = mooddb.tick(
            scope,
            float(on(cfg, "L2_emotion", "mood_halflife_hour", default=6) or 6),
            float(on(cfg, "L2_emotion", "label_halflife_min", default=45) or 45),
        )
        tough = mooddb.tough_active(scope) and bool(
            on(cfg, "L2_emotion", "sub", "tough_mouth", default=True))

        head = (f"【现在的心情】愉悦{cur['p']:.2f} 唤醒{cur['a']:.2f} "
                f"支配{cur['d']:.2f}")
        lt = mooddb._label_text(cur.get("labels") or {}, tough)
        if lt:
            head += "；" + lt
        body = head + "\n" + FOOTER
        return Block("mood", body), []
    except Exception as e:
        log.warning("L2 构建失败：%s", e)
        return None, []
