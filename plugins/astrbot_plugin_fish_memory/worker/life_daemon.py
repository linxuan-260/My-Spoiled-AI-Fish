"""生活层 daemon：周期性产生一点"这边发生过的事"。

用法：
    python worker/life_daemon.py            # 常驻
    python worker/life_daemon.py --once     # 只跳一拍
"""

from __future__ import annotations

import argparse
import random
import sys
import time

try:
    import _bootstrap  # type: ignore
except ImportError:  # pragma: no cover - 模块方式运行时的兜底
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap  # type: ignore

_bootstrap.add_path()

from astrbot_plugin_fish_memory.config import on  # noqa: E402
from astrbot_plugin_fish_memory.store import lifedb, mooddb  # noqa: E402

NOTE_POOL = [
    "今天有点闷，不想多说",
    "想起来昨天那事，还是有点在意",
    "水喝少了，嘴干",
    "想买个小东西，还没下单",
    "窗外在下雨",
    "把歌单换了一遍",
    "差点忘回一条消息",
    "手边那包零食吃完了",
    "有点困，眼皮打架",
    "脖子有点酸，坐太久了",
    "手机快没电了，充电器找不到",
    "刚拖完地，屋里有点凉",
    "今天没什么想说的",
    "想出去走走又犯懒",
    "桌角的杯子空了",
    "天阴下来了",
    "把耳机摘了，世界安静了一会儿",
    "想起来有个事没做完",
    "猫把桌上的东西扒拉到地上了",
    "有点饿，但不想动",
    "刚洗完澡，不想动",
    "明天要早起，还不想睡",
    "手指被纸划了一下",
    "风扇声有点吵",
    "白饭蒸多了，剩一半",
    "刚把窗户打开，风挺大",
]

ACTIVITY_PROMPT = """你在给一个 QQ 机器人朋友写「此刻正在做的事」。
它叫肥鱼，住在手机里，日常就是吃饭、上网、发呆、收拾屋子这类小事。

要求：
1. 写一件具体、琐碎、像真事的小活动，别写宏大的事；
2. 不要和下面「最近做过的」重复（同类或同一件事都不行）；
3. 只输出一行 JSON，形如：{"kind": "短语", "detail": "一句具体的话"}
4. kind 不超过 6 个字，detail 不超过 14 个字，不要引号、不要标点堆叠。

最近做过的：
__RECENT__
"""


def _llm_activity(logger, cfg, recent: list[tuple[str, str]]):
    """让模型编一条新活动；失败/不合格就返回 None，由调用方回落到池子。"""
    try:
        from astrbot_plugin_fish_memory.worker import llm

        recent_lines = "\n".join(f"- {k}：{d}" for k, d in recent[:10]) or "（无）"
        user_msg = ACTIVITY_PROMPT.replace("__RECENT__", recent_lines)
        raw = llm.chat(
            [{"role": "system", "content": "你只输出一行 JSON，不要解释。"},
             {"role": "user", "content": user_msg}],
            cfg, temperature=1.1, max_tokens=80,
        )
        if not raw:
            return None
        import json
        import re
        txt = raw.strip().strip("`")
        txt = re.sub(r"^json\s*", "", txt, flags=re.I)
        j = None
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                j = json.loads(m.group(0))
            except ValueError:
                j = None
        if not isinstance(j, dict):
            # 兜底：直接从文本里抠 kind/detail
            j = {}
            for key in ("kind", "detail"):
                mm = re.search(key + r'["\']?\s*[:：]\s*["\']([^"\'\n]+)', txt, re.I)
                if mm:
                    j[key] = mm.group(1)
        # 有的模型会把 key 也带上引号
        j = {str(k).strip('"\''): v for k, v in j.items()}
        kind = str(j.get("kind") or "").strip().strip('"\'')
        detail = str(j.get("detail") or "").strip().strip('"\'')
        if not kind or not detail:
            logger.warning("LLM 活动不合格，原文：%r", (raw or "")[:80])
            return None
        if len(kind) > 6 or len(detail) > 14:
            return None
        if any(c in (kind + detail) for c in "\n\r"):
            return None
        if detail in {d for _, d in recent}:
            return None
        return kind, detail
    except Exception as e:
        logger.warning("LLM 活动生成失败：%s", e)
        return None


