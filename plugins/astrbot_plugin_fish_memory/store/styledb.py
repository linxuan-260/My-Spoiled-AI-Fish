"""L4 语感：从历史 bot 回复里统计说话习惯，并缓存成一行提示。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import statistics
from pathlib import Path

from . import dbutil, paths
from .paths import db_path

log = logging.getLogger("fishmem.style")

FILLERS = ["嗯", "啊", "哦", "噢", "嘿", "唉", "诶", "呗", "嘛", "呀", "啦", "咯", "嘞", "哈"]
_TERM_PUNCT = "。！？!?…~～"

_EMOJI_ONLY = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")

# 形如 "[Image]" 的占位符不是真内容
_SKIP_PLAIN = re.compile(
    r"^\[(Image|语音|Video|文件|表情|At|Reply|Face|图片).*\]$", re.I)


def db_file() -> Path:
    return db_path("style", "style.db")


def ensure() -> Path:
    p = db_file()
    dbutil.ensure_schema(p, dbutil.DDL_STYLE, "style")
    return p


# ------------------------------------------------------------------ 抽取
def extract_text(content: str) -> str:
    """从 platform_message_history.content 里抠出纯文本。"""
    try:
        j = json.loads(content) if isinstance(content, str) else content
    except Exception:
        return ""
    if not isinstance(j, dict):
        return ""
    parts = []
    for seg in j.get("message") or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "plain":
            continue
        t = str(seg.get("text") or "")
        if not t or _SKIP_PLAIN.match(t):
            continue
        parts.append(t)
    return "\n".join(parts).strip()


def _is_bot(content: str, sender_id: str, bot_ids: set[str]) -> bool:
    try:
        j = json.loads(content) if isinstance(content, str) else content
        if isinstance(j, dict) and str(j.get("type")) == "bot":
            return True
    except Exception:
        pass
    return str(sender_id) in bot_ids


def fetch_bot_texts(window_n: int, bot_ids: set[str]) -> list[str]:
    """取最近 window_n 条 bot 回复的纯文本。"""
    if not paths.MAIN_DB.exists():
        return []
    out: list[str] = []
    try:
        con = dbutil.connect(paths.MAIN_DB, readonly=True)
    except sqlite3.Error as e:
        log.debug("主库打不开：%s", e)
        return []
    try:
        rows = con.execute(
            "SELECT content, sender_id, platform_id FROM platform_message_history "
            "ORDER BY id DESC LIMIT ?", (int(window_n) * 4,),
        ).fetchall()
    except sqlite3.Error as e:
        log.debug("历史表查询失败：%s", e)
        rows = []
    finally:
        con.close()

    base = set(bot_ids) | {"bot"}
    for r in rows:
        sid = str(r["sender_id"] or "")
        pid = str(r["platform_id"] or "")
        ids = set(base)
        if "-" in pid:
            ids.add(pid.split("-")[-1])
        if not _is_bot(r["content"], sid, ids):
            continue
        t = extract_text(r["content"])
        if t:
            out.append(t)
        if len(out) >= window_n:
            break
    return out


# ------------------------------------------------------------------ 统计
def compute(texts: list[str]) -> dict:
    texts = [t for t in texts if t]
    if not texts:
        return {}
    lens = [len(t) for t in texts]
    n_all = max(1, len(texts))
    no_punct = sum(1 for t in texts if not any(c in _TERM_PUNCT for c in t))
    emoji = sum(1 for t in texts if _EMOJI_ONLY.search(t))
    qs = sum(1 for t in texts if t.rstrip().endswith(("?", "？")) or "吗" in t)

    counter: dict[str, int] = {}
    for t in texts:
        for f in FILLERS:
            n = t.count(f)
            if n:
                counter[f] = counter.get(f, 0) + n
    top = sorted(counter.items(), key=lambda kv: -kv[1])[:4]

    return {
        "n": len(texts),
        "median_len": float(statistics.median(lens)),
        "p90_len": float(sorted(lens)[min(len(lens) - 1, int(len(lens) * 0.9))]),
        "no_punct_ratio": round(no_punct / n_all, 3),
        "emoji_ratio": round(emoji / n_all, 3),
        "q_ratio": round(qs / n_all, 3),
        "fillers": top,
        "multi_para_ratio": round(sum(1 for t in texts if "\n\n" in t) / n_all, 3),
    }


def render(stats: dict, max_chars: int = 130) -> str:
    if not stats:
        return ""
    ml = stats.get("median_len", 0)
    bits = []
    if ml:
        bits.append(f"中位{ml:.0f}字")
    if stats.get("no_punct_ratio", 0) >= 0.4:
        bits.append("常不带句号")
    if stats.get("multi_para_ratio", 0) >= 0.3:
        bits.append("爱分两段")
    fl = [f for f, _ in (stats.get("fillers") or [])[:3]]
    if fl:
        bits.append("爱用" + "/".join(fl))
    if stats.get("emoji_ratio", 0) >= 0.15:
        bits.append("偶尔带表情")
    if not bits:
        return ""
    head = "【说话的调子】最近这么说话，别端着："
    body = head + "，".join(bits) + "。短句为主，别长篇。"
    return body[:max_chars] if max_chars > 0 and len(body) > max_chars else body


def refresh(window_n: int = 300, bot_ids: set[str] | None = None,
            force: bool = False, every_min: int = 30) -> dict:
    """重算并落库（带节流）。返回统计 dict。"""
    p = ensure()
    if not force:
        with dbutil.ro(p) as con:
            if con is not None:
                try:
                    r = con.execute(
                        "SELECT v, updated_at FROM style_cache WHERE k='stats'").fetchone()
                    if r and dbutil.age_hours(r["updated_at"]) * 60 < every_min:
                        return json.loads(r["v"])
                except (sqlite3.Error, json.JSONDecodeError):
                    pass

    stats = compute(fetch_bot_texts(window_n, bot_ids or set()))
    if not stats:
        return {}
    ts = dbutil.iso()
    with dbutil.tx(p) as con:
        con.execute(
            "INSERT INTO style_stats(ts, window_n, median_len, p90_len, no_punct_ratio,"
            " fillers, emoji_ratio, q_ratio, raw) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, int(stats["n"]), stats["median_len"], stats["p90_len"],
             stats["no_punct_ratio"],
             json.dumps([f for f, _ in stats["fillers"]], ensure_ascii=False),
             stats["emoji_ratio"], stats["q_ratio"],
             json.dumps(stats, ensure_ascii=False)),
        )
        con.execute(
            "INSERT INTO style_cache(k, v, updated_at) VALUES ('stats', ?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (json.dumps(stats, ensure_ascii=False), ts),
        )
    return stats


def cached_stats() -> dict:
    p = db_file()
    if not p.exists():
        return {}
    with dbutil.ro(p) as con:
        if con is None:
            return {}
        try:
            r = con.execute("SELECT v FROM style_cache WHERE k='stats'").fetchone()
            return json.loads(r["v"]) if r else {}
        except (sqlite3.Error, json.JSONDecodeError):
            return {}


def log_feedback(rule: str, before: str, after: str, scope_id: str = "*") -> None:
    p = ensure()
    try:
        with dbutil.tx(p) as con:
            con.execute(
                "INSERT INTO style_feedback(ts, rule, before, after, scope_id) "
                "VALUES (?,?,?,?,?)",
                (dbutil.iso(), rule, before[:400], after[:400], scope_id),
            )
    except sqlite3.Error:
        pass
