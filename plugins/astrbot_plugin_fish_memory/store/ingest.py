"""会话原文入队。主进程只 append，worker 进程消费。

为什么不让主进程直接提炼记忆：
    提炼要调 LLM，慢、可能失败、可能超时。放主进程会把回复拖慢甚至卡死。
    主进程只做一次 INSERT（微秒级），worker 落后了也不影响对话。
"""

from __future__ import annotations

import logging
import sqlite3

from . import dbutil, memorydb

log = logging.getLogger("fishmem.ingest")


def append(*, scope_id: str, umo: str, role: str, text: str,
           sender_id: str = "", sender_name: str = "", msg_id: str = "") -> None:
    text = (text or "").strip()
    if not text:
        return
    try:
        p = memorydb.ensure()
        with dbutil.tx(p) as con:
            con.execute(
                "INSERT INTO ingest_queue(scope_id, umo, role, sender_id, sender_name,"
                " msg_id, text, ts) VALUES (?,?,?,?,?,?,?,?)",
                (scope_id or "*", umo or "", role, sender_id or "", sender_name or "",
                 msg_id or "", text[:2000], dbutil.iso()),
            )
    except sqlite3.Error as e:
        log.debug("入队失败：%s", e)


def pending(limit: int = 200) -> list[dict]:
    p = memorydb.db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM ingest_queue WHERE consumed_at IS NULL ORDER BY id LIMIT ?",
                (int(limit),))]
        except sqlite3.Error:
            return []


def pending_count() -> int:
    p = memorydb.db_file()
    if not p.exists():
        return 0
    with dbutil.ro(p) as con:
        if con is None:
            return 0
        try:
            return int(con.execute(
                "SELECT count(*) FROM ingest_queue WHERE consumed_at IS NULL").fetchone()[0])
        except sqlite3.Error:
            return 0


def mark_consumed(ids: list[int], batch_id: int) -> None:
    if not ids:
        return
    with dbutil.tx(memorydb.db_file()) as con:
        ph = ",".join("?" * len(ids))
        con.execute(
            f"UPDATE ingest_queue SET consumed_at=?, batch_id=? WHERE id IN ({ph})",
            [dbutil.iso(), int(batch_id)] + [int(i) for i in ids],
        )


def trim(max_backlog: int = 500) -> int:
    """积压过多时丢最老的，防止库无限膨胀。"""
    p = memorydb.db_file()
    if not p.exists():
        return 0
    with dbutil.tx(p) as con:
        cur = con.execute(
            "DELETE FROM ingest_queue WHERE consumed_at IS NULL AND id NOT IN "
            "(SELECT id FROM ingest_queue WHERE consumed_at IS NULL ORDER BY id DESC LIMIT ?)",
            (int(max_backlog),),
        )
        return cur.rowcount or 0
