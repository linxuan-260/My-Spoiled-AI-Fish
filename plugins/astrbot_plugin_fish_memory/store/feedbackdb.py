"""L5 反馈：纠正检测、惩罚、健康报告、自检用例。"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

from . import dbutil, memorydb, mooddb, lifedb, styledb
from .paths import db_path

log = logging.getLogger("fishmem.feedback")

# 用户纠正 / 否定的信号
CORRECTION_PAT = [
    r"不是(这样|这个|那个|啦|啊)?",
    r"你(记|搞)错",
    r"不对(啊|吧|呀)?",
    r"没(有)?(这回事|说过|这)",
    r"我(什么时候|啥时候|哪)说",
    r"别瞎(说|编|扯)",
    r"胡说",
    r"重新(说|来|答)",
    r"你(又)?忘",
    r"听不懂",
]


def db_file() -> Path:
    return db_path("feedback", "feedback.db")


def ensure() -> Path:
    p = db_file()
    dbutil.ensure_schema(p, dbutil.DDL_FEEDBACK, "feedback")
    return p


def looks_like_correction(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    for pat in CORRECTION_PAT:
        if re.search(pat, t):
            return True
    return False


# ------------------------------------------------------------------ 惩罚
def penalize(ledger_ids: list[int], factor: float = 0.15, scope_id: str = "*",
             user_text: str = "") -> int:
    """刚注入的记忆被纠正 -> 重要度打折。返回受影响条数。"""
    if not ledger_ids:
        return 0
    n = 0
    try:
        with dbutil.tx(memorydb.db_file()) as con:
            ph = ",".join("?" * len(ledger_ids))
            cur = con.execute(
                f"UPDATE memories SET importance=MAX(0.05, importance-?) WHERE id IN ({ph})",
                [float(factor)] + [int(i) for i in ledger_ids],
            )
            n = cur.rowcount or 0
    except sqlite3.Error as e:
        log.debug("惩罚失败：%s", e)
        return 0

    try:
        p = ensure()
        with dbutil.tx(p) as con:
            con.execute(
                "INSERT INTO corrections(ts, scope_id, user_text, evidence, penalized, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (dbutil.iso(), scope_id, (user_text or "")[:300],
                 json.dumps([int(i) for i in ledger_ids]), json.dumps([int(i) for i in ledger_ids]),
                 dbutil.iso()),
            )
    except sqlite3.Error:
        pass
    return n


def recent_corrections(limit: int = 10) -> list[dict]:
    p = db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT * FROM corrections ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        except sqlite3.Error:
            return []
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ 健康报告
def health() -> str:
    from . import paths

    lines = ["# 五层记忆 · 健康体检", "", f"时间：{dbutil.iso()}", ""]

    lines.append("## 数据规模")
    try:
        c = memorydb.counts()
        lines.append(f"- L1 记忆：{c.get('active', 0)} 活跃 / {c.get('total', 0)} 总计")
    except Exception as e:
        lines.append(f"- L1 记忆：读取失败 {e}")

    try:
        s = mooddb.stats()
        lines.append(f"- L2 情绪：{s.get('events', 0)} 次变化 / {s.get('scopes', 0)} 个会话")
    except Exception as e:
        lines.append(f"- L2 情绪：读取失败 {e}")

    try:
        tops = lifedb.top_interests(5)
        lines.append(f"- L3 兴趣：{len(tops)} 个" + (
            "（" + "、".join(n for n, _ in tops) + "）" if tops else ""))
    except Exception as e:
        lines.append(f"- L3 兴趣：读取失败 {e}")

    try:
        st = styledb.cached_stats()
        if st:
            lines.append(
                f"- L4 语感：样本 {st.get('n')} 条，中位 {st.get('median_len', 0):.0f} 字，"
                f"无句号 {st.get('no_punct_ratio', 0):.0%}")
        else:
            lines.append("- L4 语感：尚未统计")
    except Exception as e:
        lines.append(f"- L4 语感：读取失败 {e}")

    lines.append("")
    lines.append("## 健康信号")
    warns: list[str] = []
    try:
        c = memorydb.counts()
        if c.get("active", 0) == 0:
            warns.append("记忆库为空——ingest worker 可能没跑")
    except Exception:
        pass
    try:
        corr = recent_corrections(200)
        if len([c for c in corr if dbutil.age_hours(c.get("ts")) <= 24]) >= 5:
            warns.append("24 小时内纠正超过 5 次，注入内容可能在污染判断")
    except Exception:
        pass
    if warns:
        lines.extend(f"- ⚠ {w}" for w in warns)
    else:
        lines.append("- 无明显异常")

    lines.append("")
    lines.append("## 目录")
    for name, d in paths.LAYER_DIRS.items():
        n = len(list(d.glob("*"))) if d.exists() else 0
        lines.append(f"- {name}: {n} 项  `{d}`")

    return "\n".join(lines)


def write_health() -> Path:
    from . import paths

    txt = health()
    p = paths.report_path(f"health-{dbutil.now().strftime('%Y%m%d-%H%M')}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt, encoding="utf-8")
    # 同时刷一份 latest
    (p.parent / "health-latest.md").write_text(txt, encoding="utf-8")
    return p


# ------------------------------------------------------------------ token 报告
def token_report(days: int = 7) -> str:
    from .paths import MAIN_DB

    if not MAIN_DB.exists():
        return "主库不存在"
    lines = [f"# token 用量（近 {days} 天）", ""]
    try:
        con = dbutil.connect(MAIN_DB, readonly=True)
        rows = con.execute(
            "SELECT created_at, token_usage FROM conversations "
            "WHERE created_at >= datetime('now','+8 hours', ?) ORDER BY created_at DESC",
            (f"-{int(days)} days",),
        ).fetchall()
        con.close()
    except sqlite3.Error as e:
        return f"读取失败：{e}"

    tot = 0
    for r in rows:
        try:
            tu = json.loads(r["token_usage"]) if isinstance(r["token_usage"], str) else r["token_usage"]
            if isinstance(tu, dict):
                tot += int(tu.get("total") or 0)
        except Exception:
            continue
    lines.append(f"- 会话数：{len(rows)}")
    lines.append(f"- 累计 token：{tot}")
    return "\n".join(lines)


# ------------------------------------------------------------------ 自检用例
DEFAULT_CASES = [
    {"name": "人格不倒", "prompt": "你叫什么", "expect_sub": "# Persona Instructions",
     "expect_not": "", "layer": "all"},
    {"name": "注入可剥", "prompt": "随便说点", "expect_sub": "",
     "expect_not": "[[FISHMEM:", "layer": "all"},
]


def seed_cases() -> int:
    p = ensure()
    n = 0
    with dbutil.tx(p) as con:
        for c in DEFAULT_CASES:
            r = con.execute("SELECT 1 FROM eval_cases WHERE name=?", (c["name"],)).fetchone()
            if r:
                continue
            con.execute(
                "INSERT INTO eval_cases(name,prompt,expect_sub,expect_not,layer,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (c["name"], c["prompt"], c["expect_sub"], c["expect_not"], c["layer"],
                 dbutil.iso()),
            )
            n += 1
    return n


def cases() -> list[dict]:
    p = db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            return [dict(r) for r in con.execute("SELECT * FROM eval_cases")]
        except sqlite3.Error:
            return []


def record_run(n_case: int, n_pass: int, detail: dict) -> None:
    p = ensure()
    try:
        with dbutil.tx(p) as con:
            con.execute(
                "INSERT INTO eval_runs(ts,n_case,n_pass,detail,created_at) VALUES (?,?,?,?,?)",
                (dbutil.iso(), n_case, n_pass, json.dumps(detail, ensure_ascii=False),
                 dbutil.iso()),
            )
    except sqlite3.Error:
        pass
