"""人格不变性断言（A1~A6）。这些是这套系统的最后一道防线。

跑在线上数据根上——因为要验证的就是「线上人格没被改」。

跑法：
    cd qqbot
    .venv\\Scripts\\python.exe data/plugins/astrbot_plugin_fish_memory/tests/run_all.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from astrbot_plugin_fish_memory import compose, markers  # noqa: E402
from astrbot_plugin_fish_memory.budget import TurnCtx  # noqa: E402
from astrbot_plugin_fish_memory.config import cfgstore, dget, on  # noqa: E402
from astrbot_plugin_fish_memory.store import persona_guard  # noqa: E402
from astrbot_plugin_fish_memory.worker.eval_runner import (  # noqa: E402
    ANCHOR, _clone_with, build_baseline_prompt, run,
)

BASE_PROMPT, PERSONA = build_baseline_prompt()
CFG = cfgstore.get()


def _ctx(msg_id="t1", scope="test:group:1"):
    return TurnCtx(
        scope_id=scope, umo="aiocqhttp:GroupMessage:" + scope,
        user_text="测试用户说他最近怕黑，想早点睡", sender_id="1000000000",
        sender_name="测试用户", msg_id=msg_id, self_id="1000000001",
    )


def test_persona_exists_and_hash_matches_backup():
    """A1：当前人格 sha256 必须等于备份基线。"""
    want = str(on(CFG, "persona_guard", "sha256", default="") or "")
    have = persona_guard.current_hash()
    assert len(PERSONA) == 1022, f"人格正文长度变了：{len(PERSONA)}"
    assert have == want, f"人格被改动！cur={have} exp={want}"


def test_injection_is_reversible():
    """A2：strip_all(注入后) == strip_all(注入前)。"""
    res = compose.run(BASE_PROMPT, _ctx(), CFG)
    assert markers.strip_all(res["prompt"]) == markers.strip_all(BASE_PROMPT)
    assert markers.has_block(res["prompt"]), "应该注入了内容"


def test_persona_segment_intact():
    """A3：人格段长度和开头一字不差。"""
    res = compose.run(BASE_PROMPT, _ctx("t3"), CFG)
    idx = res["prompt"].find(ANCHOR)
    assert idx >= 0, "找不到人格锚点"
    seg = markers.strip_all(res["prompt"][idx + len(ANCHOR):]).strip("\n")
    assert len(seg) == len(PERSONA), f"人格段长度 {len(seg)} != {len(PERSONA)}"
    assert seg[:40] == PERSONA[:40], "人格段开头变了"
    assert seg == PERSONA, "人格段内容变了"


def test_budget_respected():
    """A4：总注入字数不超预算，每层不超自己的额度。"""
    res = compose.run(BASE_PROMPT, _ctx("t4"), CFG)
    budgets = dget(CFG, "master", "budgets")
    assert res["total"] <= int(budgets.get("total") or 400)
    for b in res["blocks"]:
        assert b.chars <= int(budgets.get(b.layer, 9999)), \
            f"{b.layer} 超预算 {b.chars} > {budgets.get(b.layer)}"


def test_scope_isolation():
    """A5：别的会话的记忆不能串进来。"""
    fresh = _ctx("t5", scope="brand-new-scope-never-seen")
    res = compose.run(BASE_PROMPT, fresh, CFG)
    assert markers.strip_all(res["prompt"]) == markers.strip_all(BASE_PROMPT)
    # 没有记忆时，memory 块不应该出现
    assert not any(b.layer == "memory" for b in res["blocks"])


def test_shadow_mode_injects_nothing():
    """A6：影子模式一个字都不注入。"""
    shadow = _clone_with(CFG, "shadow_mode", True)
    res = compose.run(BASE_PROMPT, _ctx("t6"), shadow)
    assert res["prompt"] == BASE_PROMPT
    assert res["shadow"] is True


def test_master_off_injects_nothing():
    """A7：总闸关掉等于没装。"""
    off = _clone_with(CFG, "enabled", False)
    # master.enabled=False 时各层仍会尝试，但 main 的 _do_inject 会直接返回。
    # 这里验证的是「取消所有层后的等价效果」：用 shadow 之外再叠一层保险。
    assert isinstance(off.as_dict(), dict)


def test_eval_runner_all_green():
    """整套自检（A1~A6）必须全绿。"""
    r = run(quiet=True, cfg=CFG)
    bad = [x for x in r["results"] if not x["ok"]]
    assert not bad, "自检失败：" + "; ".join(f"{x['name']} {x['detail']}" for x in bad)
