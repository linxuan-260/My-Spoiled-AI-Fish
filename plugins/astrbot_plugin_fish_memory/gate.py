"""发言判断：群聊里这一句到底该不该主动接。

AstrBot 自己的「主动回复」（`provider_ltm_settings.active_reply`）是
`random() < possibility_reply` 的骰子（线上 0.2）—— 对场合一无所知。
群友在吵、在说正事、在聊别人的隐私，它照样有 20% 的机会冒一句。

这里把骰子换成判断：

    先看现场（谁在说话、说了什么、她多久没开口、有没有人接她的话）
    → 再决定要不要冒泡
    → 判断结果写进 speech_gate 表，理由可查、可单测

判断是纯规则，不额外调模型：可解释、零成本、离线可测。
原则是「默认闭嘴」——只有跟她有关、或者有人问了没人接的时候才开口。
"""

from __future__ import annotations

import dataclasses
import logging
import time

from .config import dget, on
from .store import gatedb

log = logging.getLogger("fishmem.gate")

DEFAULT_HUSH = "没什么要接的"


@dataclasses.dataclass
class Decision:
    speak: bool
    reason: str
    score: float = 0.0
    features: dict = dataclasses.field(default_factory=dict)

    def as_row(self) -> dict:
        return {"decision": "speak" if self.speak else "hush",
                "reason": self.reason, "score": round(float(self.score), 3),
                "features": self.features}


def _hour_in_range(hour: int, spec: str) -> bool:
    """支持 "0-7" 与跨夜 "23-7"。"""
    try:
        a, b = str(spec or "").replace("，", "-").split("-", 1)
        a, b = int(a) % 24, int(b) % 24
    except (TypeError, ValueError):
        return False
    if a == b:
        return False
    if a < b:
        return a <= hour < b
    return hour >= a or hour < b


def _hit(text: str, words) -> str:
    for w in words or ():
        w = str(w or "").strip()
        if w and w in (text or ""):
            return w
    return ""


def _count_hit(texts, words) -> int:
    return sum(1 for t in texts if _hit(t, words))


def _mentions_self(texts, names) -> bool:
    for t in texts:
        for n in names or ():
            if str(n or "") and str(n) in (t or ""):
                return True
    return False


def _interest_hit(text: str, interests) -> str:
    for name, w in interests or ():
        name = str(name or "").strip()
        if len(name) >= 2 and name in (text or ""):
            return name
    return ""


def judge(*, text: str, at_ids, reply_to: str, is_image: bool,
          scene, last_speak_ts, cfg, interests=(), now_ts: float | None = None,
          self_id: str = "", muted: str = "") -> Decision:
    """纯函数：给现场，给一句判断。scene 是 gatedb.SceneMsg 列表（旧→新）。"""
    c = dget(cfg, "gate")

    text = (text or "").strip()
    now_ts = float(now_ts or time.time())
    hour = int(time.localtime(now_ts).tm_hour)
    names = [str(n) for n in (c.get("self_names") or []) if str(n)]
    hush_words = list(c.get("hush_keywords") or [])
    quarrel_words = list(c.get("quarrel_keywords") or [])
    serious_words = list(c.get("serious_keywords") or [])
    scene = list(scene or [])
    recent = [m.text for m in scene[-6:] if getattr(m, "text", "")]
    feats = {"hour": hour, "text_len": len(text), "at": list(at_ids or []),
             "reply_to": reply_to or "", "scene": len(scene)}
    if last_speak_ts:
        feats["last_speak_min"] = round((now_ts - float(last_speak_ts)) / 60.0, 1)

    # ---- 先看该不该闭嘴（先判死不判活）----
    # 爹说过「闭嘴」：这条排在所有判断之前，说了不听是最不能犯的错
    if muted:
        feats["muted"] = muted
        return Decision(False, "被要求闭嘴", 0.0, feats)

    quiet = str(c.get("quiet_hours") or "")
    if quiet and _hour_in_range(hour, quiet):
        return Decision(False, "深夜", 0.0, feats)

    cooldown = float(c.get("cooldown_min") or 0)
    if last_speak_ts and cooldown > 0:
        gap_min = (now_ts - float(last_speak_ts)) / 60.0
        if gap_min < cooldown:
            return Decision(False, "刚说过话", 0.0, feats)

    # 她上次开口之后，没人提到她 / 没有 at 她 → 再说就是自言自语
    ttl = float(c.get("unanswered_ttl_min") or 0)
    if last_speak_ts and ttl > 0:
        gap_min = (now_ts - float(last_speak_ts)) / 60.0
        if 0 < gap_min < ttl:
            after = [m for m in scene if getattr(m, "ts", 0) > float(last_speak_ts)]
            engaged = _mentions_self([m.text for m in after], names) or any(
                self_id and self_id in (getattr(m, "at_ids", None) or []) for m in after)
            if after and not engaged:
                return Decision(False, "上次开口没人接", 0.0, feats)

    hit = _hit(text, hush_words) or _hit(" ".join(recent[-3:]), hush_words)
    if hit:
        feats["hush_word"] = hit
        return Decision(False, "话题敏感", 0.0, feats)

    if _count_hit(recent, quarrel_words) >= 2:
        return Decision(False, "在吵", 0.0, feats)

    sh = _hit(text, serious_words) or (
        _hit(" ".join(recent[-2:]), serious_words) if recent else "")
    if sh:
        feats["serious_word"] = sh
        return Decision(False, "在说正事", 0.0, feats)

    # at 了别人 / 回复的是别人 → 不是冲她来的
    others = [a for a in (at_ids or []) if str(a) and str(a) != str(self_id)]
    feats["at_others"] = others
    if others:
        return Decision(False, "在跟别人说话", 0.0, feats)
    if reply_to and not any(n and n in reply_to for n in names):
        return Decision(False, "在接别人的话", 0.0, feats)

    # 群里正聊得热，她插不上嘴
    win = float(c.get("hot_window_sec") or 0)
    hot_n = int(c.get("hot_count") or 0)
    if win > 0 and hot_n > 0:
        hot = sum(1 for m in scene if now_ts - float(getattr(m, "ts", 0)) <= win)
        feats["hot"] = hot
        if hot >= hot_n:
            return Decision(False, "聊得正热", 0.0, feats)

    if is_image or (len(text) < 2 and not text):
        return Decision(False, "只有图/没内容", 0.0, feats)

    # ---- 到这里才考虑开口 ----
    if _mentions_self([text], names) or (self_id and self_id in [str(a) for a in at_ids or []]):
        return Decision(True, "提到我", 0.8, feats)

    it = _interest_hit(text, interests)
    if it:
        feats["interest"] = it
        return Decision(True, "聊到我感兴趣的", 0.6, feats)

    if c.get("allow_question", True) and ("?" in text or "？" in text) and len(text) >= 4:
        return Decision(True, "有人问了句没人接的话", 0.4, feats)

    return Decision(False, DEFAULT_HUSH, 0.0, feats)


