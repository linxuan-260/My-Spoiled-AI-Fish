"""对话提炼 worker：每够 batch_size 条未消费消息，提炼成记忆。

用法：
    python -m worker.ingest_worker            # 常驻
    python  worker/ingest_worker.py --once    # 跑一轮就退出（测试用）
    python  worker/ingest_worker.py --force   # 不等攒够，有多少处理多少
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time

import _bootstrap  # type: ignore  # noqa: F401  (同目录脚本运行时)

_bootstrap.add_path()

from astrbot_plugin_fish_memory.config import on  # noqa: E402
from astrbot_plugin_fish_memory.store import (  # noqa: E402
    ingest, lifedb, memorydb,
)
from astrbot_plugin_fish_memory.worker import llm  # noqa: E402

SYSTEM_PROMPT = """你在为一个长期记忆系统做提炼。输入是一段 QQ 群聊记录。
只提取「值得长期记住」的信息，输出 JSON 数组，不要任何解释文字。

每条记忆的字段：
  kind       episode（发生过的事）/ fact（稳定属性）/ lesson（教训）/ skill（技能）/ reflection（想法）
  text       一句话，中文，不超过 40 字，用第三人称描述（"爹"指机器人主人）
  importance 0.0~1.0，越长期有用越高
  category   profile / preference / life / study / work / social / health / general

