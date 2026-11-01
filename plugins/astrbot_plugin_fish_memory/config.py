"""配置：读 data/config/astrbot_plugin_fish_memory_config.json，按 mtime 热重载。

为什么不用 AstrBot 注入的 config：
    AstrBot 的插件 config 只在加载/重载时刷新，改一次要重载插件甚至重启。
    这里直接盯文件的 mtime，改完下一次请求就生效（毫秒级）。

用法：
    from config import cfgstore
    c = cfgstore.get()          # Node，属性访问
    c.master.enabled            # -> True
    "L1_memory" in c.as_dict()  # 用 as_dict() 做存在性判断
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("fishmem.config")


# ---------------------------------------------------------------- 默认值
DEFAULTS: dict[str, Any] = {
    "master": {
        "enabled": True,
        "shadow_mode": False,
        "enabled_scopes": [],      # []=全开；["aiocqhttp:GroupMessage:xxx"]=白名单
        # 记忆可见性 --------------------------------------------------
        # owner_id：机器人主人的 QQ 号。只有这个人的消息才会被算成「主人名下的记忆」，
        #           这类记忆在 ta 的其它会话里也可见（私聊记得、群里也记得）。
        #           ⚠️ 必填：请填入你自己的 QQ 号，例如 "123456789"
        "owner_id": "",
        # public_scopes：共享层的「来源」。这些会话里记下的事，别的会话也能检索到。
        #   例：["aiocqhttp-<机器人QQ>:GroupMessage:<群号>"]
        # public_readers：共享层的「读者」。留空 = 所有会话都能读来源；
        #   填了就只给这些会话（+来源会话自己）读，避免群里的话漏给陌生人。
        #   例：["aiocqhttp-<机器人QQ>:FriendMessage:<主人QQ>"]
        "public_scopes": [],
        "public_readers": [],
        "budget_chars": 400,
        "budgets": {
            # 单层上限管「别超」，总量由 compose 兜底。实测（11-01 账本三天）：
            # style 恒定 46 字、mood 52~68、life 58~110、memory 49~137。
            # 所以把 style 用不上的额度让给 memory：那里要多装一段「一直记着的」。
            "memory": 168,
            "mood": 72,
            "life": 110,
            "style": 100,
            "total": 400,
        },
        "marker_version": "1",
        "log_injections": True,
    },
    "L1_memory": {
        "enabled": True,
        "k": 3,
        "max_lines": 5,               # 整块最多几行（预算才是硬约束）
        # 同一条记忆这么短时间内不重复进「刚想起来的」，避免反复念同一件事
        "repeat_cooldown_min": 20,
        # 一直记着的那段：不检索，每轮都在
        "core": {
            "enabled": True,
            "max_lines": 2,           # 最多两条，剩下的预算留给检索
        },
        # 沉淀整理：谁升进 core、谁降回去（daily_digest 每晚跑）
        "consolidate": {
            "min_importance": 0.6,
            "strong_importance": 0.7,
            "min_access": 3,
            "max_core": 30,
            "demote_after_days": 120,
        },
        "weights": {
            "semantic": 0.18,
            "keyword": 0.46,
            "graph": 0.11,
            "importance": 0.14,
            "freshness": 0.11,
        },
        "freshness_halflife_days": 30.0,
        "min_score": 0.12,
        "sub": {
            "semantic": False,        # 需要 bge-m3 onnx，未装则自动跳过
            "keyword": True,
            "graph": True,
            "time_decay": True,
            "archive": True,
            "cleanup_180d": True,
            "dedup_merge": True,
            "conflict_detect": True,
            "category_caps": True,
            "multimodal_image": False,
            "multimodal_audio": False,
            "multimodal_video": False,
        },
        "decay_halflife_days": {
            "episode": 14.0,
            "reflection": 90.0,
            "fact": 0.0,      # 0 = 不衰减
            "skill": 0.0,
            "lesson": 0.0,
        },
        "archive_after_days": 90,
        "category_cap": 2,
    },
    "L2_emotion": {
        "enabled": True,
        "step_cap": 0.12,
        "label_cap": 0.60,
        "label_halflife_min": 45.0,
        "mood_halflife_hour": 6.0,
        "comfort_drop": 0.08,
        "tough_mouth_ratio": 0.35,
        "sub": {
            "classify": True,
            "decay": True,
            "comfort": True,
            "tough_mouth": True,
        },
    },
    "L3_life": {
        "enabled": True,
        "daemon_enabled": True,
        "explore_prob": 0.10,
        "llm_activity_prob": 0.35,
        "tick_seconds": 1200,
        # 自主探索：自己上网搜东西看，写进生活层（不是刷别人推的内容）
        "browse_enabled": True,
        "browse_interval_seconds": 7200,
        "browse_note_ttl_hours": 36,
        "browse_max_chars": 1200,
        "sub": {
            "random_activity": True,
            "interest_weight": True,
            "notes_long_to_memory": True,
            "notes_temp_1d": True,
            "mood_bias": True,
        },
    },
    "L4_style": {
        "enabled": True,
        "window_n": 300,
        "max_chars": 130,
        "recompute_every_min": 30,
        "bot_id": "",              # 空=自动从 platform 配置推断
        "sub": {
            "stats": True,
            "sanitize_cs_tone": True,
            "sanitize_drop_period": True,
            "sanitize_merge_paras": True,
            "sanitize_emoji_rhythm": True,
        },
        "max_reply_chars": 0,      # 0=不硬截断（默认关闭，见设计文档）
        "cs_tone": {
            "您": "你",
            "是什么": "是啥",
            "为什么": "为啥",
            "怎么": "咋",
            "非常": "特别",
        },
    },
    "L5_feedback": {
        "enabled": True,
        "penalty": 0.15,
        "sub": {
            "correction": True,
            "daily_health": True,
            "token_report": True,
            "eval_on_change": True,
        },
    },
    "ingest": {
        "enabled": True,
        "batch_size": 12,
        "interval_seconds": 300,
        "max_backlog": 500,
        "provider_source_id": "deepseek_source",
        "provider_id": "deepseek-chat",
        "model": "deepseek-chat",
        "max_chars_per_msg": 240,
        "summarize_kinds": ["episode", "fact"],
        "min_kind_importance": 0.5,
        "heuristic_fallback": True,
        # 主人（master.owner_id）的这些分类会被写成共享层 public；留空=不自动公开
        "auto_share_categories": [],
    },
    "workers": {
        "python": "",              # 空=自动找 qqbot\.venv\Scripts\python.exe
        "life_tick_seconds": 1200,
        "digest_hour": 3,
        "digest_minute": 17,
    },
    "persona_guard": {
        "enabled": True,
        "sha256": "",              # 空=首次运行自动写入当前值
        "check_every_min": 10,
        "auto_restore": False,
    },
    # 发言判断（gate.py）：把 AstrBot 主动回复的「骰子」换成「判断」。
    # 默认闭嘴，只有跟她有关、或者有人问了没人接的时候才主动开口。
    "gate": {
        "enabled": True,
        "quiet_hours": "0-7",          # 深夜不主动开口；跨夜写 23-7
        "cooldown_min": 10,            # 她刚说过话，这么久内不再主动接
        "unanswered_ttl_min": 30,      # 她开口后这么久没人理，就不再自己接话
        "cooldown_speak_min": 60,      # （预留）两次主动开口的最小间隔
        "scene_n": 12,                 # 看最近几条群消息
        "hot_window_sec": 120,         # 「聊得正热」的窗口
        "hot_count": 4,                # 窗口内这么多条 → 插不上嘴
        "allow_question": True,        # 有人问了句没人接的话，可以接
        "self_names": ["肥鱼"],        # 别人提到这些词 = 在说她
        # 听话：主人说这些 = 闭嘴一段时间（私聊下的指令管所有会话）
        "mute_keywords": ["闭嘴", "别说话", "别说了", "安静", "别吵", "别插嘴",
                          "少说两句", "shut up"],
        "unmute_keywords": ["可以说了", "说话吧", "出来说话", "别憋着"],
        "mute_minutes": 30,            # 一次闭嘴多久；24 小时内反复被要求会自动加码
        "hush_keywords": [             # 话题敏感：不插嘴
            "自杀", "去世", "住院", "手术", "癌", "抑郁症", "分手", "离婚",
            "借钱", "身份证", "裸", "政治", "举报", "封号", "隐私", "家里出事",
        ],
        "quarrel_keywords": ["滚", "傻", "闭嘴", "别烦", "骂", "吵", "恶心", "去死", "有病"],
        "serious_keywords": [          # 有人在正经说事
            "作业", "考试", "论文", "面试", "报错", "代码", "答辩", "工资", "合同",
            "简历", "面试题", "考研", "毕业",
        ],
        "log_decisions": True,
        "prune_days": 14,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Node:
    """dict 的属性访问壳，只读。"""

    __slots__ = ("_d",)

    def __init__(self, d: dict | None = None):
        object.__setattr__(self, "_d", d if isinstance(d, dict) else {})

    def __getattr__(self, k: str):
        if k.startswith("_"):
            raise AttributeError(k)
        v = self._d.get(k)
        return Node(v) if isinstance(v, dict) else v

    def __getitem__(self, k):
        return self.__getattr__(k)

    def __contains__(self, k):
        return k in self._d

    def get(self, k, default=None):
        v = self._d.get(k, default)
        return Node(v) if isinstance(v, dict) else v

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def __bool__(self):
        return bool(self._d)

    def __repr__(self):
        return f"Node({list(self._d)})"

    def as_dict(self) -> dict:
        return self._d


class ConfigStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._mtime = -1.0
        self._node = Node(_deep_merge(DEFAULTS, {}))
        self._err: str | None = None

    # ---------------------------------------------------------------- 读
    def reload_if_changed(self) -> bool:
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_mtime == self._mtime:
            return False
        with self._lock:
            if st.st_mtime == self._mtime:
                return False
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("顶层必须是 JSON 对象")
                self._node = Node(_deep_merge(DEFAULTS, raw))
                self._err = None
            except Exception as e:  # 配置写坏了也不能把机器人搞挂
                self._err = f"{type(e).__name__}: {e}"
                log.warning("配置解析失败，继续用上一份好配置：%s", self._err)
            self._mtime = st.st_mtime
        return True

    def get(self) -> Node:
        self.reload_if_changed()
        return self._node

    @property
    def last_error(self) -> str | None:
        return self._err

    # ---------------------------------------------------------------- 写
    def ensure_file(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("已生成默认配置：%s", self.path)

    def write_back(self, patch: dict) -> None:
        """把 patch 合并进文件（保留用户已有键）。用于自动记录 persona sha256 等。"""
        try:
            cur = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
        merged = _deep_merge(cur, patch)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = -1.0  # 强制下次重读
        self.reload_if_changed()


def on(cfg, *path, default=None):
    """cfg.master.enabled 的容错版本：任何一层缺失都返回 default 而不是抛。"""
    cur = cfg
    for k in path:
        if isinstance(cur, Node):
            cur = cur.get(k) if k in cur else None
        elif isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
        if cur is None:
            return default
    return cur


def flag(cfg, layer: str, sub: str, default: bool = True) -> bool:
    """层开关 + 子开关都得开才算开。"""
    if not on(cfg, layer, "enabled", default=True):
        return False
    return bool(on(cfg, layer, "sub", sub, default=default))


def dget(cfg, *path) -> dict:
    """取一个 dict 配置（weights / sub / decay_halflife_days ...）。"""
    v = on(cfg, *path, default=None)
    if isinstance(v, Node):
        return v.as_dict()
    return v if isinstance(v, dict) else {}


def default_path() -> Path:
    try:
        from .store.paths import CONFIG_FILE

        return CONFIG_FILE
    except Exception:
        return Path(__file__).resolve().parents[2] / "config" / (
            "astrbot_plugin_fish_memory_config.json")


# 全局单例：所有模块共用一份，改文件即热重载
cfgstore = ConfigStore(default_path())
