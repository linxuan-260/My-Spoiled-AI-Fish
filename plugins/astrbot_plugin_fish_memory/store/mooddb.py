"""L2 情绪：PAD 三维 + 标签向量，全部惰性衰减、有步长上限。"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from pathlib import Path

from . import dbutil
from .paths import db_path

log = logging.getLogger("fishmem.mood")

BASELINE = (0.10, 0.30, 0.10)   # 愉悦 / 唤醒 / 支配
ENERGY0 = 0.60


def db_file() -> Path:
    return db_path("emotion", "emotion.db")


def ensure() -> Path:
    p = db_file()
    dbutil.ensure_schema(p, dbutil.DDL_EMOTION, "emotion")
    return p


# ------------------------------------------------------------------ 词典
# (正则, dP, dA, dD, 标签, 权重)
LEX: list[tuple[str, float, float, float, str, float]] = [
    (r"哈哈|笑死|hhh|233|嘿嘿|乐死", 0.16, 0.10, 0.04, "开心", 0.55),
    (r"谢谢|多谢|感谢|爱你|亲亲|抱抱", 0.14, 0.04, 0.02, "暖心", 0.50),
    (r"可爱|好看|好听|厉害|牛|强啊", 0.12, 0.06, 0.06, "得意", 0.40),
    (r"加油|可以的|没问题|稳了|过了", 0.11, 0.08, 0.09, "振奋", 0.45),
    (r"想你|在吗|陪我|聊会儿|睡了没", 0.06, 0.05, -0.02, "黏人", 0.35),
    (r"烦|气死|操|滚|闭嘴|傻|去死|恶心", -0.20, 0.16, -0.06, "不爽", 0.65),
    (r"哭|难受|委屈|难受死|崩溃|好累|累了", -0.16, 0.04, -0.14, "心疼", 0.60),
    (r"怕|黑|吓|恐怖|不敢", -0.10, 0.18, -0.12, "担心", 0.55),
    (r"无聊|没意思|好闲|发呆", -0.07, -0.10, -0.02, "无聊", 0.35),
    (r"对不起|抱歉|我错|别生气", 0.05, -0.02, 0.03, "软了", 0.40),
    (r"晚安|睡吧|早点睡|休息", 0.06, -0.08, 0.02, "困", 0.30),
    (r"滚蛋|别烦我|不想理你|走开", -0.22, 0.14, -0.10, "被推开", 0.70),
]

_NEG_CTX = re.compile(r"无所谓|随便|算了|不想说|没事|再见|哦$|行吧")

COMPLAINT = re.compile(r"凶我|不高兴|生气|别这样|你态度|好冷淡|不理我|凶了")


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _decay_to(con, scope: str, half_hour: float) -> dict:
    row = con.execute("SELECT * FROM mood_state WHERE scope_id=?", (scope,)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO mood_state(scope_id, pleasure, arousal, dominance, energy, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (scope, BASELINE[0], BASELINE[1], BASELINE[2], ENERGY0, dbutil.iso()),
        )
        return {"p": BASELINE[0], "a": BASELINE[1], "d": BASELINE[2], "e": ENERGY0}

    age_h = dbutil.age_hours(row["updated_at"])
    f = 0.5 ** (age_h / max(0.5, half_hour)) if half_hour > 0 else 1.0
    f = _clamp(f, 0.0, 1.0)
    p = BASELINE[0] + (float(row["pleasure"]) - BASELINE[0]) * f
    a = BASELINE[1] + (float(row["arousal"]) - BASELINE[1]) * f
    d = BASELINE[2] + (float(row["dominance"]) - BASELINE[2]) * f
    e = ENERGY0 + (float(row["energy"]) - ENERGY0) * f
    return {"p": p, "a": a, "d": d, "e": e}


def _decay_labels(con, scope: str, half_min: float) -> dict[str, float]:
    rows = con.execute(
        "SELECT label, value, updated_at FROM mood_labels WHERE scope_id=?", (scope,)
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        age_m = dbutil.age_hours(r["updated_at"]) * 60.0
        f = 0.5 ** (age_m / max(1.0, half_min))
        v = float(r["value"]) * _clamp(f, 0.0, 1.0)
        if v > 0.02:
            out[str(r["label"])] = v
    return out


def _persist(con, scope: str, p: float, a: float, d: float, e: float,
             labels: dict[str, float]) -> None:
    ts = dbutil.iso()
    con.execute(
        "UPDATE mood_state SET pleasure=?, arousal=?, dominance=?, energy=?, updated_at=? "
        "WHERE scope_id=?",
        (p, a, d, e, ts, scope),
    )
    con.execute("DELETE FROM mood_labels WHERE scope_id=?", (scope,))
    for lbl, v in labels.items():
        if v > 0.02:
            con.execute(
                "INSERT INTO mood_labels(scope_id,label,value,updated_at) VALUES (?,?,?,?)",
                (scope, lbl, min(v, 0.99), ts),
            )


def tick(scope: str, half_hour: float, half_min: float) -> dict:
    """只做惰性衰减并落盘，返回当前状态。"""
    p = ensure()
    with dbutil.tx(p) as con:
        cur = _decay_to(con, scope, half_hour)
        labels = _decay_labels(con, scope, half_min)
        _persist(con, scope, cur["p"], cur["a"], cur["d"], cur["e"], labels)
        cur["labels"] = labels
        return cur


def apply_text(scope: str, text: str, *, step_cap: float = 0.12,
               label_cap: float = 0.60, half_hour: float = 6.0,
               half_min: float = 45.0, classify: bool = True) -> dict:
    """按一条用户消息更新情绪。返回 after 状态。"""
    p = ensure()
    with dbutil.tx(p) as con:
        before = _decay_to(con, scope, half_hour)
        labels = _decay_labels(con, scope, half_min)

        dP = dA = dD = 0.0
        matched: list[str] = []
        if classify and text:
            for pat, dp, da, dd, lbl, wt in LEX:
                if re.search(pat, text):
                    hits = len(re.findall(pat, text))
                    scale = min(2.0, 1.0 + 0.4 * (hits - 1)) * wt
                    dP += dp * scale
                    dA += da * scale
                    dD += dd * scale
                    labels[lbl] = min(label_cap + 0.2, labels.get(lbl, 0.0) + 0.30 * wt)
                    matched.append(lbl)
            if _NEG_CTX.search(text) and not matched:
                dP -= 0.04
                dA -= 0.03
                labels["无所谓"] = min(0.5, labels.get("无所谓", 0.0) + 0.18)

        # 过激保护：单步必须收敛
        dP = _clamp(dP, -step_cap, step_cap)
        dA = _clamp(dA, -step_cap, step_cap)
        dD = _clamp(dD, -step_cap, step_cap)

        after = {
            "p": _clamp(before["p"] + dP, -1, 1),
            "a": _clamp(before["a"] + dA, -1, 1),
            "d": _clamp(before["d"] + dD, -1, 1),
        }
        labels = {k: _clamp(v, 0.0, label_cap) for k, v in labels.items() if v > 0.03}

        _persist(con, scope, after["p"], after["a"], after["d"], before["e"], labels)
        con.execute(
            """INSERT INTO mood_events(scope_id, ts, trigger, d_p, d_a, d_d,
                   before_p, before_a, before_d, after_p, after_a, after_d, labels, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (scope, dbutil.iso(), (text or "")[:120], dP, dA, dD,
             before["p"], before["a"], before["d"], after["p"], after["a"], after["d"],
             json.dumps(labels, ensure_ascii=False), ",".join(matched)),
        )
        after["labels"] = labels
        return after


