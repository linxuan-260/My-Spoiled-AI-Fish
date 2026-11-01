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