def one_tick(logger, cfg, rng: random.Random | None = None) -> dict:
    rng = rng or random.Random()
    explore = float(on(cfg, "L3_life", "explore_prob", default=0.10) or 0)

    mood_p, mood_a = 0.1, 0.3
    try:
        st = mooddb.tick("_life", 6.0, 45.0)
        mood_p, mood_a = st["p"], st["a"]
    except Exception:
        pass

    bias = bool(on(cfg, "L3_life", "sub", "mood_bias", default=True))
    recent = lifedb.recent_activities(8)
    llm_prob = float(on(cfg, "L3_life", "llm_activity_prob", default=0.35) or 0)

    kind = detail = ""
    if llm_prob > 0 and rng.random() < llm_prob:
        got = _llm_activity(logger, cfg, recent)
        if got:
            kind, detail = got
    if not kind:
        kind, detail = lifedb.pick_activity(
            mood_p=mood_p if bias else 0.1,
            mood_a=mood_a if bias else 0.3,
            explore_prob=explore, recent=recent, rng=rng,
        )
    lifedb.add_activity(kind, detail, detail, curious=1 if "新学到" in detail else 0,
                        mood=(mood_p, mood_a))

    # 兴趣自然衰减：长期没被提到就淡出
    tops = lifedb.top_interests(3)
    for name, w in tops:
        if w > 0.12 and rng.random() < 0.15:
            lifedb.touch_interest(name, delta=-0.02)

    # 偶尔写一条随笔（临时，1 天内有效）
    wrote_note = ""
    if bool(on(cfg, "L3_life", "sub", "notes_temp_1d", default=True)) and rng.random() < 0.35:
        wrote_note = rng.choice(NOTE_POOL)
        lifedb.add_note(wrote_note, ttl_hours=24)

    # 自己上网看点东西（默认两小时一次）
    browsed = {}
    try:
        from astrbot_plugin_fish_memory.worker import browser

        browsed = browser.maybe_browse(logger, cfg)
        if browsed.get("query"):
            lifedb.add_activity(
                "上网翻东西", f"搜了「{browsed['query']}」",
                f"搜了「{browsed['query']}」", curious=1, mood=(mood_p, mood_a))
    except Exception as e:
        logger.warning("自主探索异常：%s", e)

    lifedb.set_state("last_tick", str(int(time.time())))
    logger.info("tick 活动=%s/%s 心情=(%.2f,%.2f) 随笔=%s 探索=%s",
                kind, detail, mood_p, mood_a, wrote_note or "-",
                browsed.get("query") or "-")
    return {"kind": kind, "detail": detail, "note": wrote_note,
            "browse": browsed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=0)
    args = ap.parse_args(argv)

    logger, cfgstore = _bootstrap.boot("life_daemon")
    cfg = cfgstore.get()
    interval = args.interval or int(
        on(cfg, "workers", "life_tick_seconds", default=1200) or 1200)

    if not on(cfg, "L3_life", "daemon_enabled", default=True):
        logger.info("L3 daemon 在配置里关着，退出")
        return 0

    logger.info("life_daemon 启动 interval=%ss", interval)
    while True:
        try:
            cfg = cfgstore.get()
            if not on(cfg, "L3_life", "enabled", default=True):
                logger.info("L3 已关闭，休息一轮")
            else:
                one_tick(logger, cfg)
            if args.once:
                return 0
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            logger.exception("tick 异常：%s", e)
            if args.once:
                return 1
        try:
            time.sleep(max(30, interval))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
