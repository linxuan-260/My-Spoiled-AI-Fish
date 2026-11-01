"""自检 runner：每次改配置/改代码后跑一遍，确认没把人格和注入搞坏。

检查项：
    A1  人格 sha256 与基线一致（插件全程没写 personas 表）
    A2  strip_all(注入后) == strip_all(注入前)
    A3  "# Persona Instructions" 段长度与人格正文一致、前 40 字一致
    A4  注入总字数不超预算
    A5  作用域隔离（别的会话的记忆进不来）

用法：
    python worker/eval_runner.py
    python worker/eval_runner.py --quiet
"""

from __future__ import annotations

import argparse
import json
import sys

try:
    import _bootstrap  # type: ignore
except ImportError:  # pragma: no cover
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap  # type: ignore

_bootstrap.add_path()

from astrbot_plugin_fish_memory import compose, markers  # noqa: E402
from astrbot_plugin_fish_memory.budget import TurnCtx  # noqa: E402
from astrbot_plugin_fish_memory.config import dget, on  # noqa: E402
from astrbot_plugin_fish_memory.store import (  # noqa: E402
    feedbackdb, persona_guard,
)

ANCHOR = "# Persona Instructions"


def build_baseline_prompt() -> tuple[str, str]:
    """还原 AstrBot 拼人格的方式，返回 (完整 system_prompt, 人格正文)。"""
    row = persona_guard.read_persona() or {}
    persona = row.get("system_prompt") or ""
    head = (
        "You are an AI assistant. 当前时间 2026-10-28 21:00:00。\n"
        "用户 测试用户（1000000000）在群里说话。\n"
    )
    full = head + f"\n{ANCHOR}\n\n{persona}\n"
    return full, persona


def run(logger=None, cfg=None, quiet: bool = False) -> dict:
    import logging

    logger = logger or logging.getLogger("fishmem.eval")
    cfg = cfg or _bootstrap.boot("eval_runner")[1].get()

    base_prompt, persona = build_baseline_prompt()
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        if not quiet:
            logger.info("%s %s %s", "PASS" if ok else "FAIL", name, detail)

    # ---- A1 人格未被改动
    have = persona_guard.current_hash()
    want = str(on(cfg, "persona_guard", "sha256", default="") or "")
    check("A1 人格sha256", (not want) or have == want,
          f"cur={have[:16]} exp={want[:16]}")

    # ---- 构造一轮注入
    ctx = TurnCtx(
        scope_id="eval:group:1", umo="aiocqhttp:GroupMessage:eval",
        user_text="测试用户说他最近怕黑，想早点睡", sender_id="1000000000",
        sender_name="测试用户", msg_id="eval-msg-1", self_id="1000000001",
    )
    res = compose.run(base_prompt, ctx, cfg)

    after = res["prompt"]

    # ---- A2 可逆
    check("A2 注入可逆", markers.strip_all(after) == markers.strip_all(base_prompt),
          f"+{res['total']}字 块={[b.layer for b in res['blocks']]}")

    # ---- A3 人格段完整
    idx = after.find(ANCHOR)
    tail = after[idx + len(ANCHOR):] if idx >= 0 else ""
    seg = tail.strip("\n")
    # 注入块在人格之后，抠掉它们再比
    seg_clean = markers.strip_all(seg).strip("\n")
    check("A3 人格段完整", seg_clean == persona,
          f"len {len(seg_clean)} vs {len(persona)}")
    check("A3b 前40字", seg_clean[:40] == persona[:40],
          repr(persona[:40]))

    # ---- A4 预算
    budgets = dget(cfg, "master", "budgets")
    total_budget = int(budgets.get("total") or 400)
    check("A4 预算", res["total"] <= total_budget,
          f"{res['total']}/{total_budget}")

    # ---- A5 作用域隔离
    other = TurnCtx(scope_id="eval:group:999", umo="aiocb23:GroupMessage:x",
                    user_text="爹说他最近怕黑，想早点睡", msg_id="eval-msg-2")
    res2 = compose.run(base_prompt, other, cfg)
    leak = markers.strip_all(res2["prompt"]) != markers.strip_all(base_prompt)
    check("A5 作用域隔离", not leak, "无泄漏" if not leak else "泄漏!")

    # ---- A6 影子模式不注入
    shadow_cfg = _clone_with(cfg, "shadow_mode", True)
    res3 = compose.run(base_prompt, ctx, shadow_cfg)
    check("A6 影子模式", res3["prompt"] == base_prompt and not res3["shadow"] is False,
          f"shadow={res3['shadow']} 注入={res3['prompt'] != base_prompt}")

    n_pass = sum(1 for r in results if r["ok"])
    detail = {"results": results, "blocks": [b.layer for b in res["blocks"]],
              "total_chars": res["total"]}
    try:
        feedbackdb.record_run(len(results), n_pass, detail)
    except Exception:
        pass

    if not quiet:
        logger.info("自检 %d/%d 通过", n_pass, len(results))
    return {"n_case": len(results), "n_pass": n_pass, "results": results}


def _clone_with(cfg, key: str, value):
    """在不落盘的前提下改一个 master 键，用于影子模式测试。"""
    from astrbot_plugin_fish_memory.config import Node

    d = json.loads(json.dumps(cfg.as_dict()))
    d.setdefault("master", {})[key] = value
    return Node(d)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    logger, _ = _bootstrap.boot("eval_runner")
    r = run(logger, quiet=args.quiet)
    bad = [x for x in r["results"] if not x["ok"]]
    for x in bad:
        logger.error("失败：%s %s", x["name"], x["detail"])
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
