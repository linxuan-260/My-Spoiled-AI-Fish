# -*- coding: utf-8 -*-
"""本机 dashboard 插件管理（HS256 JWT，密钥取自 data/cmd_config.json）。

用法：
    python tools/plugin_hotload.py --status
    python tools/plugin_hotload.py --reload astrbot_plugin_fish_memory
    python tools/plugin_hotload.py --off    astrbot_plugin_livingmemory
    python tools/plugin_hotload.py --on     astrbot_plugin_livingmemory

注意：dashboard 有两套路由并存。**能用的是 legacy 这一套**（带 `plugin` 单数）：
    POST /api/plugin/reload    {"name": ...}
    POST /api/plugin/off       {"name": ...}
    POST /api/plugin/on        {"name": ...}
    GET  /api/plugin/get       ?name=...
新的 `/api/plugins/...`（复数）在这台机器上返回 405，别用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import jwt

BASE = "http://127.0.0.1:6185"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "data", "cmd_config.json")


def make_token() -> str:
    with open(CFG, encoding="utf-8-sig") as fh:
        d = json.load(fh)["dashboard"]
    payload = {"username": d.get("username", "astrbot"),
               "exp": int(time.time()) + 1800}
    return jwt.encode(payload, d["jwt_secret"], algorithm="HS256")


def call(tok: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + tok)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def show(tag: str, st: int, raw: str, limit: int = 500) -> None:
    flat = raw.replace("\n", " ")
    print(f"[{tag}] {st} {flat[:limit]}" + (" ..." if len(flat) > limit else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reload", metavar="NAME")
    ap.add_argument("--reload-failed", metavar="NAME",
                    help="给「上次启动失败」的插件用；成功后会清掉失败记录")
    ap.add_argument("--off", metavar="NAME")
    ap.add_argument("--on", metavar="NAME")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--name", default="astrbot_plugin_fish_memory")
    args = ap.parse_args()

    tok = make_token()

    if args.reload:
        show("reload", *call(tok, "POST", "/api/plugin/reload", {"name": args.reload}))
    if args.reload_failed:
        show("reload-failed",
             *call(tok, "POST", "/api/plugin/reload-failed",
                   {"dir_name": args.reload_failed}))
    if args.off:
        show("off", *call(tok, "POST", "/api/plugin/off", {"name": args.off}))
    if args.on:
        show("on", *call(tok, "POST", "/api/plugin/on", {"name": args.on}))
    if args.status or not (args.reload or args.reload_failed or args.off or args.on):
        q = urllib.parse.urlencode({"name": args.name})
        show("status", *call(tok, "GET", f"/api/plugin/get?{q}"), limit=1200)
    return 0


if __name__ == "__main__":
    sys.exit(main())
