"""L1 记忆库读写。"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from . import dbutil
from .bigram import grams_text, norm
from .paths import db_path

log = logging.getLogger("fishmem.mem")

KINDS = ("episode", "fact", "reflection", "skill", "lesson")


def db_file() -> Path:
    return db_path("memory", "memory.db")


def ensure() -> Path:
    p = db_file()
    dbutil.ensure_schema(p, dbutil.DDL_MEMORY, "memory")
    return p


def make_hash(kind: str, text: str) -> str:
    raw = f"{kind}\x00{norm(text)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def add(
    kind: str,
    text: str,
    *,
    category: str = "general",
    importance: float = 0.5,
    valence: float = 0.0,
    scope_id: str = "*",
    visibility: str = "private",
    source: str = "chat",
    meta: dict | None = None,
    arousal: float | None = None,
    dominance: float | None = None,
    edges: list[tuple[int, str, float]] | None = None,
) -> int | None:
    """写一条记忆。同 hash 已存在则只更新重要度并返回原 id（去重）。"""
    text = (text or "").strip()
    if not text:
        return None
    if kind not in KINDS:
        kind = "fact"
    h = make_hash(kind, text)

    p = ensure()
    ts = dbutil.iso()
    with dbutil.tx(p) as con:
        row = con.execute(
            "SELECT id FROM memories WHERE hash=? AND status!='deleted'", (h,)
        ).fetchone()
        if row:
            mid = int(row[0])
            con.execute(
                "UPDATE memories SET importance=MAX(importance, ?), last_access=? WHERE id=?",
                (float(importance), ts, mid),
            )
            # 可见性只升级不降级：private < group < owner < public
            rank = {"private": 0, "group": 1, "owner": 2, "public": 3}
            old = con.execute(
                "SELECT visibility, scope_id FROM memories WHERE id=?", (mid,)
            ).fetchone()
            old_vis = str(old["visibility"] or "private") if old else "private"
            if rank.get(str(visibility), 0) > rank.get(old_vis, 0):
                con.execute(
                    "UPDATE memories SET visibility=?, scope_id=?, meta=? WHERE id=?",
                    (visibility, scope_id, json.dumps(meta or {}, ensure_ascii=False), mid),
                )
                con.execute(
                    "UPDATE memories_fts SET scope_id=? WHERE memory_id=?", (scope_id, mid)
                )
            return mid
        cur = con.execute(
            """INSERT INTO memories
               (kind, text, text_norm, hash, category, importance, valence, arousal, dominance,
                created_at, last_access, status, source, meta, visibility, scope_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",
            (
                kind, text, norm(text), h, category, float(importance), float(valence),
                arousal, dominance, ts, ts, source,
                json.dumps(meta or {}, ensure_ascii=False), visibility, scope_id,
            ),
        )
        mid = int(cur.lastrowid)
        con.execute(
            "INSERT INTO memories_fts(bigram, memory_id, scope_id) VALUES (?,?,?)",
            (grams_text(text), mid, scope_id),
        )
        if edges:
            for dst, rel, w in edges:
                if dst and dst != mid:
                    con.execute(
                        "INSERT OR IGNORE INTO memory_edges(src_id,dst_id,rel,weight,created_at) "
                        "VALUES (?,?,?,?,?)",
                        (mid, int(dst), rel, float(w), ts),
                    )
    return mid


def bump_access(ids: list[int]) -> None:
    if not ids:
        return
    ts = dbutil.iso()
    try:
        with dbutil.tx(db_file()) as con:
            con.executemany(
                "UPDATE memories SET access_count=access_count+1, last_access=? WHERE id=?",
                [(ts, int(i)) for i in ids],
            )
    except sqlite3.Error as e:
        log.debug("bump_access 失败：%s", e)


def get(con, mid: int):
    return con.execute("SELECT * FROM memories WHERE id=?", (int(mid),)).fetchone()


def search_fts(con, query: str, limit: int = 300) -> list[tuple[int, float]]:
    """返回 [(memory_id, bm25)]，bm25 越小越相关（负数）。"""
    from .bigram import fts_query

    q = fts_query(query)
    if not q:
        return []
    try:
        rows = con.execute(
            "SELECT memory_id, bm25(memories_fts) AS r FROM memories_fts "
            "WHERE memories_fts MATCH ? ORDER BY r LIMIT ?",
            (q, int(limit)),
        ).fetchall()
    except sqlite3.Error as e:
        log.debug("FTS 查询失败（q=%r）：%s", q[:80], e)
        return []
    return [(int(r[0]), float(r[1])) for r in rows]


def neighbours(con, mid: int, limit: int = 6) -> list[tuple[int, str, float]]:
    try:
        rows = con.execute(
            "SELECT dst_id, rel, weight FROM memory_edges WHERE src_id=? "
            "ORDER BY weight DESC LIMIT ?",
            (int(mid), int(limit)),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(int(r[0]), str(r[1]), float(r[2])) for r in rows]


