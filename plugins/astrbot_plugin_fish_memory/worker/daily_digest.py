"""每日维护：归档旧 episode、清理过期随笔、出健康报告和 token 报告。

用法：
    python worker/daily_digest.py           # 常驻，每晚固定时刻跑一次
    python worker/daily_digest.py --once    # 立刻跑一次
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

try:
    import _bootstrap  # type: ignore
except ImportError:  # pragma: no cover
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap  # type: ignore

_bootstrap.add_path()

from astrbot_plugin_fish_memory.config import dget, on  # noqa: E402
from astrbot_plugin_fish_memory.store import (  # noqa: E402
    dbutil, feedbackdb, lifedb, memorydb,
)


def run_once(logger, cfg) -> dict:
    out: dict = {}

    try:
        out["archived"] = memorydb.archive_old(
            int(on(cfg, "L1_memory", "archive_after_days", default=90) or 90))
    except Exception as e:
        out["archived_error"] = str(e)

    # 沉淀整理：反复想起、够重要的事升到 core（一直记着），太久没想起的降回去
    try:
        c = dget(cfg, "L1_memory", "consolidate")
        out["consolidate"] = memorydb.consolidate(
            min_importance=float(c.get("min_importance") or 0.6),
            strong_importance=float(c.get("strong_importance") or 0.7),
            min_access=int(c.get("min_access") or 3),
            max_core=int(c.get("max_core") or 30),
            demote_after_days=int(c.get("demote_after_days") or 120),
        )
    except Exception as e:
        out["consolidate_error"] = str(e)

    try:
        out["notes_expired"] = lifedb.expire_notes()
    except Exception as e:
        out["notes_error"] = str(e)

    # 发言判断记录只留最近两周（每天几百条，不清理会一直长）
    try:
        from astrbot_plugin_fish_memory.store import gatedb

        out["gate_pruned"] = gatedb.prune(
            int(on(cfg, "gate", "prune_days", default=14) or 14))
    except Exception as e:
        out["gate_prune_error"] = str(e)

    try:
        if on(cfg, "L5_feedback", "sub", "daily_health", default=True):
            out["health"] = str(feedbackdb.write_health())
    except Exception as e:
        out["health_error"] = str(e)

    try:
        if on(cfg, "L5_feedback", "sub", "token_report", default=True):
            from astrbot_plugin_fish_memory.store import paths

            p = paths.report_path("tokens-" + dbutil.now().strftime("%Y%m%d"))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(feedbackdb.token_report(7), encoding="utf-8")
            out["tokens"] = str(p)
    except Exception as e:
        out["tokens_error"] = str(e)

    logger.info("daily_digest 完成：%s", out)
    return out


def _next_run(hour: int, minute: int) -> float:
    now = dt.datetime.now()
    t = now.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return (t - now).total_seconds()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)

    logger, cfgstore = _bootstrap.boot("daily_digest")
    cfg = cfgstore.get()

    if args.once:
        run_once(logger, cfg)
        return 0

    hour = int(on(cfg, "workers", "digest_hour", default=3) or 3)
    minute = int(on(cfg, "workers", "digest_minute", default=17) or 17)
    logger.info("daily_digest 启动，每天 %02d:%02d", hour, minute)

    while True:
        try:
            wait = _next_run(hour, minute)
            logger.info("下一轮 %.1f 分钟后", wait / 60)
            time.sleep(wait)
            run_once(logger, cfgstore.get())
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            logger.exception("digest 异常：%s", e)
            time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
