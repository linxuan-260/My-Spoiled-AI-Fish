"""worker 进程启动引导（被脚本直接运行时用）。

把 data/plugins 加进 sys.path，然后就能 import astrbot_plugin_fish_memory.*。
worker 全部不依赖 astrbot，只依赖标准库 + 可选 openai。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent


def add_path() -> None:
    p = str(PLUGINS_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


def setup_logging(name: str) -> logging.Logger:
    add_path()
    from astrbot_plugin_fish_memory.store import paths

    paths.ensure_dirs()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = RotatingFileHandler(
        paths.log_path(name), maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(stream=sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    if os.environ.get("FISHMEM_DEBUG"):
        logger.setLevel(logging.DEBUG)
    return logger


def boot(name: str):
    """返回 (logger, cfgstore)。顺带把五个库准备好。"""
    logger = setup_logging(name)
    from astrbot_plugin_fish_memory.config import cfgstore
    from astrbot_plugin_fish_memory.store import bootstrap

    cfgstore.ensure_file()
    r = bootstrap.bootstrap(cfgstore.get(), write_back=cfgstore.write_back)
    if r.get("errors"):
        logger.warning("bootstrap 有告警：%s", r["errors"])
    return logger, cfgstore
