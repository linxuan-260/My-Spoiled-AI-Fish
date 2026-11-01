"""人格只读守卫：算 personas.system_prompt 的 sha256，和基线比。

本插件从不写 personas 表。这个模块只做两件事：
    1. 首次运行把当前 sha256 记进配置（persona_guard.sha256）；
    2. 之后每 check_every_min 分钟比一次，变了就告警（可选自动还原）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time

from . import dbutil, paths

log = logging.getLogger("fishmem.persona")

PERSONA_ID = "1"

_backup_cache: dict | None = None
_last_check = 0.0
_lock = threading.Lock()


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()


def read_persona(persona_id: str = PERSONA_ID) -> dict | None:
    if not paths.MAIN_DB.exists():
        return None
    try:
        con = dbutil.connect(paths.MAIN_DB, readonly=True)
    except sqlite3.Error:
        return None
    try:
        r = con.execute("SELECT * FROM personas WHERE persona_id=?", (persona_id,)).fetchone()
        return dict(r) if r else None
    except sqlite3.Error as e:
        log.debug("读 personas 失败：%s", e)
        return None
    finally:
        con.close()


def current_hash(persona_id: str = PERSONA_ID) -> str:
    row = read_persona(persona_id)
    return sha256_text(row["system_prompt"]) if row else ""


def find_backup() -> dict | None:
    """在 qqbot/backup/*-persona/persona_1.json 里找最新的一份。"""
    global _backup_cache
    if _backup_cache is not None:
        return _backup_cache or None

    root = paths.MAIN_DB.parent.parent / "backup"
    if not root.exists():
        _backup_cache = {}
        return None
    cands = sorted(root.glob("*-persona/persona_1.json"))
    if not cands:
        _backup_cache = {}
        return None
    try:
        rows = json.loads(cands[-1].read_text(encoding="utf-8"))
        row = rows[0] if isinstance(rows, list) else rows
        _backup_cache = row
        log.info("人格备份基线：%s", cands[-1])
        return row
    except Exception as e:
        log.warning("人格备份读不了：%s", e)
        _backup_cache = {}
        return None


def check(cfg, *, force: bool = False, write_back=None) -> dict:
    """返回 {ok, changed, current, expected, restored}。不抛异常。

    write_back: 可选回调 (patch: dict) -> None，用于首次把算出的 sha256 写回配置。
    """
    global _last_check
    guard = cfg.get("persona_guard") if cfg else None
    if not guard or not guard.enabled:
        return {"ok": True, "skipped": True}

    every = float(guard.check_every_min or 10) * 60
    now = time.time()
    with _lock:
        if not force and now - _last_check < every:
            return {"ok": True, "skipped": True}
        _last_check = now

    cur = current_hash()
    expected = str(guard.sha256 or "").strip().lower()

    if not expected:
        if cur and write_back is not None:
            try:
                write_back({"persona_guard": {"sha256": cur}})
                log.info("已记录人格基线 sha256=%s", cur[:16])
            except Exception as e:
                log.warning("写回人格基线失败：%s", e)
        return {"ok": True, "current": cur, "expected": "", "initialized": True}

    if cur == expected:
        return {"ok": True, "current": cur, "expected": expected}

    log.error("人格被改动了！expected=%s current=%s", expected[:16], cur[:16])
    result = {"ok": False, "changed": True, "current": cur, "expected": expected,
              "restored": False}

    if guard.auto_restore:
        row = find_backup()
        if row and row.get("system_prompt"):
            try:
                con = dbutil.connect(paths.MAIN_DB)
                try:
                    con.execute(
                        "UPDATE personas SET system_prompt=?, updated_at=? WHERE persona_id=?",
                        (row["system_prompt"], row.get("updated_at"), PERSONA_ID),
                    )
                finally:
                    con.close()
                result["restored"] = True
                log.warning("已按备份还原人格")
            except sqlite3.Error as e:
                log.error("还原失败：%s", e)
    return result


def summary() -> dict:
    row = read_persona()
    if not row:
        return {"found": False}
    p = row.get("system_prompt") or ""
    return {
        "found": True,
        "persona_id": row.get("persona_id"),
        "len": len(p),
        "sha256": sha256_text(p),
        "updated_at": row.get("updated_at"),
    }
