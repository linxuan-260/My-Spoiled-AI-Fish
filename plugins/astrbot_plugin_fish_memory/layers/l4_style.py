"""L4 语感层：从历史回复统计出的说话习惯。"""

from __future__ import annotations

import logging

from ..budget import Block
from ..config import on
from ..store import styledb

log = logging.getLogger("fishmem.l4")


def _bot_ids(ctx, cfg) -> set[str]:
    ids: set[str] = {"bot"}
    if ctx.self_id:
        ids.add(str(ctx.self_id))
    cfg_bot = str(on(cfg, "L4_style", "bot_id", default="") or "").strip()
    if cfg_bot:
        ids.add(cfg_bot)
    # platform_id 形如 aiocqhttp-<机器人QQ>
    if ctx.umo and ":" in ctx.umo:
        head = ctx.umo.split(":")[0]
        if "-" in head:
            ids.add(head.split("-")[-1])
    return ids


def build(ctx, cfg) -> tuple[Block | None, list[int]]:
    try:
        if not on(cfg, "L4_style", "enabled", default=False):
            return None, []
        if not on(cfg, "L4_style", "sub", "stats", default=True):
            return None, []

        stats = styledb.cached_stats()
        if not stats:
            stats = styledb.refresh(
                window_n=int(on(cfg, "L4_style", "window_n", default=300) or 300),
                bot_ids=_bot_ids(ctx, cfg),
                every_min=int(on(cfg, "L4_style", "recompute_every_min", default=30) or 30),
            )
        if not stats:
            return None, []
        body = styledb.render(
            stats, int(on(cfg, "L4_style", "max_chars", default=130) or 130))
        if not body:
            return None, []
        return Block("style", body), []
    except Exception as e:
        log.warning("L4 构建失败：%s", e)
        return None, []
