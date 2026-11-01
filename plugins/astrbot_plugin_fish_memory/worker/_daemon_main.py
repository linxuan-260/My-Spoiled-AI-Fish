"""把三个 worker 拉进一个进程（线程版，够用且好停）。

用法：
    python worker/_daemon_main.py            # 全部
    python worker/_daemon_main.py ingest     # 只跑某个

生产上更稳的做法是分开跑（一个挂了不影响别的）：
    start_workers.ps1 里就是各起一个进程。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

try:
    import _bootstrap  # type: ignore
except ImportError:  # pragma: no cover
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap  # type: ignore

_bootstrap.add_path()

from astrbot_plugin_fish_memory.config import on  # noqa: E402
from astrbot_plugin_fish_memory.worker import (  # noqa: E402
    daily_digest, ingest_worker, life_daemon,
)


def _loop(name, fn, logger, cfgstore):
    while True:
        try:
            cfg = cfgstore.get()
            if on(cfg, "L3_life", "enabled", default=True) or name != "life":
                fn(logger, cfg)
        except Exception as e:
            logger.exception("%s 异常：%s", name, e)
        time.sleep(60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="?", default="all")
    args = ap.parse_args(argv)

    logger, cfgstore = _bootstrap.boot("daemon_main")
    logger.info("daemon_main 启动 only=%s", args.only)

    jobs = {
        "ingest": (ingest_worker.run_once, 300),
        "life": (life_daemon.one_tick, 1200),
        "digest": (daily_digest.run_once, 3600),
    }
    want = jobs.keys() if args.only == "all" else [args.only]

    threads = []
    for name in want:
        fn, every = jobs[name]
        t = threading.Thread(
            target=_loop, args=(name, fn, logger, cfgstore), daemon=True,
            name=f"fishmem-{name}")
        t.start()
        threads.append(t)
        logger.info("已启动 %s（每 %ss 一跳）", name, every)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("收到中断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