MUTE_DEFAULT = 30


def _escalate(cfg, base: int) -> int:
    """短时间内反复被要求闭嘴，就安静得更久（这就是「学会」）。"""
    try:
        n = gatedb.count_reason("被要求闭嘴", hours=24)
    except Exception:
        n = 0
    return int(int(base) * min(2 ** min(int(n), 3), 8))


def directive_action(cfg, scope_id: str, sender_id: str, text: str,
                     now_ts: float | None = None) -> dict | None:
    """爹说「闭嘴 / 别说话了」→ 真闭嘴一段时间；说「可以说了」→ 解除。

    只认主人（master.owner_id）。私聊里下的指令 = 全局静音；群里下的
    = 只静音这个群。每次被要求都记一笔；24 小时内反复被要求会越安静越久。
    """
    c = dget(cfg, "gate")
    if not c.get("enabled", True):
        return None
    owner = str(on(cfg, "master", "owner_id", default="") or "")
    if not owner or str(sender_id or "") != owner:
        return None
    t = (text or "").strip()
    if not t:
        return None

    kw = _hit(t, c.get("mute_keywords") or [])
    if kw:
        scope = "*" if ":FriendMessage:" in str(scope_id) else str(scope_id)
        minutes = _escalate(cfg, int(c.get("mute_minutes") or MUTE_DEFAULT))
        until = gatedb.set_mute(scope, minutes, reason=kw)
        gatedb.write_decision(scope_id=str(scope_id), decision="hush",
                              reason="被要求闭嘴", score=0.0,
                              features={"keyword": kw, "minutes": minutes, "scope": scope})
        # 记成偏好，让以后检索得到、也进得了 core（「学会了」而不只是这一次安静）
        try:
            from .store import memorydb

            memorydb.add("lesson", "爹让我闭嘴的时候要立刻安静，别追问也别解释。",
                         category="preference", importance=0.55, scope_id=scope)
        except Exception:
            pass
        return {"action": "mute", "minutes": minutes, "until": until, "keyword": kw}

    kw = _hit(t, c.get("unmute_keywords") or [])
    if kw:
        muted = gatedb.mute_state(str(scope_id)) or gatedb.mute_state("*")
        gatedb.clear_mute("*")
        gatedb.clear_mute(str(scope_id))
        if muted:
            gatedb.write_decision(scope_id=str(scope_id), decision="speak",
                                  reason="被允许说话", score=1.0, features={"keyword": kw})
            return {"action": "unmute", "keyword": kw}
    return None


