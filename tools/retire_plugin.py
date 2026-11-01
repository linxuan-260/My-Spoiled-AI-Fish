# -*- coding: utf-8 -*-
"""Retire (deactivate) a plugin the same way the dashboard toggle does.

Writes ``inactivated_plugins`` in the ``preferences`` table of data_v4.db.
Row layout is ``{"val": ["data.plugins.<dir>.main", ...]}``.

Usage:
    python tmp/retire_plugin.py --list
    python tmp/retire_plugin.py --add astrbot_plugin_livingmemory
    python tmp/retire_plugin.py --remove astrbot_plugin_livingmemory
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3

DB = r"C:\Users\27874\Documents\deepseek-harness\default-workspace\qqbot\data\data_v4.db"
BACKUP = r"C:\Users\27874\Documents\deepseek-harness\default-workspace\qqbot\tmp\inactivated_plugins.backup.json"
KEY = "inactivated_plugins"
SCOPE = "global"
SCOPE_ID = "global"


def module_path(dir_name: str) -> str:
    return f"data.plugins.{dir_name}.main"


def load(con: sqlite3.Connection):
    row = con.execute(
        "SELECT id, value FROM preferences WHERE scope=? AND scope_id=? AND key=?",
        (SCOPE, SCOPE_ID, KEY),
    ).fetchone()
    if row is None:
        return None, {"val": []}
    rid, raw = row
    obj = json.loads(raw)
    if isinstance(obj, list):  # tolerate legacy bare-list layout
        obj = {"val": obj}
    obj.setdefault("val", [])
    return rid, obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--add", metavar="PLUGIN_DIR")
    ap.add_argument("--remove", metavar="PLUGIN_DIR")
    args = ap.parse_args()

    con = sqlite3.connect(DB, isolation_level=None)
    rid, obj = load(con)

    if args.list or (not args.add and not args.remove):
        print(f"row id={rid} count={len(obj['val'])}")
        for v in obj["val"]:
            print("   ", v)
        con.close()
        return 0

    with open(BACKUP, "w", encoding="utf-8") as fh:
        json.dump({"row_id": rid, "value": obj}, fh, ensure_ascii=False, indent=2)
    print(f"backup -> {BACKUP}")

    changed = False
    if args.add:
        mp = module_path(args.add)
        if mp in obj["val"]:
            print(f"already inactive: {mp}")
        else:
            obj["val"].append(mp)
            changed = True
            print(f"added: {mp}")
    if args.remove:
        mp = module_path(args.remove)
        if mp in obj["val"]:
            obj["val"].remove(mp)
            changed = True
            print(f"removed: {mp}")
        else:
            print(f"not present: {mp}")

    if changed:
        payload = json.dumps(obj, ensure_ascii=False)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        if rid is None:
            con.execute(
                "INSERT INTO preferences (created_at, updated_at, scope, scope_id, key, value) "
                "VALUES (?,?,?,?,?,?)",
                (now, now, SCOPE, SCOPE_ID, KEY, payload),
            )
        else:
            con.execute(
                "UPDATE preferences SET value=?, updated_at=? WHERE id=?",
                (payload, now, rid),
            )
        print("written.")
    else:
        print("no change.")

    rid2, obj2 = load(con)
    print(f"now row id={rid2} count={len(obj2['val'])}")
    for v in obj2["val"]:
        print("   ", v)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
