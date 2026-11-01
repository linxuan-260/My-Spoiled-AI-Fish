"""记忆可见性 / 共享层管理（人工可审）。

用法（在插件目录下跑）：
    /opt/astrbot/.venv/bin/python share_memory.py --stats
    /opt/astrbot/.venv/bin/python share_memory.py --list --vis private --limit 30
    /opt/astrbot/.venv/bin/python share_memory.py --public 42,43
    /opt/astrbot/.venv/bin/python share_memory.py --private 42
    /opt/astrbot/.venv/bin/python share_memory.py --core 42,43
    /opt/astrbot/.venv/bin/python share_memory.py --normal 42
    /opt/astrbot/.venv/bin/python share_memory.py --consolidate
    /opt/astrbot/.venv/bin/python share_memory.py --backfill-owner
    /opt/astrbot/.venv/bin/python share_memory.py --apply-shared-scopes

可见性语义（memories.visibility）：
    private  只在本会话可见（默认）
    owner    meta.owner_id 这个人在任何会话里都可见（同一个人跨会话）
    public   共享层，所有会话可见
    group    预留，未使用

层次语义（memories.tier）：
    core     一直记着：与这句话说了什么无关，每轮都注入（不复述）
    normal   普通记忆：只有被这句检索到才出现

只改 memories 的 visibility / meta / tier 三列，不动正文、不动人格、不动其它层。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # .../astrbot_plugin_fish_memory
sys.path.insert(0, str(ROOT.parent))            # .../plugins（包名导入要它）

from astrbot_plugin_fish_memory.config import ConfigStore, default_path, dget  # noqa: E402
from astrbot_plugin_fish_memory.store import dbutil, memorydb  # noqa: E402

DM_RE = re.compile(r":FriendMessage:(\d+)\s*$")


def _meta(raw) -> dict:
    try:
        m = json.loads(raw or "{}")
        return m if isinstance(m, dict) else {}
    except (TypeError, ValueError):
        return {}


def _cfg():
    try:
        return ConfigStore(default_path()).get()
    except Exception:
        return {}


def stats() -> None:
    p = memorydb.ensure()
    with dbutil.ro(p) as con:
        rows = con.execute(
            "SELECT visibility, count(*) c FROM memories WHERE status='active' "
            "GROUP BY visibility ORDER BY c DESC").fetchall()
        print("按可见性：")
        for r in rows:
            print("  %-8s %s" % (r[0], r[1]))
        owned = con.execute(
            "SELECT count(*) FROM memories WHERE status='active' "
            "AND meta LIKE '%owner_id%'").fetchone()[0]
        print("带 owner_id 的：", owned)
        print("按层次：")
        for r in con.execute(
                "SELECT tier, count(*) c FROM memories WHERE status='active' "
                "GROUP BY tier ORDER BY c DESC").fetchall():
            print("  %-8s %s" % (r[0], r[1]))


def listing(vis: str | None, limit: int) -> None:
    p = memorydb.ensure()
    sql = ("SELECT id, visibility, tier, scope_id, category, importance, substr(text,1,40) t, meta "
           "FROM memories WHERE status='active'")
    args: list = []
    if vis:
        sql += " AND visibility=?"
        args.append(vis)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with dbutil.ro(p) as con:
        for r in con.execute(sql, args):
            own = _meta(r["meta"]).get("owner_id", "")
            print("%-5s %-8s %-6s %-22s %-10s %-4s own=%-11s %s" % (
                r["id"], r["visibility"], r["tier"] or "normal",
                (r["scope_id"] or "")[-22:], r["category"],
                r["importance"], own, (r["t"] or "").replace("\n", " ")))


def _set(ids: list[int], vis: str, note: str = "") -> int:
    if not ids:
        return 0
    p = memorydb.ensure()
    n = 0
    with dbutil.tx(p) as con:
        for mid in ids:
            row = con.execute("SELECT meta FROM memories WHERE id=?", (int(mid),)).fetchone()
            if not row:
                continue
            meta = _meta(row[0])
            if note:
                meta["shared_by"] = note
            con.execute("UPDATE memories SET visibility=?, meta=? WHERE id=?",
                        (vis, json.dumps(meta, ensure_ascii=False), int(mid)))
            n += 1
    return n


def backfill_owner() -> None:
    """把私聊会话里的记忆挂到那个 QQ 名下（owner），群里不猜。"""
    p = memorydb.ensure()
    n = 0
    with dbutil.tx(p) as con:
        rows = con.execute(
            "SELECT id, scope_id, visibility, meta FROM memories WHERE status='active'"
        ).fetchall()
        for r in rows:
            m = DM_RE.search(r["scope_id"] or "")
            if not m:
                continue
            qq = m.group(1)
            meta = _meta(r["meta"])
            if meta.get("owner_id") == qq:
                continue
            meta["owner_id"] = qq
            vis = r["visibility"] or "private"
            if vis == "private":
                vis = "owner"
            con.execute("UPDATE memories SET visibility=?, meta=? WHERE id=?",
                        (vis, json.dumps(meta, ensure_ascii=False), r["id"]))
            n += 1
    print("回填 owner 的记忆条数：", n)


def apply_shared_scopes(cfg) -> None:
    scopes = [str(s) for s in (dget(cfg, "master", "public_scopes") or [])]
    if not scopes:
        print("master.public_scopes 为空，跳过")
        return
    p = memorydb.ensure()
    n = 0
    with dbutil.tx(p) as con:
        for sc in scopes:
            cur = con.execute(
                "UPDATE memories SET visibility='public' WHERE status='active' AND scope_id=?",
                (sc,))
            n += cur.rowcount or 0
            print("  %s → public %s 条" % (sc, cur.rowcount or 0))
    print("共享层同步完成，共", n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--vis", default=None)
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--public", default="")
    ap.add_argument("--private", default="")
    ap.add_argument("--core", default="")
    ap.add_argument("--normal", default="")
    ap.add_argument("--consolidate", action="store_true")
    ap.add_argument("--backfill-owner", action="store_true")
    ap.add_argument("--apply-shared-scopes", action="store_true")
    a = ap.parse_args(argv)

    if a.stats:
        stats()
    if a.list:
        listing(a.vis, a.limit)
    if a.backfill_owner:
        backfill_owner()
    if a.apply_shared_scopes:
        apply_shared_scopes(_cfg())
    if a.public:
        ids = [int(x) for x in a.public.replace(" ", "").split(",") if x]
        print("设为 public：", _set(ids, "public", note="manual"))
    if a.private:
        ids = [int(x) for x in a.private.replace(" ", "").split(",") if x]
        print("设为 private：", _set(ids, "private", note="manual"))
    if a.core:
        ids = [int(x) for x in a.core.replace(" ", "").split(",") if x]
        print("设为 core（一直记着）：", memorydb.set_tier(ids, "core"))
    if a.normal:
        ids = [int(x) for x in a.normal.replace(" ", "").split(",") if x]
        print("设为 normal：", memorydb.set_tier(ids, "normal"))
    if a.consolidate:
        c = dget(_cfg(), "L1_memory", "consolidate")
        print("沉淀整理：", memorydb.consolidate(
            min_importance=float(c.get("min_importance") or 0.6),
            strong_importance=float(c.get("strong_importance") or 0.7),
            min_access=int(c.get("min_access") or 3),
            max_core=int(c.get("max_core") or 30),
            demote_after_days=int(c.get("demote_after_days") or 120),
        ))
    if not any((a.stats, a.list, a.public, a.private, a.core, a.normal,
                a.consolidate, a.backfill_owner, a.apply_shared_scopes)):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
