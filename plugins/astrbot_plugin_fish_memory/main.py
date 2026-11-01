"""肥鱼五层记忆 —— 主入口。

只有两个钩子：
    on_llm_request       往 req.system_prompt 追加带 marker 的注入块。
                         req 是 dataclass、只在内存里，人格表一个字节都不碰。
    on_decorating_result 发送前清洗文本。

设计底线：
    1. 本插件从不写 personas 表。唯一例外是 persona_guard.auto_restore，
       默认关闭。
    2. 任何异常都吞掉——宁可没有记忆，也不能让对话报错。
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from . import compose, markers
from .budget import TurnCtx
from .config import cfgstore, dget, on
from .store import (bootstrap as boot, feedbackdb, ingest, ledger, mooddb,
                    paths, persona_guard, sanitizer, styledb)

log = logging.getLogger("fishmem")

SEEN_MAX = 512


@register(
    "astrbot_plugin_fish_memory",
    "local",
    "五层记忆增强（记忆/情绪/生活/表达/反馈）。人格只读，增强全部走内存 system_prompt 追加。",
    "1.0.0",
)
class FishMemoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._blocks_cache: OrderedDict = OrderedDict()
        self._mood_seen: OrderedDict[str, float] = OrderedDict()
        self._guard_at = 0.0

        try:
            cfgstore.ensure_file()
            cfg = cfgstore.get()
            r = boot.bootstrap(cfg, write_back=cfgstore.write_back)
            logger.info("[fishmem] 启动 ok=%s errors=%s data=%s",
                        r.get("ok"), r.get("errors"), paths.PDATA)
            p = r.get("persona") or {}
            if p and p.get("current"):
                s = persona_guard.summary()
                logger.info("[fishmem] 人格基线 sha256=%s 正文=%s字 at=%s",
                            str(p["current"])[:16], s.get("len"), s.get("updated_at"))
            if p and p.get("ok") is False:
                logger.error("[fishmem] 人格基线不一致 current=%s expected=%s",
                             str(p.get("current"))[:16], str(p.get("expected"))[:16])
        except Exception:
            logger.exception("[fishmem] 启动引导失败（插件继续加载，功能降级）")

    # ================================================================ 钩子
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent,
                             req: ProviderRequest) -> None:
        try:
            self._do_inject(event, req)
        except Exception:
            logger.exception("[fishmem] 注入失败（已忽略，本轮无记忆）")

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        try:
            self._do_sanitize(event)
        except Exception:
            logger.exception("[fishmem] 发送前清洗失败（已忽略）")

    # ================================================================ 注入
    def _do_inject(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        cfg = cfgstore.get()
        if not on(cfg, "master", "enabled", default=False):
            return

        ctx = self._ctx_from_event(event)
        if not self._scope_allowed(cfg, ctx.scope_id):
            return

        self._maybe_guard(cfg)
        self._maybe_update_mood(cfg, ctx)

        prompt = getattr(req, "system_prompt", "") or ""
        res = compose.run(prompt, ctx, cfg, cache=self._blocks_cache)

        if res["prompt"] != prompt:
            req.system_prompt = res["prompt"]

        if on(cfg, "master", "log_injections", default=True) and res["blocks"]:
            total = res["total"]
            for b in res["blocks"]:
                ledger.write(
                    scope_id=ctx.scope_id, umo=ctx.umo, layer=b.layer,
                    memory_ids=res["ids"] if b.layer == "memory" else [],
                    block_chars=b.chars, total_chars=total,
                    shadow=bool(res["shadow"]), query=ctx.user_text[:120],
                )

    # ================================================================ 情绪
    def _maybe_update_mood(self, cfg, ctx: TurnCtx) -> None:
        enabled = on(cfg, "L2_emotion", "enabled", default=False)
        ing = on(cfg, "ingest", "enabled", default=False)
        if not enabled and not ing:
            return

        key = ctx.msg_id or f"{ctx.umo}|{ctx.user_text[:48]}"
        if key in self._mood_seen:
            return
        self._mood_seen[key] = time.time()
        while len(self._mood_seen) > SEEN_MAX:
            self._mood_seen.popitem(last=False)

        scope = ctx.scope_id or "*"
        text = ctx.user_text or ""

        if enabled:
            try:
                mooddb.apply_text(
                    scope, text,
                    step_cap=float(on(cfg, "L2_emotion", "step_cap", default=0.12) or 0.12),
                    label_cap=float(on(cfg, "L2_emotion", "label_cap", default=0.6) or 0.6),
                    half_hour=float(on(cfg, "L2_emotion", "mood_halflife_hour", default=6) or 6),
                    half_min=float(on(cfg, "L2_emotion", "label_halflife_min", default=45) or 45),
                    classify=bool(on(cfg, "L2_emotion", "sub", "classify", default=True)),
                )
                if on(cfg, "L2_emotion", "sub", "comfort", default=True) and \
                        mooddb.COMPLAINT.search(text):
                    mooddb.apply_comfort(
                        scope,
                        drop=float(on(cfg, "L2_emotion", "comfort_drop", default=0.08) or 0.08),
                        half_hour=float(on(cfg, "L2_emotion", "mood_halflife_hour", default=6) or 6),
                    )
                    ratio = float(on(cfg, "L2_emotion", "tough_mouth_ratio", default=0.35) or 0)
                    seed = abs(hash((scope, int(time.time() // 3600)))) % 1000 / 1000.0
                    if ratio > 0 and seed < ratio:
                        mooddb.set_tough(scope, True, "被凶了", hours=4.0)
            except Exception as e:
                log.debug("情绪更新失败：%s", e)

        # 纠正 -> 惩罚上一次注入的记忆
        if on(cfg, "L5_feedback", "enabled", default=False) and \
                on(cfg, "L5_feedback", "sub", "correction", default=True) and \
                feedbackdb.looks_like_correction(text):
            try:
                ids = ledger.last_memory_ids(scope, within_min=30)
                if ids:
                    n = feedbackdb.penalize(
                        ids,
                        factor=float(on(cfg, "L5_feedback", "penalty", default=0.15) or 0.15),
                        scope_id=scope, user_text=text,
                    )
                    log.info("纠正命中，惩罚 %s 条记忆", n)
            except Exception as e:
                log.debug("纠正检测失败：%s", e)

        if ing:
            ingest.append(scope_id=scope, umo=ctx.umo, role="user", text=text,
                          sender_id=ctx.sender_id, sender_name=ctx.sender_name,
                          msg_id=ctx.msg_id)

    # ================================================================ 清洗
    def _do_sanitize(self, event: AstrMessageEvent) -> None:
        cfg = cfgstore.get()
        if not on(cfg, "master", "enabled", default=False):
            return
        if not on(cfg, "L4_style", "enabled", default=False):
            return
        try:
            if event.is_stopped():
                return
        except Exception:
            pass

        result = event.get_result()
        if result is None or not getattr(result, "chain", None):
            return
        try:
            if not result.is_llm_result():
                return
        except Exception:
            return

        plains = [c for c in result.chain if isinstance(c, Plain)]
        if not plains:
            return
        text = "".join(c.text or "" for c in plains)
        if not text.strip():
            return

        new, hits = sanitizer.sanitize(
            text, dget(cfg, "L4_style", "sub"),
            max_chars=int(on(cfg, "L4_style", "max_reply_chars", default=0) or 0),
            cs_tone_map=dget(cfg, "L4_style", "cs_tone"),
        )
        if not new or new == text:
            return

        # 只改 Plain 的文本，图片/at 等结构原样保留
        first = True
        for c in result.chain:
            if isinstance(c, Plain):
                if first:
                    c.text = new
                    first = False
                else:
                    c.text = ""

        scope = self._scope_of(event)
        try:
            styledb.log_feedback(",".join(hits), text, new, scope_id=scope)
        except Exception:
            pass

        if on(cfg, "ingest", "enabled", default=False):
            try:
                ingest.append(
                    scope_id=scope,
                    umo=getattr(event, "unified_msg_origin", "") or "",
                    role="assistant", text=new,
                )
            except Exception:
                pass

    # ================================================================ 工具
    @staticmethod
    def _scope_of(event) -> str:
        try:
            return event.unified_msg_origin or "*"
        except Exception:
            return "*"

    def _ctx_from_event(self, event: AstrMessageEvent) -> TurnCtx:
        def safe(fn, default=""):
            try:
                v = fn()
                return default if v is None else v
            except Exception:
                return default

        umo = safe(lambda: event.unified_msg_origin, "")
        ctx = TurnCtx(
            scope_id=umo or "*",
            umo=umo,
            user_text=safe(lambda: event.message_str, "") or "",
            sender_id=str(safe(lambda: event.get_sender_id(), "")),
            sender_name=str(safe(lambda: event.get_sender_name(), "")),
            self_id=str(safe(lambda: event.get_self_id(), "")),
            is_group="GroupMessage" in umo,
            group_id=str(safe(lambda: event.get_group_id(), "")),
            msg_id=str(safe(lambda: event.message_obj.message_id, "")),
        )
        ctx.event = event
        return ctx

    @staticmethod
    def _scope_allowed(cfg, scope: str) -> bool:
        allow = on(cfg, "master", "enabled_scopes", default=None)
        if not allow:
            return True
        try:
            return scope in set(allow)
        except TypeError:
            return True

    def _maybe_guard(self, cfg) -> None:
        every = float(on(cfg, "persona_guard", "check_every_min", default=10) or 10) * 60
        now = time.time()
        if now - self._guard_at < every:
            return
        self._guard_at = now
        try:
            r = persona_guard.check(cfg, write_back=cfgstore.write_back)
            if r.get("changed"):
                logger.error("[fishmem] 人格被外部改写！current=%s",
                             str(r.get("current"))[:16])
        except Exception as e:
            log.debug("人格守卫失败：%s", e)
