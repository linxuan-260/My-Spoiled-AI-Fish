"""L3 生活层：把 daemon 攒下的活动/随笔渲染成一段背景。"""

from __future__ import annotations

import logging

from ..budget import Block
from ..config import on
from ..store import lifedb

log = logging.getLogger("fishmem.l3")

HEADER = ("【你这一轮真的在做的事】这是事实，不是设定。"
          "有人问你在干嘛，照这个答，别编吃饭瘫床翻旧账。")


def build(ctx, cfg) -> tuple[Block | None, list[int]]:
    try:
        if not on(cfg, "L3_life", "enabled", default=False):
            return None, []
        if not on(cfg, "L3_life", "sub", "random_activity", default=True) and not on(
            cfg, "L3_life", "sub", "notes_temp_1d", default=True
        ):
            return None, []

        # 整块必须 <= master.budgets.life，否则 compose 会整块丢弃（以前就是这么被静默丢的）
        limit = int(on(cfg, "master", "budgets", "life", default=60) or 60)
        lines: list[str] = []
        used = len(HEADER)
        if on(cfg, "L3_life", "sub", "random_activity", default=True):
            act = lifedb.latest_activity()
            if act and lifedb.dbutil.age_hours(act.get("ts")) <= 12:
                detail = act.get("detail") or act.get("topic") or ""
                line = f"· 刚才{act['kind']}：{detail}"
                if used + 1 + len(line) <= limit:
                    lines.append(line)
                    used += 1 + len(line)
        if on(cfg, "L3_life", "sub", "notes_temp_1d", default=True):
            for n in lifedb.live_notes(2):
                line = f"· {n}"
                if used + 1 + len(line) > limit:
                    break
                lines.append(line)
                used += 1 + len(line)
        if not lines:
            return None, []
        return Block("life", HEADER + "\n" + "\n".join(lines)), []
    except Exception as e:
        log.warning("L3 构建失败：%s", e)
        return None, []
