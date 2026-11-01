"""L3 生活层：兴趣、活动、临时随笔。"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from pathlib import Path

from . import dbutil
from .paths import db_path

log = logging.getLogger("fishmem.life")


def db_file() -> Path:
    return db_path("life", "life.db")


def ensure() -> Path:
    p = db_file()
    dbutil.ensure_schema(p, dbutil.DDL_LIFE, "life")
    return p


# ------------------------------------------------------------------ 兴趣
def touch_interest(name: str, *, delta: float = 0.05, source: str = "chat") -> None:
    name = (name or "").strip()
    if not name or len(name) > 24:
        return
    p = ensure()
    with dbutil.tx(p) as con:
        con.execute(
            """INSERT INTO interests(name, weight, source, last_touched, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET
                 weight=MIN(1.0, weight+?),
                 last_touched=excluded.last_touched""",
            (name, min(1.0, 0.5 + delta), source, dbutil.iso(), dbutil.iso(), delta),
        )


def top_interests(n: int = 5) -> list[tuple[str, float]]:
    p = db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            return [(r[0], float(r[1])) for r in con.execute(
                "SELECT name, weight FROM interests ORDER BY weight DESC, last_touched DESC LIMIT ?",
                (int(n),))]
        except sqlite3.Error:
            return []


# ------------------------------------------------------------------ 活动
# 每个 (kind, detail)：kind 短（会进 prompt），detail 是具体一句。
# 写得像「一个住在手机里、每天吃白饭的 QQ 机器人朋友」会干的小事。
ACTIVITY_POOL = [
    # 看天 / 放空
    ("看云", "阳台站了一会儿"),
    ("看云", "云被风吹散了"),
    ("看云", "傍晚的天是橘子色"),
    ("看云", "云压得低低的"),
    ("发呆", "盯着天花板想事"),
    ("发呆", "对着屏幕放空"),
    ("发呆", "坐着没动，脑子在飘"),
    ("打瞌睡", "趴了一会儿"),
    ("打瞌睡", "差点睡过去"),
    # 吃喝
    ("喝甜的", "点了杯冰的"),
    ("喝甜的", "泡了杯热的"),
    ("喝水", "倒了杯温水"),
    ("喝水", "灌了一大杯凉的"),
    ("吃零食", "拆了包薯片"),
    ("吃零食", "啃了个苹果"),
    ("蒸白饭", "米淘了两遍"),
    ("蒸白饭", "蒸了一锅，香"),
    ("研究菜谱", "想做但没做"),
    ("研究菜谱", "看了个红烧肉教程"),
    ("研究菜谱", "收藏了三道菜"),
    # 玩乐
    ("听歌", "单曲循环一首老歌"),
    ("听歌", "随机到一首没听过的"),
    ("听歌", "歌单又循环到第三遍"),
    ("打游戏", "输了两把"),
    ("打游戏", "赢了一把"),
    ("打游戏", "排位卡住了"),
    ("看小说", "追了两章"),
    ("看小说", "弃了一本"),
    ("看电影", "看了半部"),
    ("看电影", "跳着看完了"),
    ("刷短视频", "刷了会儿就退出来"),
    ("刷到冷知识", "记住了"),
    ("刷到冷知识", "顺手存下来了"),
    ("刷到冷知识", "看完觉得没用但有趣"),
    # 家务
    ("收拾桌面", "把线理了一遍"),
    ("收拾桌面", "把杯子洗了"),
    ("整理相册", "删了一堆截图"),
    ("整理相册", "翻到去年的照片"),
    ("拖地", "把角落擦了"),
    ("刷碗", "洗了一池子"),
    ("洗衣服", "晾了一排"),
    ("浇花", "给绿萝浇了水"),
    ("浇花", "把叶子擦了一遍"),
    # 杂事
    ("拆快递", "到了个小玩意儿"),
    ("拆快递", "盒子比想的小"),
    ("回消息", "攒了一堆没回"),
    ("回消息", "一条条回完了"),
    ("找东西", "翻了半天没找到"),
    ("找东西", "在抽屉底下找到"),
    ("做计划", "列了个清单"),
    ("做计划", "把明天的排了一遍"),
    ("算账", "算了算这个月花的"),
    ("修东西", "把松了的螺丝拧紧"),
    ("修东西", "折腾半天没修好"),
    ("写东西", "记了两行"),
    ("写东西", "写了半页又删了"),
]


def recent_activities(limit: int = 8) -> list[tuple[str, str]]:
    """最近若干条的 (kind, detail)，用来防重复。"""
    p = db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT kind, detail FROM activities ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        except sqlite3.Error:
            return []
    return [(str(r[0]), str(r[1] or "")) for r in rows]


def pick_activity(mood_p: float = 0.1, mood_a: float = 0.3,
                  explore_prob: float = 0.10,
                  recent: list[tuple[str, str]] | None = None,
                  rng: random.Random | None = None) -> tuple[str, str]:
    rng = rng or random
    pool = ACTIVITY_POOL
    n = len(pool)

    if rng.random() < explore_prob:
        kind, detail = pool[rng.randrange(n)]
        return kind, detail + "（新学到的）"

    # 情绪偏低 -> 偏静态
    if mood_p < -0.15:
        idx = [i for i, (k, _) in enumerate(pool) if k in
               ("发呆", "看云", "听歌", "写东西", "打瞌睡")] or list(range(n))
    elif mood_p > 0.35:
        idx = [i for i, (k, _) in enumerate(pool) if k in
               ("拆快递", "喝甜的", "打游戏", "吃零食", "看电影")] or list(range(n))
    else:
        idx = list(range(n))

    # 防重复：最近 8 条里出现过的 kind 和完全相同的 detail 都排除
    recent = recent or []
    recent_kind = {k for k, _ in recent}
    recent_detail = {d for _, d in recent}
    fresh = [i for i in idx
             if pool[i][0] not in recent_kind and pool[i][1] not in recent_detail]
    if not fresh:
        fresh = [i for i in idx if pool[i][1] not in recent_detail]
    if fresh:
        idx = fresh

    i = rng.choice(idx) if idx else 0
    return pool[i]


def add_activity(kind: str, topic: str, detail: str = "", curious: int = 0,
                 mood: tuple[float, float] | None = None) -> int:
    p = ensure()
    ts = dbutil.iso()
    with dbutil.tx(p) as con:
        cur = con.execute(
            "INSERT INTO activities(ts, kind, topic, detail, curious, mood_p, mood_a, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (ts, kind, topic, detail, 1 if curious else 0,
             mood[0] if mood else None, mood[1] if mood else None, ts),
        )
        return int(cur.lastrowid)


def latest_activity() -> dict | None:
    p = db_file()
    if not p.exists():
        return None
    with dbutil.ro(p) as con:
        if con is None:
            return None
        try:
            r = con.execute(
                "SELECT * FROM activities ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            return None
        return dict(r) if r else None


# ------------------------------------------------------------------ 随笔
def add_note(note: str, ttl_hours: int = 24) -> int:
    p = ensure()
    ts = dbutil.iso()
    with dbutil.tx(p) as con:
        cur = con.execute(
            "INSERT INTO life_notes(ts, note, ttl_hours, created_at) VALUES (?,?,?,?)",
            (ts, note, int(ttl_hours), ts),
        )
        return int(cur.lastrowid)


def live_notes(limit: int = 3) -> list[str]:
    p = db_file()
    if not p.exists():
        return []
    with dbutil.ro(p) as con:
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT note, ts, ttl_hours FROM life_notes "
                "WHERE promoted=0 ORDER BY id DESC LIMIT 20").fetchall()
        except sqlite3.Error:
            return []
    out = []
    for r in rows:
        if dbutil.age_hours(r["ts"]) <= float(r["ttl_hours"]):
            out.append(str(r["note"]))
            if len(out) >= limit:
                break
    return out


def expire_notes() -> int:
    p = db_file()
    if not p.exists():
        return 0
    with dbutil.tx(p) as con:
        cur = con.execute(
            "DELETE FROM life_notes WHERE promoted=0 AND "
            "julianday(ts) < julianday('now') - ttl_hours / 24.0")
        return cur.rowcount or 0


# ------------------------------------------------------------------ daemon 状态
def state(key: str, default: str = "") -> str:
    p = db_file()
    if not p.exists():
        return default
    with dbutil.ro(p) as con:
        if con is None:
            return default
        try:
            r = con.execute("SELECT v FROM daemon_state WHERE k=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
    return r[0] if r else default


def set_state(key: str, value: str) -> None:
    p = ensure()
    with dbutil.tx(p) as con:
        con.execute(
            "INSERT INTO daemon_state(k, v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, str(value)),
        )


# ------------------------------------------------------------------ 渲染
def render(max_chars: int = 60) -> str:
    """拼生活层正文。读不到就返回空。"""
    try:
        act = latest_activity()
        notes = live_notes(2)
    except Exception as e:
        log.debug("生活层读取失败：%s", e)
        return ""
    lines = []
    if act:
        age_h = dbutil.age_hours(act.get("ts"))
        if age_h <= 12:
            lines.append(f"· 刚才{act['kind']}：{act['detail'] or act['topic']}")
    for n in notes:
        lines.append(f"· {n}")
    if not lines:
        return ""
    head = "【这边的事】不是设定，是这一轮的背景，随口提一句就行。"
    body = head + "\n" + "\n".join(lines)
    return body[:max_chars] if max_chars > 0 and len(body) > max_chars else body
