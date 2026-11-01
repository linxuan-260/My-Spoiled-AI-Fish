"""L1 记忆层：一直记着的事 + 这一句刚想起的事 -> 一个注入块。

两段的区别，就是「真记得」和「翻旧账」的区别：

    一直记着的（memories.tier='core'）
        与这句话说了什么无关，每轮都在。措辞是「你本来就知道」，
        不复述，只用来决定这轮怎么接话。

    刚想起来的（retrieve 检索结果）
        这句话触发的，用完即弃。有就自然带一句，没有就不提。

2026-11-01 之前只有检索那一段：每轮现查现拼、按关键词命中，模型看到的是
「查到几条记录」，于是她也真的把它当档案念。记忆越多，越不像「记得」。
"""

from __future__ import annotations

import logging

from ..budget import Block
from ..config import dget, on
from ..store import ledger, memorydb, retrieve

log = logging.getLogger("fishmem.l1")

HEADER_CORE = "【一直记着的】你本来就知道的事，别一条条念，只用来决定怎么接话。"
HEADER_RECALL = "【刚想起来的】对方提到才想到的，自然带过就行。"
LINE_MAX = 46          # 单行记忆最多这么多字
MAX_LINES = 5

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


def _render(budget: int, sections) -> str:
    """按预算拼段落：sections = [(header, [line, ...]), ...]。

    装不下的整行丢掉，绝不截半句；一段连标题都放不下就整段不要。
    这样产出的块一定 <= 单层预算，不会被 compose 整块丢弃。
    """
    parts: list[str] = []
    used = 0
    for header, lines in sections:
        lines = [ln for ln in lines if ln]
        if not lines:
            continue
        # 标题 + 换行；后面可能还有一段，所以都按至少一个换行算
        cost = len(header) + 1 + (1 if parts else 0)
        if used + cost > budget:
            continue
        keep: list[str] = []
        for ln in lines:
            if used + cost + len(ln) + 1 > budget:
                break
            keep.append(ln)
            cost += len(ln) + 1
        if not keep:
            continue
        parts.append(header + "\n" + "\n".join(keep))
        used += cost
    return "\n".join(parts)


def _visible_args(cfg) -> dict:
    return {
        "public_scopes": on(cfg, "master", "public_scopes", default=[]) or [],
        "public_readers": on(cfg, "master", "public_readers", default=[]) or [],
    }


def build(ctx, cfg) -> tuple[Block | None, list[int]]:
    try:
        if not on(cfg, "L1_memory", "enabled", default=False):
            return None, []

        sub = dget(cfg, "L1_memory", "sub")
        if not sub.get("keyword", True) and not sub.get("semantic", False):
            return None, []

        budget = int(on(cfg, "master", "budgets", "memory", default=138) or 138)
        max_lines = int(on(cfg, "L1_memory", "max_lines", default=MAX_LINES) or MAX_LINES)
        vis = _visible_args(cfg)

        # ---- 一直记着的：不检索，按「够不够格一直记着」排序 ----
        core_hits = []
        core_cfg = dget(cfg, "L1_memory", "core")
        if core_cfg.get("enabled", True):
            core_hits = retrieve.core_memories(
                scope_id=ctx.scope_id or "*",
                sender_id=ctx.sender_id or "",
                limit=int(core_cfg.get("max_lines", 2) or 2),
                **vis,
            )
        core_lines = [f"· {_trim(h.text)}" for h in core_hits]
        core_ids = [h.mid for h in core_hits]

        # ---- 刚想起来的：这句话触发 ----
        semantic_on = bool(sub.get("semantic")) and memorydb.has_vectors()
        query = getattr(ctx, "user_text", "") or ""

        # 最近几轮刚注入过的先让一让，别隔两句就说同一件事
        avoid: set[int] = set()
        cooldown = int(on(cfg, "L1_memory", "repeat_cooldown_min", default=20) or 0)
        if cooldown > 0:
            try:
                avoid = set(ledger.recent_memory_ids(ctx.scope_id or "*",
                                                     within_min=cooldown))
            except Exception as e:
                log.debug("读取最近注入失败（忽略）：%s", e)

        k = int(on(cfg, "L1_memory", "k", default=3) or 3)
        hits = retrieve.retrieve(
            query=query,
            scope_id=ctx.scope_id or "*",
            sender_id=ctx.sender_id or "",
            k=k + len(core_ids) + len(avoid),
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
            **vis,
        )
        # 沉淀过的单独一段写过了，这里不重复；刚说过的先跳过
        recall = [h for h in hits if h.mid not in avoid and h.mid not in core_ids]
        recall = recall[: max(0, max_lines - len(core_lines))]
        recall_lines = [f"· [{KIND_TAG.get(h.kind, '记')}] {_trim(h.text)}" for h in recall]

        body = _render(budget, [(HEADER_CORE, core_lines), (HEADER_RECALL, recall_lines)])
        if not body:
            return None, []

        try:
            # 沉淀层不算「被检索到」，否则核心记忆会自我强化、永远下不来
            memorydb.bump_access([h.mid for h in recall])
        except Exception:
            pass
        return Block("memory", body), core_ids + [h.mid for h in recall]
    except Exception as e:
        log.warning("L1 构建失败：%s", e)
        return None, []
