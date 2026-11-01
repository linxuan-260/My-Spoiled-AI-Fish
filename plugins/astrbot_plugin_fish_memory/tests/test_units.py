"""纯逻辑单测。插件数据跑在临时目录，人格/聊天历史仍读线上（只读）。

跑法：
    cd qqbot
    .venv\\Scripts\\python.exe -m pytest data/plugins/astrbot_plugin_fish_memory/tests -q
或
    .venv\\Scripts\\python.exe data/plugins/astrbot_plugin_fish_memory/tests/run_all.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))

from astrbot_plugin_fish_memory.store import paths  # noqa: E402

# 先固定线上主库，再把插件数据根换到临时目录
_REAL_MAIN_DB = paths.MAIN_DB
_TMP = Path(tempfile.mkdtemp(prefix="fishmem-units-"))
paths.use_root(_TMP)
paths.MAIN_DB = _REAL_MAIN_DB

from astrbot_plugin_fish_memory import budget, markers  # noqa: E402
from astrbot_plugin_fish_memory.store import (  # noqa: E402
    bigram, bootstrap, dbutil, feedbackdb, ledger, lifedb, memorydb, mooddb,
    retrieve, sanitizer, styledb,
)

BASE = "# Persona Instructions\n\n她是肥鱼。\n单次回复不超过15字。"


# ---------------------------------------------------------------- markers
def test_markers_idempotent():
    s = markers.append_block(BASE, "memory", "· [事] 爹怕黑")
    s = markers.append_block(s, "mood", "【现在的心情】愉悦0.42")
    s = markers.append_block(s, "style", "【说话的调子】短句")
    assert markers.strip_all(s) == markers.strip_all(BASE)
    assert markers.strip_all(BASE) == BASE
    assert markers.has_block(s)


def test_markers_same_layer_overwrites():
    s = markers.append_block(BASE, "memory", "A")
    s = markers.append_block(s, "memory", "B")
    assert s.count("FISHMEM") == 2
    assert "A" not in s and "B" in s


def test_markers_orphan_cleanup():
    s = BASE + "\n[[FISHMEM:v1:mood:begin]]\n漏了结尾"
    assert "FISHMEM" not in markers.strip_all(s)


def test_markers_does_not_touch_persona():
    s = markers.append_block(BASE, "memory", "X")
    assert "# Persona Instructions" in s
    assert "单次回复不超过15字。" in s


def test_markers_extract():
    s = markers.append_block(BASE, "life", "· 刚才发呆")
    assert markers.extract(s, "life") == "· 刚才发呆"
    assert markers.extract(s, "mood") is None


# ---------------------------------------------------------------- budget
def test_budget_fit_drops_whole_blocks():
    blocks = [budget.Block("memory", "x" * 100), budget.Block("mood", "y" * 72),
              budget.Block("life", "z" * 60), budget.Block("style", "w" * 130)]
    kept = budget.fit(blocks, 200)
    assert sum(b.chars for b in kept) <= 200
    assert all(b.chars in (100, 72, 60, 130) for b in kept)  # 没有半块


# ---------------------------------------------------------------- bigram
def test_bigram_basic():
    gs = bigram.bigrams("爹怕黑，睡觉要留一盏灯")
    assert "爹怕" in gs and "盏灯" in gs
    assert bigram.fts_query("") == ""
    assert '"爹怕"' in bigram.fts_query("爹怕黑")


# ---------------------------------------------------------------- sanitizer
def test_sanitizer_cs_tone():
    out, hits = sanitizer.sanitize("您是什么人呢？这里非常好。",
                                   {"sanitize_cs_tone": True})
    assert "你" in out and "啥" in out and "特别" in out
    assert "cs_tone" in hits


def test_sanitizer_paras_and_emoji():
    out, _ = sanitizer.sanitize("行\n\n\n\n好\n\n再来一段\n\n第三段", {})
    assert "\n\n\n" not in out
    out2, hits2 = sanitizer.sanitize("😀😀😀😀走", {"sanitize_emoji_rhythm": True})
    assert out2.startswith("😀走")
    assert "emoji_rhythm" in hits2


def test_sanitizer_drop_period():
    out, _ = sanitizer.sanitize("知道了。", {"sanitize_drop_period": True})
    assert out == "知道了"
    out2, _ = sanitizer.sanitize("一。二。三", {"sanitize_drop_period": True})
    assert "一。" in out2


def test_sanitizer_hard_clamp_off_by_default():
    long = "字" * 100
    out, _ = sanitizer.sanitize(long, {})
    assert len(out) == 100
    out2, hits = sanitizer.sanitize(long, {}, max_chars=15)
    assert len(out2) == 15 and "clamp" in hits


# ---------------------------------------------------------------- 库
def test_bootstrap_creates_five_dbs():
    r = bootstrap.bootstrap()
    assert r["ok"], r["errors"]
    for name in ("memory", "emotion", "life", "style", "feedback"):
        dbs = list(paths.LAYER_DIRS[name].glob("*.db"))
        assert dbs, f"{name} 没有建库"


def test_memory_add_dedup_and_search():
    mid = memorydb.add("episode", "爹怕黑，睡觉要留一盏灯", category="life",
                       importance=0.8, scope_id="s1")
    assert mid
    assert memorydb.add("episode", "爹怕黑，睡觉要留一盏灯", scope_id="s1") == mid
    memorydb.add("fact", "爹的生日是三月十二", category="profile",
                 importance=0.9, scope_id="s1")
    hits = retrieve.retrieve(query="睡觉怕黑", scope_id="s1", k=3, min_score=0.0,
                             enabled={"semantic": False, "keyword": True, "graph": True})
    assert hits and hits[0].mid == mid


def test_memory_scope_isolation():
    memorydb.add("fact", "只在A会话里存在的事实", scope_id="scopeA")
    hits = retrieve.retrieve(query="只在A会话里存在的事实", scope_id="scopeB", k=5,
                             min_score=0.0,
                             enabled={"semantic": False, "keyword": True, "graph": True})
    assert not hits


def test_mood_step_cap():
    st0 = mooddb.tick("m1", 6.0, 45.0)
    st1 = mooddb.apply_text("m1", "哈哈哈哈哈笑死", step_cap=0.12, label_cap=0.6)
    assert st1["p"] - st0["p"] <= 0.12 + 1e-9
    body = mooddb.render("m1")
    assert "情绪只影响语气" in body


def test_mood_negative_and_comfort():
    mooddb.apply_text("m2", "滚", step_cap=0.12)
    p1 = mooddb.tick("m2", 6.0, 45.0)["p"]
    mooddb.apply_comfort("m2", drop=0.08)
    p2 = mooddb.tick("m2", 6.0, 45.0)["p"]
    assert p2 < p1


def test_life_interest_and_render():
    lifedb.touch_interest("单片机")
    lifedb.touch_interest("单片机")
    assert lifedb.top_interests(3)[0][0] == "单片机"
    k, d = lifedb.pick_activity()
    lifedb.add_activity(k, d)
    lifedb.add_note("今天想早睡")
    body = lifedb.render()
    assert "这边的事" in body


def test_style_stats_on_real_history():
    st = styledb.refresh(force=True)
    assert st and st["n"] > 0
    assert st["median_len"] > 0
    body = styledb.render(st)
    assert "说话的调子" in body


def test_ledger_and_penalty():
    mid = memorydb.add("episode", "会被纠正掉的记忆", importance=0.8, scope_id="p1")
    ledger.write(scope_id="p1", umo="u", layer="memory", memory_ids=[mid],
                 block_chars=40, total_chars=200, shadow=False, query="q")
    assert ledger.last_memory_ids("p1") == [mid]
    assert feedbackdb.looks_like_correction("你记错了，不是这样")
    assert not feedbackdb.looks_like_correction("今天天气不错")
    feedbackdb.penalize([mid], factor=0.15, scope_id="p1", user_text="你记错了")
    with dbutil.ro(memorydb.db_file()) as con:
        imp = con.execute("SELECT importance FROM memories WHERE id=?", (mid,)).fetchone()[0]
    assert imp < 0.8


def test_archive_old_keeps_facts():
    old = memorydb.add("episode", "很久以前的事", scope_id="a1")
    keep = memorydb.add("fact", "很久以前的事实", scope_id="a1")
    with dbutil.tx(memorydb.db_file()) as con:
        con.execute(
            "UPDATE memories SET created_at=? WHERE id=?",
            ("2020-01-01T00:00:00+08:00", old),
        )
    n = memorydb.archive_old(90)
    assert n >= 1
    with dbutil.ro(memorydb.db_file()) as con:
        st = {r[0]: r[1] for r in con.execute(
            "SELECT id, status FROM memories WHERE id IN (?,?)", (old, keep))}
    assert st[old] == "archived" and st[keep] == "active"


# ---------------------------------------------------------------- 沉淀层
# 「真记得」和「翻旧账」的区别：一直记着的（core）与这句话无关也一直在；
# 检索结果只在这一句命中时出现，而且说完就往下走。

def _cfg():
    """默认配置的一份副本，改它不影响别人。"""
    import copy

    from astrbot_plugin_fish_memory.config import DEFAULTS, Node

    return Node(copy.deepcopy(DEFAULTS))


def _ctx_for(scope: str, text: str, msg_id: str):
    return budget.TurnCtx(
        scope_id=scope, umo="aiocqhttp:GroupMessage:" + scope,
        user_text=text, sender_id="1000000000", msg_id=msg_id,
    )


def _tier(mid: int) -> str:
    with dbutil.ro(memorydb.db_file()) as con:
        row = con.execute("SELECT tier FROM memories WHERE id=?", (mid,)).fetchone()
    return str(row[0]) if row else ""


def test_consolidate_promotes_important_facts():
    core = memorydb.add("fact", "主人是退伍军人", category="profile",
                        importance=0.9, scope_id="core-scope")
    trivia = memorydb.add("episode", "今天中午吃了黄焖鸡", category="life",
                          importance=0.3, scope_id="core-scope")
    r = memorydb.consolidate()
    assert r["promoted"] >= 1, r
    assert _tier(core) == "core"
    assert _tier(trivia) == "normal", "episode 是经历，不是「一直记着」的事"


def test_l1_writes_settled_and_recalled_sections():
    from astrbot_plugin_fish_memory.layers import l1_memory

    memorydb.add("fact", "爹从事科研工作", category="profile", importance=0.9,
                 scope_id="l1-scope")
    memorydb.add("episode", "刚才说到火锅很好吃", category="life", importance=0.2,
                 scope_id="l1-scope")
    memorydb.consolidate()

    block, ids = l1_memory.build(_ctx_for("l1-scope", "火锅好吃吗", "l1-a"), _cfg())
    assert block is not None, "应该产出记忆块"
    assert "【一直记着的】" in block.body, block.body
    assert "爹从事科研工作" in block.body
    assert "【刚想起来的】" in block.body, block.body
    assert "火锅" in block.body
    assert len(block.body) <= 168, f"超 memory 预算：{len(block.body)}"
    assert ids, "注入的记忆 id 要进账本"


def test_l1_settled_needs_no_query_match():
    from astrbot_plugin_fish_memory.layers import l1_memory

    block, _ = l1_memory.build(_ctx_for("l1-scope", "qwertyuiop", "l1-b"), _cfg())
    assert block is not None
    assert "爹从事科研工作" in block.body, "一直记着的不该依赖这一句说什么"
    assert "【刚想起来的】" not in block.body, "没命中就不该有检索段"


def test_l1_repeat_cooldown_skips_recent():
    from astrbot_plugin_fish_memory.layers import l1_memory

    mid = memorydb.add("episode", "昨晚群里聊到火锅底料", category="life",
                       importance=0.2, scope_id="cool-scope")
    first, _ = l1_memory.build(_ctx_for("cool-scope", "火锅底料好辣", "c1"), _cfg())
    assert first is not None and "火锅底料" in first.body

    ledger.write(scope_id="cool-scope", umo="u", layer="memory", memory_ids=[mid],
                 block_chars=len(first.body), total_chars=len(first.body),
                 shadow=False, query="火锅底料好辣")
    second, _ = l1_memory.build(_ctx_for("cool-scope", "火锅底料好辣", "c2"), _cfg())
    assert second is None or "火锅底料" not in second.body, "刚说过的不该隔一句又说"


def test_consolidate_demotes_stale():
    mid = memorydb.add("fact", "很久没想起的旧事", category="general",
                       importance=0.9, scope_id="old-scope")
    memorydb.consolidate()
    assert _tier(mid) == "core"
    with dbutil.tx(memorydb.db_file()) as con:
        con.execute("UPDATE memories SET created_at=?, last_access=? WHERE id=?",
                    ("2020-01-01T00:00:00+08:00", "2020-01-01T00:00:00+08:00", mid))
    r = memorydb.consolidate(demote_after_days=30)
    assert r["demoted"] >= 1, r
    assert _tier(mid) == "normal"


def test_consolidate_caps_core_total():
    for i in range(5):
        memorydb.add("fact", f"封顶测试事实{i}", importance=0.9, scope_id="cap-scope")
    r = memorydb.consolidate(max_core=2)
    assert r["core_total"] <= 2, r
    memorydb.consolidate(max_core=30)  # 还原，别把前面的 core 留在外面