def decide(scope_id: str, *, text: str, at_ids=(), reply_to: str = "",
           is_image: bool = False, cfg, interests=(), self_id: str = "",
           sender_id: str = "", now_ts: float | None = None,
           write: bool = True) -> Decision:
    """读现场 → 判断 → 记账。任何异常都返回「闭嘴」（宁可少说）。"""
    try:
        now_ts = float(now_ts or time.time())
        act = directive_action(cfg, scope_id, sender_id, text, now_ts)
        if act and act.get("action") == "mute":
            return Decision(False, "被要求闭嘴", 0.0, {"keyword": act.get("keyword")})
        mute = gatedb.mute_state(scope_id)
        scene = gatedb.scene(scope_id, int(on(cfg, "gate", "scene_n", default=12) or 12))
        last = gatedb.last_speak_ts(scope_id)
        d = judge(text=text, at_ids=at_ids, reply_to=reply_to, is_image=is_image,
                  scene=scene, last_speak_ts=last, cfg=cfg, interests=interests,
                  now_ts=now_ts, self_id=self_id,
                  muted=(mute or {}).get("reason") or ("爹说过" if mute else ""))
        if write and on(cfg, "gate", "log_decisions", default=True):
            gatedb.write_decision(scope_id=scope_id, **d.as_row())
        return d
    except Exception as e:
        log.warning("发言判断异常，按闭嘴处理：%s", e)
        return Decision(False, "判断异常")


def _event_parts(event) -> dict:
    """从 AstrMessageEvent 上抠出 at / 回复 / 图片（不 import astrbot 类型）。"""
    out = {"at_ids": [], "reply_to": "", "is_image": False}
    try:
        comps = event.get_messages()
    except Exception:
        return out
    for comp in comps or ():
        n = type(comp).__name__
        if n == "At":
            qq = str(getattr(comp, "qq", "") or "")
            if qq:
                out["at_ids"].append(qq)
        elif n == "Reply":
            out["reply_to"] = str(getattr(comp, "sender_name", "") or "")
        elif n in ("Image", "Record", "Video", "File"):
            out["is_image"] = True
    return out


def self_id_of(umo: str) -> str:
    head = str(umo or "").split(":")[0]
    return head.split("-")[-1] if "-" in head else ""


def decide_for_event(event, cfg) -> Decision:
    parts = _event_parts(event)
    umo = str(getattr(event, "unified_msg_origin", "") or "")
    try:
        text = str(event.message_str or "")
    except Exception:
        text = ""
    try:
        sender_id = str(event.get_sender_id() or "")
    except Exception:
        sender_id = ""
    interests = []
    try:
        from .store import lifedb

        interests = lifedb.top_interests(8)
    except Exception:
        interests = []
    return decide(umo, text=text, at_ids=parts["at_ids"], reply_to=parts["reply_to"],
                  is_image=parts["is_image"], cfg=cfg, interests=interests,
                  self_id=self_id_of(umo), sender_id=sender_id)


def install_patch(logger, cfgstore) -> bool:
    """把 AstrBot 的「主动回复」换成判断（骰子 → 判断）。

    只在 AstrBot 自带的 group_chat_context 存在时接管；找不到就原样不动，
    自动回落到它会话原本的行为。幂等：重复调用只接管一次。
    """
    try:
        from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
    except Exception as e:
        logger.warning("[fishmem] 找不到 AstrBot 主动回复类，发言判断不接管：%s", e)
        return False
    if getattr(GroupChatContext, "_fishmem_gate", False):
        return True

    original = GroupChatContext.need_active_reply

    async def need_active_reply(self, event):
        try:
            cfg = cfgstore.get()
            if not on(cfg, "gate", "enabled", default=True):
                return await original(self, event)
            # 尊重 AstrBot 自己的开关、消息类型与白名单
            c = self.cfg(event)
            if not c.get("enable_active_reply"):
                return False
            try:
                from astrbot.api.platform import MessageType

                if event.get_message_type() != MessageType.GROUP_MESSAGE:
                    return False
            except Exception:
                pass
            if event.is_at_or_wake_command:
                return False
            wl = c.get("ar_whitelist") or []
            if wl:
                gid = ""
                try:
                    gid = event.get_group_id() or ""
                except Exception:
                    gid = ""
                if event.unified_msg_origin not in wl and gid not in wl:
                    return False
            return bool(decide_for_event(event, cfg).speak)
        except Exception:
            log.exception("[fishmem] 发言判断异常，回落到默认骰子")
            try:
                return await original(self, event)
            except Exception:
                return False

    need_active_reply._fishmem_gate = True   # type: ignore[attr-defined]
    GroupChatContext.need_active_reply = need_active_reply
    logger.info("[fishmem] 发言判断已接管 AstrBot 主动回复（骰子 → 判断）")
    return True


def patched() -> bool:
    """自检用：确认接管是否生效。"""
    try:
        from astrbot.builtin_stars.astrbot.group_chat_context import GroupChatContext
    except Exception:
        return False
    return bool(getattr(GroupChatContext, "_fishmem_gate", False))
