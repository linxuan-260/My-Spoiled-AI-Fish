"""路径解析。插件在 <data>/plugins/<name>/，数据在 <data>/plugin_data/<name>/。"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_fish_memory"

# store/paths.py -> store -> <plugin>
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent
DATA_ROOT = PLUGINS_ROOT.parent

# 允许环境变量覆盖（测试/多实例用）
_env_data = os.environ.get("FISHMEM_DATA_ROOT", "").strip()
if _env_data:
    DATA_ROOT = Path(_env_data).resolve()
    PLUGINS_ROOT = DATA_ROOT / "plugins"
    PLUGIN_ROOT = PLUGINS_ROOT / PLUGIN_NAME

PDATA = DATA_ROOT / "plugin_data" / PLUGIN_NAME
CONFIG_DIR = DATA_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / f"{PLUGIN_NAME}_config.json"
MAIN_DB = DATA_ROOT / "data_v4.db"

LAYER_DIRS = {
    "memory": PDATA / "memory",
    "emotion": PDATA / "emotion",
    "life": PDATA / "life",
    "style": PDATA / "style",
    "feedback": PDATA / "feedback",
    "reports": PDATA / "reports",
    "logs": PDATA / "logs",
    "tmp": PDATA / "tmp",
}


def layer_dir(name: str) -> Path:
    p = LAYER_DIRS.get(name, PDATA / name)
    p.mkdir(parents=True, exist_ok=True)
    return p


def use_root(root: Path, *, plugin_name: str = PLUGIN_NAME) -> None:
    """把数据根换到别处（测试/多实例用）。不改插件源码位置。"""
    global DATA_ROOT, PDATA, CONFIG_DIR, CONFIG_FILE, MAIN_DB, LAYER_DIRS
    root = Path(root).resolve()
    DATA_ROOT = root
    PDATA = root / "plugin_data" / plugin_name
    CONFIG_DIR = root / "config"
    CONFIG_FILE = CONFIG_DIR / f"{plugin_name}_config.json"
    MAIN_DB = root / "data_v4.db"
    LAYER_DIRS = {
        "memory": PDATA / "memory",
        "emotion": PDATA / "emotion",
        "life": PDATA / "life",
        "style": PDATA / "style",
        "feedback": PDATA / "feedback",
        "reports": PDATA / "reports",
        "logs": PDATA / "logs",
        "tmp": PDATA / "tmp",
    }
    for p in LAYER_DIRS.values():
        p.mkdir(parents=True, exist_ok=True)


def db_path(layer: str, filename: str) -> Path:
    return layer_dir(layer) / filename


def ensure_dirs() -> None:
    for p in LAYER_DIRS.values():
        p.mkdir(parents=True, exist_ok=True)


def report_path(stem: str, ext: str = "md") -> Path:
    d = layer_dir("reports")
    return d / f"{stem}.{ext}"


def log_path(stem: str) -> Path:
    d = layer_dir("logs")
    return d / f"{stem}.log"
