"""自主探索：自己上网搜点东西看，判断一下，写进生活层。

和「刷视频喂到嘴边」相反：她自己挑问题、自己找页面、自己下判断。
数据流：
    兴趣/最近生活 -> LLM 生成搜索词 -> Anysearch 搜索 -> extract 正文
    -> LLM 用她的语气写一句「看到了什么 + 我怎么看」
    -> life_notes（临时，默认 36 小时）；值得留的再写一条 memory（scope='*'）

配置（L3_life 下）：
    browse_enabled          默认 True
    browse_interval_seconds 默认 7200（两小时一次）
    browse_note_ttl_hours   默认 36
    browse_max_chars        正文截断长度，默认 1200
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from ..config import on
from ..store import lifedb, memorydb
from . import llm

ENDPOINT = "https://api.anysearch.com/mcp"

QUERY_PROMPT = """你是 QQ 机器人肥鱼的脑子。它想自己去网上看点东西，而不是刷别人推给它的短视频。
请出一个它现在会好奇、想自己去查证的问题。

要求：
1. 像人自己好奇的小问题，具体一点，别写宏大议题；
2. 不要和下面「最近查过的」重复；
3. 只输出一行 JSON：{"q": "不超过 18 个字的搜索词"}

最近查过的：
__RECENT__
"""

NOTE_PROMPT = """你在给 QQ 机器人肥鱼写「今天上网看到的东西」。
下面是一段网页内容摘录。请用她的语气写一条记录：看到了什么 + 她自己的判断（信不信、有没有意思）。
要求：
1. 总共不超过 55 个字，口语，别用「综上所述」这类词；
2. 不要照抄原文，不要列点；
3. 输出一行 JSON：{"note": "记录", "keep": true/false}
   keep=true 表示这件事值得长期记住（不是纯八卦/广告/无信息量）。

网页内容摘录：
__PAGE__
"""


def _mcp_call(tool: str, arguments: dict, timeout: int = 25) -> str:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Anysearch 连接失败：{e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"Anysearch 返回非 JSON：{text[:120]}") from e
    if "error" in data:
        raise RuntimeError(f"Anysearch 错误：{data['error']}")
    for item in (data.get("result") or {}).get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")
    return ""


def _json_from(raw: str) -> dict:
    import re
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def _history() -> list[str]:
    try:
        return json.loads(lifedb.state("browse_history", "[]")) or []
    except ValueError:
        return []


def _save_history(items: list[str]) -> None:
    lifedb.set_state("browse_history", json.dumps(items[-8:], ensure_ascii=False))


def maybe_browse(logger, cfg, rng=None) -> dict:
    """到点就探索一次；返回 {"note":..., "query":..., "kept":bool} 或 {}。"""
    if not bool(on(cfg, "L3_life", "browse_enabled", default=True)):
        return {}
    interval = int(on(cfg, "L3_life", "browse_interval_seconds", default=7200) or 7200)
    try:
        last = int(lifedb.state("last_browse", "0") or 0)
    except ValueError:
        last = 0
    now = int(time.time())
    if now - last < interval:
        return {}

    # 无论成功失败都记时间，避免失败后每 20 分钟重试打爆接口
    lifedb.set_state("last_browse", str(now))

    hist = _history()
    try:
        raw = llm.chat(
            [{"role": "system", "content": "你只输出一行 JSON，不要解释。"},
             {"role": "user", "content": QUERY_PROMPT.replace(
                 "__RECENT__", "\n".join(f"- {q}" for q in hist) or "（无）")}],
            cfg, temperature=1.0, max_tokens=60,
        )
        q = str(_json_from(raw or "").get("q") or "").strip()
    except Exception as e:
        logger.warning("自主探索：生成问题失败 %s", e)
        return {}
    if not q or len(q) > 24 or q in hist:
        logger.info("自主探索：问题不合格（%r），本轮跳过", q)
        return {}

    try:
        results = _mcp_call("search", {"query": q, "max_results": 3})
    except Exception as e:
        logger.warning("自主探索：搜索失败（%s）", e)
        return {}

    page = ""
    try:
        import re
        urls = re.findall(r"https?://[^\s\"'<>)\]]+", results or "")
        if urls:
            page = _mcp_call("extract", {"url": urls[0]})
    except Exception as e:
        logger.warning("自主探索：抓正文失败（%s），用搜索结果凑合", e)
    page = (page or results or "").strip()
    limit = int(on(cfg, "L3_life", "browse_max_chars", default=1200) or 1200)
    if not page:
        logger.info("自主探索：没抓到内容，跳过")
        return {}

    try:
        raw = llm.chat(
            [{"role": "system", "content": "你只输出一行 JSON，不要解释。"},
             {"role": "user", "content": NOTE_PROMPT.replace(
                 "__PAGE__", page[:limit])}],
            cfg, temperature=0.9, max_tokens=120,
        )
        j = _json_from(raw or "")
    except Exception as e:
        logger.warning("自主探索：写记录失败 %s", e)
        return {}
    note = str(j.get("note") or "").strip()
    if not note or len(note) > 90:
        logger.info("自主探索：记录不合格，跳过")
        return {}
    note = note[:80]

    ttl = int(on(cfg, "L3_life", "browse_note_ttl_hours", default=36) or 36)
    lifedb.add_note(f"看了点东西：{note}", ttl_hours=ttl)

    kept = False
    if bool(j.get("keep")):
        try:
            memorydb.add(
                "fact", note[:60], category="general", importance=0.3,
                scope_id="*", source="browse",
                meta={"query": q, "source": "anysearch"},
            )
            kept = True
        except Exception as e:
            logger.warning("自主探索：写记忆失败 %s", e)

    hist = (hist + [q])[-8:]
    _save_history(hist)
    logger.info("自主探索：搜了「%s」→ 记录 %d 字 keep=%s",
                q, len(note), "是" if kept else "否")
    return {"query": q, "note": note, "kept": kept}