def archive_old(after_days: int = 90) -> int:
    try:
        with dbutil.tx(db_file()) as con:
            cur = con.execute(
                "UPDATE memories SET status='archived' "
                "WHERE status='active' AND kind='episode' "
                "AND julianday(created_at) < julianday('now', ?)",
                (f"-{int(after_days)} days",),
            )
            return cur.rowcount or 0
    except sqlite3.Error as e:
        log.debug("archive_old 失败：%s", e)
        return 0


# ---------------------------------------------------------------- 沉淀 / 分层
# core   = 一直记着的事：与这一句话说了什么无关，每轮都注入（l1_memory）
# normal = 普通记忆：只有被检索到才出现
#
# 为什么要有这一层：检索出来的东西是「查到几条记录」，模型看到的是档案；
# 沉淀下来的东西才是「本来就记得」。不是新的检索算法，是换一类东西进 prompt。

CORE_KINDS = ("fact", "reflection", "lesson")

CORE_DEFAULTS = {
    "min_importance": 0.6,      # 升 core 的门槛
    "strong_importance": 0.7,   # 重要度到这条线，不问被想起过几次
    "min_access": 3,            # 否则要求至少被检索到过这么多次
    "max_core": 30,
    "demote_after_days": 120,   # 这么久没被检索到，就不再算「一直记着」
}


def consolidate(**kw) -> dict:
    """把反复想起、够重要的事沉淀成 core；太久没想起的降回 normal。

    只在 daily_digest 里跑（也可手动 `share_memory.py --consolidate`）。
    幂等：同一份数据跑几次结果一样。
    """
    opt = dict(CORE_DEFAULTS)
    opt.update({k: v for k, v in kw.items() if k in opt and v is not None})

    out = {"promoted": 0, "demoted": 0, "core_total": 0}
    p = db_file()
    if not p.exists():
        return out

    kinds = ",".join("?" * len(CORE_KINDS))
    try:
        with dbutil.tx(p) as con:
            # 1) 升：够重要，并且要么被反复想起，要么重要度本身就够高
            cur = con.execute(
                f"UPDATE memories SET tier='core' "
                f"WHERE status='active' AND tier='normal' AND kind IN ({kinds}) "
                f"AND importance >= ? AND (access_count >= ? OR importance >= ?)",
                (*CORE_KINDS, float(opt["min_importance"]),
                 int(opt["min_access"]), float(opt["strong_importance"])),
            )
            out["promoted"] = cur.rowcount or 0

            # 2) 降：归档了，或者很久没被检索到（never accessed 的看创建时间）
            cur = con.execute(
                "UPDATE memories SET tier='normal' WHERE tier='core' AND ("
                "status!='active' OR COALESCE(julianday(last_access), "
                "julianday(created_at)) < julianday('now', ?))",
                (f"-{int(opt['demote_after_days'])} days",),
            )
            out["demoted"] = cur.rowcount or 0

            # 3) 封顶：超过 max_core 就按「重要度、被想起次数」留最好的
            n = int(con.execute(
                "SELECT count(*) FROM memories WHERE tier='core' AND status='active'"
            ).fetchone()[0])
            max_core = int(opt["max_core"] or 0)
            if max_core > 0 and n > max_core:
                con.execute(
                    "UPDATE memories SET tier='normal' WHERE id IN ("
                    " SELECT id FROM memories WHERE tier='core' AND status='active'"
                    " ORDER BY importance ASC, access_count ASC LIMIT ?)",
                    (n - max_core,),
                )

            out["core_total"] = int(con.execute(
                "SELECT count(*) FROM memories WHERE tier='core' AND status='active'"
            ).fetchone()[0])
    except sqlite3.Error as e:
        log.warning("沉淀整理失败：%s", e)
    return out


def set_tier(ids: list[int], tier: str = "core") -> int:
    """人工改层（share_memory.py --core / --normal）。tier 只认 core / normal。"""
    if not ids or tier not in ("core", "normal"):
        return 0
    try:
        with dbutil.tx(db_file()) as con:
            n = 0
            for mid in ids:
                cur = con.execute("UPDATE memories SET tier=? WHERE id=?",
                                  (tier, int(mid)))
                n += cur.rowcount or 0
            return n
    except sqlite3.Error as e:
        log.warning("set_tier 失败：%s", e)
        return 0


def counts() -> dict:
    out = {"total": 0, "active": 0}
    p = db_file()
    if not p.exists():
        return out
    with dbutil.ro(p) as con:
        if con is None:
            return out
        try:
            out["total"] = con.execute("SELECT count(*) FROM memories").fetchone()[0]
            out["active"] = con.execute(
                "SELECT count(*) FROM memories WHERE status='active'"
            ).fetchone()[0]
        except sqlite3.Error:
            pass
    return out


def has_vectors() -> bool:
    """语义路只有在真的有向量时才开。"""
    p = db_file()
    if not p.exists():
        return False
    with dbutil.ro(p) as con:
        if con is None:
            return False
        try:
            return bool(con.execute(
                "SELECT 1 FROM memory_vectors LIMIT 1").fetchone())
        except sqlite3.Error:
            return False
