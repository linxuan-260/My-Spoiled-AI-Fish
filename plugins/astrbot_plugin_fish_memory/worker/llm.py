"""worker 的 LLM 客户端：直接从 cmd_config.json 读 provider，不依赖 astrbot。

为什么要这样：
    worker 是独立进程，拿不到 AstrBot 的 Context，也不想为了调一次 LLM 把
    整个 bot 框架拉起来。cmd_config.json 里 provider_sources 有 key + api_base，
    provider 有 model —— 够用了。

注意：cmd_config.json 带 BOM，必须用 utf-8-sig 读。
"""

from __future__ import annotations

import json
import logging
import time

log = logging.getLogger("fishmem.llm")


def _cfg_on(cfg, *path, default=None):
    try:
        from astrbot_plugin_fish_memory.config import on

        return on(cfg, *path, default=default)
    except Exception:
        return default


def load_provider(cfg=None, provider_id: str = "") -> dict | None:
    """返回 {api_base, api_key, model, timeout}，找不到返回 None。"""
    from astrbot_plugin_fish_memory.store.paths import DATA_ROOT

    conf_path = DATA_ROOT / "cmd_config.json"
    if not conf_path.exists():
        log.warning("找不到 cmd_config.json：%s", conf_path)
        return None
    try:
        j = json.loads(conf_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        log.warning("读 cmd_config.json 失败：%s", e)
        return None

    want = (provider_id or str(_cfg_on(cfg, "ingest", "provider_id", default="") or "")).strip()
    want_model = str(_cfg_on(cfg, "ingest", "model", default="") or "").strip()

    sources = {s.get("id"): s for s in (j.get("provider_sources") or [])}
    instances = [p for p in (j.get("provider") or []) if isinstance(p, dict)]

    best = None
    if want:
        for p in instances:
            if p.get("id") == want:
                best = p
                break
    if best is None and want_model:
        for p in instances:
            if p.get("enable", True) and want_model in (p.get("id"), p.get("model")):
                best = p
                break
    if best is None:
        # 注意：cmd_config 里 chat 类实例的 provider_type 可能是 None
        # （deepseek-chat 就是 None，而 glm-4v-flash 是 chat_completion），
        # 只认 "chat_completion" 会跳过正经的文本模型、抓到视觉模型上去。
        for p in instances:
            if p.get("provider_type") in (None, "", "chat_completion") and p.get("enable", True):
                best = p
                break
    if best is None:
        return None

    src = sources.get(best.get("provider_source_id")) or {}
    api_base = str(src.get("api_base") or best.get("api_base") or "").strip()
    api_key = src.get("key") or best.get("key") or best.get("api_key") or ""
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else ""
    model = str(best.get("model") or "").strip()
    if not api_base or not api_key or not model:
        return None
    return {
        "id": best.get("id") or "",
        "api_base": api_base.rstrip("/"),
        "api_key": str(api_key),
        "model": model,
        "timeout": float(src.get("timeout") or best.get("timeout") or 120),
    }


def chat(messages: list[dict], cfg=None, *, model: str = "", max_tokens: int = 900,
         temperature: float = 0.2) -> str | None:
    """调一次 chat completion，失败返回 None（绝不抛）。"""
    p = load_provider(cfg)
    if not p:
        log.warning("没找到可用的 chat provider，跳过 LLM 调用")
        return None
    if model:
        p["model"] = model

    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai 未安装，跳过 LLM 调用")
        return None

    t0 = time.time()
    try:
        cli = OpenAI(api_key=p["api_key"], base_url=p["api_base"],
                     timeout=p["timeout"], max_retries=2)
        r = cli.chat.completions.create(
            model=p["model"], messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        txt = (r.choices[0].message.content or "").strip()
        log.info("LLM ok model=%s %.1fs -> %d 字", p["model"], time.time() - t0, len(txt))
        return txt or None
    except Exception as e:
        log.warning("LLM 调用失败 %.1fs：%s", time.time() - t0, e)
        return None
