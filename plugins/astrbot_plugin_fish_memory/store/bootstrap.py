"""启动引导：建目录、建配置、建五个库、记录人格基线。"""

from __future__ import annotations

import logging

from . import feedbackdb, lifedb, memorydb, mooddb, paths, persona_guard, styledb

log = logging.getLogger("fishmem.boot")


def bootstrap(cfg=None, *, write_back=None) -> dict:
    """幂等。插件加载时调一次，worker 启动时也调一次。"""
    out = {"ok": True, "errors": []}

    try:
        paths.ensure_dirs()
    except Exception as e:
        out["errors"].append(f"建目录失败: {e}")

    for name, fn in (
        ("memory", memorydb.ensure),
        ("emotion", mooddb.ensure),
        ("life", lifedb.ensure),
        ("style", styledb.ensure),
        ("feedback", feedbackdb.ensure),
    ):
        try:
            fn()
        except Exception as e:
            out["errors"].append(f"建库失败[{name}]: {e}")

    try:
        feedbackdb.seed_cases()
    except Exception as e:
        out["errors"].append(f"种子用例失败: {e}")

    if cfg is not None:
        try:
            out["persona"] = persona_guard.check(cfg, force=True, write_back=write_back)
        except Exception as e:
            out["errors"].append(f"人格守卫失败: {e}")

    out["ok"] = not out["errors"]
    return out