def apply_comfort(scope: str, *, drop: float = 0.08, half_hour: float = 6.0) -> bool:
    """察觉被对方凶了/不开心 -> 情绪下压一档。返回是否触发。"""
    p = ensure()
    with dbutil.tx(p) as con:
        cur = _decay_to(con, scope, half_hour)
        after_p = _clamp(cur["p"] - drop, -1, 1)
        after_d = _clamp(cur["d"] - drop * 0.5, -1, 1)
        _persist(con, scope, after_p, cur["a"], after_d, cur["e"],
                 _decay_labels(con, scope, 45.0))
        con.execute(
            """INSERT INTO mood_events(scope_id, ts, trigger, d_p, d_a, d_d,
                   before_p, before_a, before_d, after_p, after_a, after_d, labels, note)
               VALUES (?,?,?,?,0,?,?,?,?,?,?,?,'{}','comfort')""",
            (scope, dbutil.iso(), "被凶了", -drop, -drop * 0.5,
             cur["p"], cur["a"], cur["d"], after_p, cur["a"], after_d),
        )
        return True


# ------------------------------------------------------------------ 嘴硬
def set_tough(scope: str, active: bool, reason: str = "", hours: float = 4.0) -> None:
    p = ensure()
    until = None
    if active:
        until = dbutil.iso(dbutil.now() + dbutil._dt.timedelta(hours=hours))
    with dbutil.tx(p) as con:
        con.execute(
            "INSERT INTO tough_mouth(scope_id, active, until, reason, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(scope_id) DO UPDATE SET "
            "active=excluded.active, until=excluded.until, reason=excluded.reason, "
            "updated_at=excluded.updated_at",
            (scope, 1 if active else 0, until, reason, dbutil.iso()),
        )


def tough_active(scope: str) -> bool:
    try:
        p = db_file()
        if not p.exists():
            return False
        with dbutil.ro(p) as con:
            if con is None:
                return False
            r = con.execute("SELECT active, until FROM tough_mouth WHERE scope_id=?",
                            (scope,)).fetchone()
            if not r or not r["active"]:
                return False
            u = dbutil.parse_iso(r["until"])
            return bool(u and dbutil.now() < u)
    except sqlite3.Error:
        return False


# ------------------------------------------------------------------ 渲染
def _label_text(labels: dict[str, float], tough: bool, max_labels: int = 2) -> str:
    items = sorted(labels.items(), key=lambda kv: -kv[1])[:max_labels]
    s = " ".join(f"{k}{v:.2f}" for k, v in items)
    if tough:
        s += "（嘴硬中）"
    return s


def render(scope: str, *, max_chars: int = 72) -> str:
    """拼情绪注入块正文。读库失败一律返回空串（宁可不注入）。"""
    try:
        cur = tick(scope, 6.0, 45.0)
    except Exception as e:
        log.debug("情绪层读取失败：%s", e)
        return ""

    tough = tough_active(scope)
    head = (f"【现在的心情】愉悦{cur['p']:.2f} 唤醒{cur['a']:.2f} "
            f"支配{cur['d']:.2f}")
    lt = _label_text(cur.get("labels") or {}, tough)
    if lt:
        head += "；" + lt
    tail = "情绪只影响语气，不影响判断；不能拿情绪拿捏对方。"
    body = head + "\n" + tail
    return body[:max_chars] if max_chars > 0 and len(body) > max_chars else body


def stats() -> dict:
    p = db_file()
    if not p.exists():
        return {}
    with dbutil.ro(p) as con:
        if con is None:
            return {}
        try:
            n = con.execute("SELECT count(*) FROM mood_events").fetchone()[0]
            s = con.execute("SELECT count(DISTINCT scope_id) FROM mood_state").fetchone()[0]
            return {"events": n, "scopes": s}
        except sqlite3.Error:
            return {}
