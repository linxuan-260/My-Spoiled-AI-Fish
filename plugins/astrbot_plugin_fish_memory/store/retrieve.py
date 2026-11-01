"""五路加权检索。

    S = Σ w_i · score_i           （某路关闭时，剩余权重按比例重归一化）
    keyword  ← bm25 取负后 min-max
    graph    ← 以 keyword 头部为种子，沿 memory_edges 扩散
    importance / freshness ← 直接取值
    semantic ← bge-m3 onnx 余弦；模型不在就整路跳过

    最终：S *= D(kind)            D 只对 episode / reflection 生效
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from . import dbutil
from .memorydb import db_file, ensure, neighbours, search_fts

log = logging.getLogger("fishmem.retrieve")

DEFAULT_WEIGHTS = {
    "semantic": 0.18,
    "keyword": 0.46,
    "graph": 0.11,
    "importance": 0.14,
    "freshness": 0.11,
}


@dataclass
class Hit:
    mid: int
    score: float
    kind: str = "fact"
    text: str = ""
    category: str = "general"
    importance: float = 0.5
    created_at: str = ""
    parts: dict = field(default_factory=dict)


def _minmax(vals: dict[int, float]) -> dict[int, float]:
    if not vals:
        return {}
    lo = min(vals.values())
    hi = max(vals.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in vals}
    return {k: (v - lo) / (hi - lo) for k, v in vals.items()}


def _decay(kind: str, created_at: str, halflives: dict) -> float:
    hl = float(halflives.get(kind, 0) or 0)
    if hl <= 0:
        return 1.0
    age = dbutil.age_days(created_at)
    return 0.5 ** (age / hl)


def retrieve(
    *,
    query: str,
    scope_id: str = "*",
    k: int = 3,
    weights: dict | None = None,
    enabled: dict | None = None,
    halflives: dict | None = None,
    freshness_halflife_days: float = 30.0,
    min_score: float = 0.12,
    category_cap: int = 0,
    use_decay: bool = True,
    sender_id: str = "",
    public_scopes: list[str] | tuple = (),
    public_readers: list[str] | tuple = (),
) -> list[Hit]:
    """主检索入口。任何异常都返回 []，绝不让记忆层把对话搞挂。

    sender_id      「同一个人跨会话」：visibility='owner' 且 meta.owner_id
                   等于当前说话人的记忆，即使属于别的会话也参与打分。
    public_scopes  共享层来源：这些会话的记忆可以被别的会话检索到。
    public_readers 共享层读者：为空=所有会话都能读来源；非空=只有这些会话能读。
    """
    try:
        return _retrieve(
            query=query, scope_id=scope_id, k=k, weights=weights, enabled=enabled,
            halflives=halflives, freshness_halflife_days=freshness_halflife_days,
            min_score=min_score, category_cap=category_cap, use_decay=use_decay,
            sender_id=sender_id,
            public_scopes=tuple(public_scopes or ()),
            public_readers=tuple(public_readers or ()),
        )
    except Exception as e:
        log.warning("检索异常，按无记忆处理：%s", e, exc_info=False)
        return []


def _owner_of(row) -> str:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (TypeError, ValueError):
        return ""
    return str(meta.get("owner_id") or "")


def _visible(row, scope_id: str, sender_id: str, public_scopes=(),
             public_readers=()) -> bool:
    """public=共享层；public_scopes 里的会话是「共享来源」，其记忆默认所有会话可见，
    配了 public_readers 后就只对这些会话（以及来源会话自己）可见；
    owner=同一个人跨会话；scope='*'/'' 或等于当前会话 = 本会话私有。"""
    if row["status"] != "active":
        return False
    vis = str(row["visibility"] or "private")
    s = row["scope_id"] or ""
    readers = tuple(public_readers or ())
    shared_ok = (not readers) or scope_id in readers or scope_id in public_scopes
    if vis == "public":
        return shared_ok
    if s in ("*", "") or s == scope_id:
        return True
    if s and s in public_scopes:
        return shared_ok
    if vis == "owner" and sender_id and _owner_of(row) == str(sender_id):
        return True
    return False


def _retrieve(*, query, scope_id, k, weights, enabled, halflives,
              freshness_halflife_days, min_score, category_cap, use_decay,
              sender_id="", public_scopes=(), public_readers=()) -> list[Hit]:
    if not (query or "").strip():
        return []
    p = ensure()
    if not p.exists():
        return []

    w = dict(DEFAULT_WEIGHTS)
    w.update({kk: float(vv) for kk, vv in (weights or {}).items() if kk in w})
    en = {"semantic": False, "keyword": True, "graph": True}
    en.update({kk: bool(vv) for kk, vv in (enabled or {}).items() if kk in en})

    con = dbutil.connect(p, readonly=True)
    try:
        kw_pairs = search_fts(con, query, limit=300)
        if not kw_pairs:
            return []

        # 只保留 active 且可见的记忆
        ids = [m for m, _ in kw_pairs]
        ph = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT id, kind, text, category, importance, created_at, scope_id, status, "
            f"visibility, meta FROM memories WHERE id IN ({ph})", ids,
        ).fetchall()
        info = {int(r["id"]): r for r in rows if r["status"] == "active"}

        info = {mid: r for mid, r in info.items()
                if _visible(r, scope_id, sender_id, public_scopes, public_readers)}
        if not info:
            return []

        raw_kw = {m: -float(rank) for m, rank in kw_pairs if m in info}
        kw = _minmax(raw_kw)

        # ---- graph：从 keyword 头部扩散 ----
        graph: dict[int, float] = {}
        if en["graph"]:
            seeds = sorted(kw.items(), key=lambda x: -x[1])[:3]
            for sid, _s in seeds:
                for dst, _rel, ew in neighbours(con, sid, limit=6):
                    if dst in info and dst not in graph:
                        graph[dst] = ew
        gr = _minmax(graph) if graph else {}

        # ---- freshness ----
        fresh = {}
        for mid, r in info.items():
            age = dbutil.age_days(r["created_at"])
            fresh[mid] = math.exp(-math.log(2) * age / max(1.0, freshness_halflife_days))

        # ---- 权重重归一化 ----
        active_w: dict[str, float] = {}
        for name in ("semantic", "keyword", "graph"):
            if en.get(name):
                active_w[name] = w[name]
        if en["semantic"]:
            # 语义路需要向量；没有模型时由调用方把 semantic 置 False
            pass
        active_w["importance"] = w["importance"]
        active_w["freshness"] = w["freshness"]
        tot = sum(active_w.values()) or 1.0
        nw = {kk: vv / tot for kk, vv in active_w.items()}

        hits: list[Hit] = []
        for mid, r in info.items():
            s = 0.0
            parts = {}
            if "keyword" in nw:
                v = kw.get(mid, 0.0)
                parts["keyword"] = v
                s += nw["keyword"] * v
            if "graph" in nw:
                v = gr.get(mid, 0.0)
                parts["graph"] = v
                s += nw["graph"] * v
            if "importance" in nw:
                v = float(r["importance"])
                parts["importance"] = v
                s += nw["importance"] * v
            if "freshness" in nw:
                v = fresh.get(mid, 0.0)
                parts["freshness"] = v
                s += nw["freshness"] * v

            d = _decay(r["kind"], r["created_at"], halflives or {}) if use_decay else 1.0
            parts["decay"] = round(d, 4)
            s *= d

            if s < min_score:
                continue
            hits.append(
                Hit(mid=mid, score=round(s, 4), kind=str(r["kind"]), text=str(r["text"]),
                    category=str(r["category"]), importance=float(r["importance"]),
                    created_at=str(r["created_at"]), parts=parts)
            )

        hits.sort(key=lambda h: -h.score)

        # 类别配额：同一 category 最多 category_cap 条，避免一小段对话刷屏
        if category_cap and category_cap > 0:
            seen: dict[str, int] = {}
            kept = []
            for h in hits:
                n = seen.get(h.category, 0)
                if n >= category_cap:
                    continue
                seen[h.category] = n + 1
                kept.append(h)
            hits = kept

        return hits[: max(1, int(k))]
    finally:
        con.close()
