"""注入编排。不依赖 astrbot，可被 worker / 自检脚本直接调用。

    run(prompt, ctx, cfg) -> {
        "prompt":  注入后的 system_prompt（shadow 模式时与输入相同）,
        "blocks":  [Block...],
        "ids":     [memory_id...],
        "shadow":  bool,
        "total":   注入总字数,
    }

保证：markers.strip_all(返回的 prompt) == markers.strip_all(传入的 prompt)。
不满足就放弃本次注入并返回原 prompt。
"""

from __future__ import annotations

import logging
from collections import OrderedDict

from . import markers
from .budget import Block, TurnCtx
from .config import dget, on
from .layers import l1_memory, l2_mood, l3_life, l4_style

log = logging.getLogger("fishmem.compose")

LAYER_MODULES = (l1_memory, l2_mood, l3_life, l4_style)
LAYER_ORDER = ("memory", "mood", "life", "style")
CACHE_MAX = 256


def build_blocks(cfg, ctx: TurnCtx, cache=None) -> tuple[list[Block], list[int]]:
    key = ctx.msg_id or f"{ctx.umo}|{ctx.user_text[:48]}|{ctx.sender_id}"
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            try:
                cache.move_to_end(key)
            except AttributeError:
                pass
            return hit

    raw: list[Block] = []
    mem_ids: list[int] = []
    for mod in LAYER_MODULES:
        try:
            b, ids = mod.build(ctx, cfg)
        except Exception as e:
            log.warning("层 %s 异常：%s", mod.__name__, e)
            b, ids = None, []
        if b is not None and b.body:
            raw.append(b)
            if ids:
                mem_ids = ids

    budgets = dget(cfg, "master", "budgets")
    total_budget = int(
        budgets.get("total")
        or on(cfg, "master", "budget_chars", default=400)
        or 400
    )

    kept = []
    for b in raw:
        cap = int(budgets.get(b.layer, 9999) or 9999)
        if b.chars > cap:
            # 以前这里是静默丢弃，life 层就这么消失了 18 小时，留一行日志
            log.info("层 %s 超过单层预算，整块丢弃：%d > %d 字", b.layer, b.chars, cap)
            continue
        kept.append(b)
    while kept and sum(b.chars for b in kept) > total_budget:
        # 超总量时按重要度从低到高丢：style → life → mood → memory
        victim = None
        for layer in reversed(LAYER_ORDER):
            for i, b in enumerate(kept):
                if b.layer == layer:
                    victim = i
                    break
            if victim is not None:
                break
        if victim is None:
            break
        dropped = kept.pop(victim)
        log.info("总量超预算 %d，丢弃层 %s（%d 字）",
                 total_budget, dropped.layer, dropped.chars)

    if not any(b.layer == "memory" for b in kept):
        mem_ids = []

    out = (kept, mem_ids)
    if cache is not None:
        cache[key] = out
        while len(cache) > CACHE_MAX:
            cache.popitem(last=False)
    return out


def run(prompt: str, ctx: TurnCtx, cfg, cache=None) -> dict:
    shadow = bool(on(cfg, "master", "shadow_mode", default=False))
    v = str(on(cfg, "master", "marker_version", default="1") or "1")

    blocks, ids = build_blocks(cfg, ctx, cache)
    base = prompt or ""

    if not blocks:
        return {"prompt": base, "blocks": [], "ids": [], "shadow": shadow,
                "total": 0, "skipped": "no_blocks"}

    if shadow:
        return {"prompt": base, "blocks": blocks, "ids": ids, "shadow": True,
                "total": sum(b.chars for b in blocks), "skipped": "shadow"}

    rendered = markers.render(base, [(b.layer, b.body) for b in blocks], v=v)

    # 硬不变量：注入只能追加，剥掉后必须与原文逐字节相同
    if markers.strip_all(rendered) != markers.strip_all(base):
        log.error("注入破坏性变更，已放弃本次注入")
        return {"prompt": base, "blocks": [], "ids": [], "shadow": shadow,
                "total": 0, "skipped": "invariant_violation"}

    # 每一块都必须真的出现在 prompt 里（以前 render 会把前面的块抹掉，账本却照记）
    lost = [b.layer for b in blocks if markers.extract(rendered, b.layer) is None]
    if lost:
        log.error("注入渲染丢失层 %s，已放弃本次注入", lost)
        return {"prompt": base, "blocks": [], "ids": [], "shadow": shadow,
                "total": 0, "skipped": "render_lost"}

    return {"prompt": rendered, "blocks": blocks, "ids": ids, "shadow": False,
            "total": sum(b.chars for b in blocks)}
