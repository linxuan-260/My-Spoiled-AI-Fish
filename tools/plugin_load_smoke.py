# -*- coding: utf-8 -*-
"""离线复现 AstrBot 的插件加载路径，验证 fish_memory 能真正实例化。

复现的三步（与 star_manager.load 一致）：
  1. import data.plugins.astrbot_plugin_fish_memory.main
  2. 有 _conf_schema.json 才构造 AstrBotConfig —— 我们已删掉，跳过
  3. 实例化 Star
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import traceback

ROOT = r"C:\Users\27874\Documents\deepseek-harness\default-workspace\qqbot"
PLUGIN_DIR = os.path.join(ROOT, "data", "plugins", "astrbot_plugin_fish_memory")

sys.path.insert(0, ROOT)

print("=== 1. 目录自检 ===")
print("  main.py        :", os.path.exists(os.path.join(PLUGIN_DIR, "main.py")))
print("  metadata.yaml  :", os.path.exists(os.path.join(PLUGIN_DIR, "metadata.yaml")))
print("  requirements   :", os.path.exists(os.path.join(PLUGIN_DIR, "requirements.txt")))
print("  _conf_schema   :", os.path.exists(os.path.join(PLUGIN_DIR, "_conf_schema.json")),
      "  <- 必须 False，否则 AstrBotConfig 会改写 data/config/astrbot_plugin_fish_memory_config.json")

print("\n=== 2. import ===")
try:
    mod = importlib.import_module("data.plugins.astrbot_plugin_fish_memory.main")
    print("  ok ->", mod.__name__)
except Exception:
    traceback.print_exc()
    raise SystemExit(1)

print("\n=== 3. 找到 Star 子类 ===")
from astrbot.api.star import Star  # noqa: E402

classes = [
    (n, o) for n, o in vars(mod).items()
    if isinstance(o, type) and issubclass(o, Star) and o is not Star
]
for n, o in classes:
    print(f"  {n}  module={o.__module__}")
if not classes:
    print("  !! 没有 Star 子类")
    raise SystemExit(1)


class DummyContext:
    """Star.__init__ 只会把它存起来；插件代码不依赖 context 的方法。"""

    def __init__(self):
        self.plugins = []


print("\n=== 4. 实例化 + on_load ===")
name, cls = classes[0]
try:
    inst = cls(DummyContext())
    print("  实例化 ok")
except Exception:
    traceback.print_exc()
    raise SystemExit(1)

hooks = [
    n for n in dir(inst)
    if any(n.startswith(p) for p in ("on_llm_request", "on_decorating_result"))
]
print("  钩子方法:", hooks)

if hasattr(inst, "on_load"):
    try:
        asyncio.run(inst.on_load())
        print("  on_load ok")
    except Exception:
        traceback.print_exc()
        print("  !! on_load 抛异常（AstrBot 会把插件标记为加载失败）")
        raise SystemExit(1)
else:
    print("  （未定义 on_load，跳过）")

print("\n=== 5. AstrBot 的发现逻辑 _get_modules 是否能看到它 ===")
sys.path.insert(0, os.path.join(ROOT))
try:
    from astrbot.core.star.star_manager import PluginManager  # noqa: E402

    found = PluginManager._get_modules(os.path.join(ROOT, "data", "plugins"))
    hit = [m for m in found if m["pname"] == "astrbot_plugin_fish_memory"]
    print("  发现:", hit if hit else "!! 未发现")
except Exception:
    traceback.print_exc()

print("\nALL GOOD")
