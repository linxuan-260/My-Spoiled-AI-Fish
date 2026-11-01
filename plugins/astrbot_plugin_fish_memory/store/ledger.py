"""注入账本：每次注入了哪些记忆、多少字、是否影子模式。

L5 的纠正惩罚要读它（只读 ATTACH 也走这个表），灰度期主要看它也靠它。
"""

from __future__ import annotations

import json
import logging
import sqlite3

from . import dbutil, memorydb

log = logging.getLogger("fishmem.ledger")


def write(*, scope_id: str, umo: str, layer: str, memory_ids: list[int],
          block_chars: int, total_chars: int, shadow: bool, query: str) -> None:
    try:
        p = memorydb.ensure()
        with dbutil.tx(p) as con:
            con.execute(
                "INSERT INTO injection_ledger(ts, scope_id, umo, layer, memory_ids,"
                " block_chars, total_chars, shadow, query) VALUES (?,?,?,?,?,?,?,?,?)",
                (dbutil.iso(), scope_id or "*", umo or "", layer,
                 json.dumps([int(i) for i in memory_ids]), int(block_chars),
                 int(total_chars), 1 if shadow else 0, (query or "")[:200]),
            )
    except sqlite3.Error as e:
        log.debug("账本写入失败：%s", e)


def last_memory_ids(scope_id: str = "*", within_min: int = 30) -> list[int]:
    """取这个会话最近一次注入的记忆 id，供纠正惩罚使用。"""
    p = memorydb.db_file()
    if not p.exists():
        return []
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return []
    try:
        r = con.execute(
            "SELECT memory_ids FROM injection_ledger WHERE scope_id=? AND layer='memory' "
            "AND julianday(ts) >= julianday('now', ?) ORDER BY id DESC LIMIT 1",
            (scope_id or "*", f"-{int(within_min)} minutes"),
        ).fetchone()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    if not r:
        return []
    try:
        return [int(i) for i in json.loads(r[0] or "[]")]
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def recent(limit: int = 50) -> list[dict]:
    p = memorydb.db_file()
    if not p.exists():
        return []
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return []
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM injection_ledger ORDER BY id DESC LIMIT ?", (int(limit),))]
    except sqlite3.Error:
        return []
    finally:
        con.close()
