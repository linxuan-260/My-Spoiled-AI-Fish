"""发言判断的存储：读群聊现场、读写判断记录。

三份数据：

    现场      AstrBot 的 platform_message_history（data_v4.db，只读）
              谁在说话、说了什么、at 了谁、回复了谁、什么时候
    上次说话  injection_ledger 里这个会话最后一次请求（每次回复前都会写）
    判断记录  memory.db 的 speech_gate 表（说/不说 + 理由 + 现场特征）

platform_message_history 只存用户消息，机器人自己的回复不在里面；所以
「她上次开口是什么时候」只能从自己的账本里取。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3

from . import dbutil, memorydb, paths

log = logging.getLogger("fishmem.gate")


@dataclasses.dataclass
class SceneMsg:
    ts: float
    sender_id: str
    sender: str
    text: str
    at_ids: list = dataclasses.field(default_factory=list)
    reply_to: str = ""
    is_image: bool = False


_IMG_TEXT = {"[Image]", "[图片]", "[语音]", "[视频]", "[文件]", "[表情]", "[动画表情]"}


def _parse_ts(created_at: str) -> float:
    """'2026-11-01 14:52:34.564620' → epoch。库里是本地时间（+08）。"""
    s = str(created_at or "")[:19]
    try:
        import datetime as _dt

        d = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dbutil.TZ)
        return d.timestamp()
    except ValueError:
        return 0.0


def parse_content(content) -> dict:
    """从 platform_message_history.content 抠出 text / at / reply / 图片标记。"""
    out = {"text": "", "at_ids": [], "reply_to": "", "is_image": False}
    try:
        j = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return out
    if not isinstance(j, dict):
        return out
    parts: list[str] = []
    for seg in j.get("message") or ():
        if not isinstance(seg, dict):
            continue
        t = str(seg.get("type") or "")
        if t == "plain":
            s = str(seg.get("text") or "").strip()
            if not s:
                continue
            if s in _IMG_TEXT:
                out["is_image"] = True
                continue
            parts.append(s)
        elif t == "at":
            qq = str(seg.get("user_id") or seg.get("qq") or "")
            if qq:
                out["at_ids"].append(qq)
        elif t == "reply":
            out["reply_to"] = str(seg.get("sender_name") or seg.get("sender_id") or "")
        elif t in ("image", "record", "video", "file", "face", "mface"):
            out["is_image"] = True
    out["text"] = " ".join(parts).strip()
    return out


def scene(scope_id: str, limit: int = 12) -> list[SceneMsg]:
    """这个会话最近的几句群聊（旧 → 新，不含机器人自己的话）。"""
    if not paths.MAIN_DB.exists():
        return []
    try:
        con = dbutil.connect(paths.MAIN_DB, readonly=True)
    except sqlite3.Error as e:
        log.debug("主库打不开：%s", e)
        return []
    try:
        rows = con.execute(
            "SELECT created_at, sender_id, sender_name, content FROM platform_message_history "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (str(scope_id), max(1, int(limit))),
        ).fetchall()
    except sqlite3.Error as e:
        log.debug("历史表查询失败：%s", e)
        return []
    finally:
        con.close()

    out: list[SceneMsg] = []
    for r in reversed(rows):
        p = parse_content(r["content"])
        out.append(SceneMsg(
            ts=_parse_ts(r["created_at"]),
            sender_id=str(r["sender_id"] or ""),
            sender=str(r["sender_name"] or ""),
            text=p["text"], at_ids=p["at_ids"], reply_to=p["reply_to"],
            is_image=p["is_image"],
        ))
    return out


def last_speak_ts(scope_id: str) -> float | None:
    """她在这个会话里上一次被唤醒（= 上一次开口）的时间。"""
    p = memorydb.db_file()
    if not p.exists():
        return None
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return None
    try:
        r = con.execute(
            "SELECT max(ts) FROM injection_ledger WHERE scope_id=?", (str(scope_id),)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    d = dbutil.parse_iso(r[0]) if r and r[0] else None
    return d.timestamp() if d else None


# ---------------------------------------------------------------- 判断记录

def write_decision(*, scope_id: str, decision: str, reason: str,
                   score: float = 0.0, features: dict | None = None) -> None:
    try:
        with dbutil.tx(memorydb.ensure()) as con:
            con.execute(
                "INSERT INTO speech_gate(ts, scope_id, decision, reason, score, features)"
                " VALUES (?,?,?,?,?,?)",
                (dbutil.iso(), str(scope_id or "*"), str(decision), str(reason),
                 float(score or 0), json.dumps(features or {}, ensure_ascii=False)),
            )
    except sqlite3.Error as e:
        log.debug("发言判断写入失败：%s", e)


def recent(limit: int = 30, scope_id: str = "") -> list[dict]:
    p = memorydb.db_file()
    if not p.exists():
        return []
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return []
    try:
        if scope_id:
            rows = con.execute(
                "SELECT ts, scope_id, decision, reason, score, features FROM speech_gate "
                "WHERE scope_id=? ORDER BY id DESC LIMIT ?", (scope_id, int(limit))).fetchall()
        else:
            rows = con.execute(
                "SELECT ts, scope_id, decision, reason, score, features FROM speech_gate "
                "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    return [{"ts": r["ts"], "scope_id": r["scope_id"], "decision": r["decision"],
             "reason": r["reason"], "score": r["score"],
             "features": json.loads(r["features"] or "{}")} for r in rows]


def stats_today() -> dict:
    """今天的说/不说次数，按理由分组。"""
    out = {"speak": 0, "hush": 0, "reasons": {}}
    p = memorydb.db_file()
    if not p.exists():
        return out
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return out
    try:
        rows = con.execute(
            "SELECT decision, reason, count(*) FROM speech_gate "
            "WHERE date(ts)=date('now') GROUP BY decision, reason"
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for r in rows:
        d, reason, n = str(r[0]), str(r[1] or "-"), int(r[2] or 0)
        if d == "speak":
            out["speak"] += n
        else:
            out["hush"] += n
        out["reasons"][reason] = out["reasons"].get(reason, 0) + n
    return out


def count_reason(reason: str, hours: int = 24) -> int:
    """最近这么久里，某个理由出现了多少次（「被要求闭嘴」用来加码）。"""
    p = memorydb.db_file()
    if not p.exists():
        return 0
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return 0
    try:
        r = con.execute(
            "SELECT count(*) FROM speech_gate WHERE reason=? AND decision='hush' "
            "AND julianday(ts) >= julianday('now', ?)",
            (str(reason), f"-{max(1, int(hours))} hours"),
        ).fetchone()
        return int(r[0] if r else 0)
    except sqlite3.Error:
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------- 被要求闭嘴

def set_mute(scope_id: str, minutes: int, reason: str = "") -> str:
    """沉默到什么时候。scope_id='*' 表示所有会话。返回 until（ISO 字符串）。"""
    import datetime as _dt

    minutes = max(1, int(minutes))
    until = dbutil.iso(dbutil.now() + _dt.timedelta(minutes=minutes))
    try:
        with dbutil.tx(memorydb.ensure()) as con:
            con.execute(
                "INSERT INTO speech_mute(scope_id, until_ts, reason, updated_at)"
                " VALUES (?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET"
                " until_ts=excluded.until_ts, reason=excluded.reason,"
                " updated_at=excluded.updated_at",
                (str(scope_id or "*"), until, str(reason or ""), dbutil.iso()),
            )
    except sqlite3.Error as e:
        log.debug("静音写入失败：%s", e)
    return until


def clear_mute(scope_id: str = "*") -> int:
    try:
        with dbutil.tx(memorydb.ensure()) as con:
            cur = con.execute("DELETE FROM speech_mute WHERE scope_id=?", (str(scope_id or "*"),))
            return cur.rowcount or 0
    except sqlite3.Error:
        return 0


def mute_state(scope_id: str) -> dict | None:
    """还在静音里就返回 {until, reason}，否则 None。全局 '*' 优先。"""
    p = memorydb.db_file()
    if not p.exists():
        return None
    try:
        con = dbutil.connect(p, readonly=True)
    except sqlite3.Error:
        return None
    try:
        rows = con.execute(
            "SELECT scope_id, until_ts, reason FROM speech_mute "
            "WHERE scope_id IN (?, '*') ORDER BY scope_id='*' DESC", (str(scope_id),)
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    now = dbutil.now()
    for r in rows:
        d = dbutil.parse_iso(r["until_ts"])
        if d and d > now:
            return {"until": r["until_ts"], "reason": r["reason"] or ""}
    return None


def prune(days: int = 14) -> int:
    """删掉过期判断记录和过期静音（daily_digest 每晚跑）。"""
    p = memorydb.db_file()
    if not p.exists():
        return 0
    try:
        with dbutil.tx(p) as con:
            cur = con.execute("DELETE FROM speech_gate WHERE julianday(ts) < julianday('now', ?)",
                              (f"-{max(1, int(days))} days",))
            con.execute("DELETE FROM speech_mute WHERE julianday(until_ts) < julianday('now')")
            return cur.rowcount or 0
    except sqlite3.Error as e:
        log.debug("发言判断清理失败：%s", e)
        return 0
