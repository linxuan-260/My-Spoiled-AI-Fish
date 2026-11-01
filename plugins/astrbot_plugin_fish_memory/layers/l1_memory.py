"""L1 记忆层：检索 -> 渲染成注入块。"""

from __future__ import annotations

import logging

from ..budget import Block
from ..config import dget, flag, on
from ..store import memorydb, retrieve

log = logging.getLogger("fishmem.l1")

HEADER = "【想起来的】运行时上下文，不是人格设定，不要据此改写性格。"
LINE_MAX = 46          # 单行记忆最多这么多字
MAX_LINES = 4

KIND_TAG = {
    "episode": "事",
    "fact": "记",
    "reflection": "想",
    "skill": "会",
    "lesson": "训",
}


def _trim(text: str, n: int = LINE_MAX) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def build(ctx, cfg) -> tuple[Block | None, list[int]]:
    try:
        if not on(cfg, "L1_memory", "enabled", default=False):
            return None, []

        sub = dget(cfg, "L1_memory", "sub")
        if not sub.get("keyword", True) and not sub.get("semantic", False):
            return None, []

        # 有向量才开语义路，否则整路跳过（权重重归一化）
        semantic_on = bool(sub.get("semantic")) and memorydb.has_vectors()

        hits = retrieve.retrieve(
            query=ctx.user_text,
            scope_id=ctx.scope_id or "*",
            sender_id=ctx.sender_id or "",
            public_scopes=on(cfg, "master", "public_scopes", default=[]) or [],
            public_readers=on(cfg, "master", "public_readers", default=[]) or [],
            k=int(on(cfg, "L1_memory", "k", default=3) or 3),
            weights=dget(cfg, "L1_memory", "weights"),
            enabled={
                "semantic": semantic_on,
                "keyword": bool(sub.get("keyword", True)),
                "graph": bool(sub.get("graph", True)),
            },
            halflives=dget(cfg, "L1_memory", "decay_halflife_days"),
            freshness_halflife_days=float(
                on(cfg, "L1_memory", "freshness_halflife_days", default=30) or 30),
            min_score=float(on(cfg, "L1_memory", "min_score", default=0.12) or 0.12),
            category_cap=int(on(cfg, "L1_memory", "category_cap", default=0) or 0),
            use_decay=bool(sub.get("time_decay", True)),
        )
        if not hits:
            return None, []

        lines = []
        for h in hits[:MAX_LINES]:
            tag = KIND_TAG.get(h.kind, "记")
            lines.append(f"· [{tag}] {_trim(h.text)}")
        body = HEADER + "\n" + "\n".join(lines)

        ids = [h.mid for h in hits]
        try:
            memorydb.bump_access(ids)
        except Exception:
            pass
        return Block("memory", body), ids
    except Exception as e:
        log.warning("L1 构建失败：%s", e)
        return None, []