规则：
- 寒暄、重复、无信息量的内容不要。
- 不要编造。原文没有的不要写。
- 用户明确表达偏好、习惯、经历、情绪触发点时才记。
- 最多 6 条，没有值得记的就输出 []。"""


def _fmt_batch(rows: list[dict], max_chars: int = 240) -> str:
    lines = []
    for r in rows:
        who = "对方" if r.get("role") == "user" else "肥鱼"
        name = (r.get("sender_name") or "").strip()
        if who == "对方" and name:
            who = f"{name}"
        t = (r.get("text") or "").strip().replace("\n", " ")
        if len(t) > max_chars:
            t = t[:max_chars] + "…"
        lines.append(f"[{who}] {t}")
    return "\n".join(lines)


_JSON_ARR = re.compile(r"\[.*\]", re.S)

# 模型明确回答「没什么值得记的」——这是判断，不是故障，不能触发兜底
_EMPTY_ANSWER = re.compile(r"^\s*```(?:json)?\s*\[\s*\]\s*```\s*$|^\s*\[\s*\]\s*$", re.S)


def parse_memories(raw: str) -> list[dict]:
    if not raw:
        return []
    m = _JSON_ARR.search(raw)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in arr if isinstance(arr, list) else []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        if not text or len(text) > 120:
            continue
        kind = str(it.get("kind") or "fact")
        if kind not in ("episode", "fact", "lesson", "skill", "reflection"):
            kind = "fact"
        try:
            imp = float(it.get("importance") or 0.5)
        except (TypeError, ValueError):
            imp = 0.5
        out.append({
            "kind": kind,
            "text": text,
            "importance": max(0.05, min(1.0, imp)),
            "category": str(it.get("category") or "general")[:16],
        })
    return out


def heuristic(rows: list[dict]) -> list[dict]:
    """LLM 不可用时的兜底：只把够长的用户消息存成 episode，宁可少不要错。"""
    out = []
    seen = set()
    for r in rows:
        if r.get("role") != "user":
            continue
        t = (r.get("text") or "").strip()
        if len(t) < 8 or len(t) > 80:
            continue
        if t in seen or t.startswith(("[", "http")):
            continue
        seen.add(t)
        out.append({"kind": "episode", "text": t, "importance": 0.35,
                    "category": "general"})
        if len(out) >= 3:
            break
    return out


def _owner_of_batch(rows: list[dict]) -> str:
    """一批里只有一个人的消息时才敢把记忆算在某人名下。"""
    owners = {str(r.get("sender_id") or "").strip() for r in rows}
    owners.discard("")
    return owners.pop() if len(owners) == 1 else ""


def _visibility_for(cfg, scope: str, owner_id: str, category: str) -> str:
    """public=共享层（所有会话可见）；owner=同一个人跨会话；private=只在本会话。

    规则（从强到弱）：
      1. 说话人 = master.owner_id 且分类命中
         ingest.auto_share_categories          → public
      2. 这批只有一个说话人                   → owner
      3. 其他                                 → private
    """
    owner_cfg = str(on(cfg, "master", "owner_id", default="") or "")
    share_cats = [str(c) for c in (on(cfg, "ingest", "auto_share_categories",
                                       default=[]) or [])]
    if owner_id and owner_cfg and owner_id == owner_cfg and category in share_cats:
        return "public"
    if owner_id:
        return "owner"
    return "private"


def _process_group(logger, cfg, rows: list[dict]) -> dict:
    conv = _fmt_batch(rows)
    prompt = [{"role": "system", "content": SYSTEM_PROMPT},
              {"role": "user", "content": conv}]
    raw = llm.chat(prompt, cfg, temperature=0.1, max_tokens=800)
    memories = parse_memories(raw or "")

    # 三种情况必须分开：
    #   raw is None        → 调用失败，兜底才有意义
    #   raw 是 []          → 模型判断「这批没什么可记」，尊重它，别兜底塞垃圾
    #   raw 有内容却解析不出 → 格式跑偏，兜底
    used = "llm"
    fallback = bool(on(cfg, "ingest", "heuristic_fallback", default=True))
    if raw is None:
        if fallback:
            memories = heuristic(rows)
            used = "heuristic"
    elif not memories and not _EMPTY_ANSWER.match(raw):
        if fallback:
            memories = heuristic(rows)
            used = "heuristic"
        logger.warning("LLM 返回无法解析，原文前 120 字：%r", raw[:120])

    scope = rows[0].get("scope_id") or "*"
    owner_id = _owner_of_batch(rows)
    min_imp = float(on(cfg, "ingest", "min_kind_importance", default=0.5) or 0.0)
    written = []
    for m in memories:
        if m["importance"] < min_imp and m["kind"] == "episode":
            continue
        vis = _visibility_for(cfg, scope, owner_id, m["category"])
        mid = memorydb.add(
            m["kind"], m["text"],
            category=m["category"],
            importance=m["importance"],
            scope_id=scope,
            visibility=vis,
            source="ingest",
            meta={"batch": len(rows), "owner_id": owner_id, "scope": scope},
        )
        if mid:
            written.append(mid)

    batch_id = int(time.time())
    ingest.mark_consumed([r["id"] for r in rows], batch_id)
    logger.info("提炼轮次 batch=%s scope=%s 消息=%d 记忆=%d 可见=%d 方式=%s",
                batch_id, (scope or "")[-24:], len(rows), len(written),
                len(written), used)
    return {"rows": len(rows), "written": written, "mode": used}


def run_once(logger, cfg, *, force: bool = False) -> dict:
    batch_size = int(on(cfg, "ingest", "batch_size", default=12) or 12)
    if not on(cfg, "ingest", "enabled", default=True):
        return {"skipped": "disabled"}

    ingest.trim(int(on(cfg, "ingest", "max_backlog", default=500) or 500))
    n = ingest.pending_count()
    if n == 0:
        return {"skipped": "empty", "pending": 0}

    # 按会话分组：绝不跨 scope 混批，否则记忆会记到别的会话名下（老 bug）
    rows_all = ingest.pending(min(max(n, batch_size), 60))
    groups: dict[str, list[dict]] = {}
    for r in rows_all:
        groups.setdefault(r.get("scope_id") or "*", []).append(r)

    ready = [g for g in groups.values() if len(g) >= batch_size]
    if not ready and force:
        ready = [max(groups.values(), key=len)]
    if not ready:
        return {"skipped": "not_enough", "pending": n,
                "groups": {k: len(v) for k, v in groups.items()}}

    out = {"rows": 0, "written": [], "mode": "llm", "groups": []}
    for rows in ready[:2]:
        r = _process_group(logger, cfg, rows)
        out["rows"] += r["rows"]
        out["written"] += r["written"]
        out["mode"] = r["mode"]
        out["groups"].append({"scope": (rows[0].get("scope_id") or "")[-24:],
                              "rows": r["rows"], "written": len(r["written"])})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--interval", type=int, default=0)
    args = ap.parse_args(argv)

    logger, cfgstore = _bootstrap.boot("ingest_worker")
    logger.info("ingest_worker 启动 once=%s force=%s", args.once, args.force)

    cfg = cfgstore.get()
    interval = args.interval or int(on(cfg, "ingest", "interval_seconds", default=300) or 300)

    if args.once:
        r = run_once(logger, cfg, force=args.force)
        logger.info("单轮结果：%s", r)
        return 0

    while True:
        try:
            cfg = cfgstore.get()
            r = run_once(logger, cfg)
            # 跳过也要留痕，否则「攒够 N 条」期间日志一片空白，看起来像卡死
            if r.get("skipped"):
                logger.info("本轮跳过：%s 待处理=%d", r["skipped"], ingest.pending_count())
        except KeyboardInterrupt:
            logger.info("收到中断，退出")
            return 0
        except Exception as e:
            logger.exception("轮次异常：%s", e)
        try:
            time.sleep(max(10, interval))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
