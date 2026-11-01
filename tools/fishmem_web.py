"""肥鱼五层记忆 · 本地 Web 面板。

只读为主，绑 127.0.0.1，不经过 AstrBot，也不碰人格。

用法：
    .venv\\Scripts\\python.exe tools\\fishmem_web.py              # 起服务并打开浏览器
    .venv\\Scripts\\python.exe tools\\fishmem_web.py --no-open    # 只起服务
    .venv\\Scripts\\python.exe tools\\fishmem_web.py --selftest   # 不开端口，逐个接口跑一遍
    .venv\\Scripts\\python.exe tools\\fishmem_web.py --port 6187

设计约束（和主系统一致）：
- 不 import astrbot，插件包是独立可用的，这里直接复用它的 paths / dbutil / persona_guard。
- 只读连接（dbutil.connect(readonly=True)），唯一的写操作是把单条记忆在
  active / archived 之间切换 —— 用户手工整理用，不走 worker 的批量链路。
- 不允许跨域调用：POST 必须同源（防本地恶意页面顺手改库）。
"""

from __future__ import annotations

import argparse
import ctypes
import itertools
import json
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOOLS_DIR = Path(__file__).resolve().parent
QQBOT_ROOT = TOOLS_DIR.parent
PLUGINS_ROOT = QQBOT_ROOT / "data" / "plugins"
UI_FILE = TOOLS_DIR / "fishmem_web_ui.html"
STARMAP_FILE = TOOLS_DIR / "fishmem_starmap.html"

if str(PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGINS_ROOT))

from astrbot_plugin_fish_memory.config import (  # noqa: E402
    ConfigStore,
    default_path,
    dget,
    on,
)
from astrbot_plugin_fish_memory.store import dbutil, paths, persona_guard  # noqa: E402

LAYERS = (("memory", "memory.db"), ("emotion", "emotion.db"), ("life", "life.db"),
          ("style", "style.db"), ("feedback", "feedback.db"))
WORKERS = ("life", "ingest", "digest")

_cfg_store = ConfigStore(default_path())


def cfg():
    return _cfg_store.get()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------- 工具

def _connect(layer: str, filename: str, readonly: bool = True):
    p = Path(paths.db_path(layer, filename))
    if not p.exists():
        return None
    con = dbutil.connect(p, readonly=readonly)
    con.row_factory = sqlite3.Row
    return con


def _rows(con, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def _one(con, sql: str, args=()):
    r = con.execute(sql, args).fetchone()
    return r[0] if r else None


def _q(layer: str, filename: str, sql: str, args=()) -> list[dict]:
    con = _connect(layer, filename)
    if con is None:
        return []
    try:
        return _rows(con, sql, args)
    finally:
        con.close()


def worker_status() -> list[dict]:
    """pid 文件 + OpenProcess 探活（Windows 上不能用 os.kill(pid, 0)）。"""
    out = []
    pdir = Path(paths.PDATA) / "logs" / "pids"
    for name in WORKERS:
        f = pdir / f"{name}.pid"
        item = {"name": name, "pid": None, "alive": False}
        if f.exists():
            try:
                pid = int(f.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                pid = 0
            item["pid"] = pid or None
            if pid:
                try:
                    os.kill(pid, 0)
                    item["alive"] = True
                except OSError:
                    item["alive"] = False
        out.append(item)
    return out


def db_sizes() -> list[dict]:
    out = []
    for layer, filename in LAYERS:
        p = Path(paths.db_path(layer, filename))
        item = {"layer": layer, "exists": p.exists(), "kb": 0}
        if p.exists():
            item["kb"] = round(p.stat().st_size / 1024, 1)
            wal = Path(str(p) + "-wal")
            if wal.exists():
                item["kb"] += round(wal.stat().st_size / 1024, 1)
        out.append(item)
    return out


def persona_info() -> dict:
    """只比对，不触发 auto_restore（那是插件自己的活，网页不该偷偷改库）。"""
    info = persona_guard.summary()
    expected = str(on(cfg(), "persona_guard", "sha256", default="") or "").strip().lower()
    info["expected"] = expected
    if not info.get("found"):
        info["ok"] = None
        info["note"] = "没读到期人格"
    elif not expected:
        info["ok"] = None
        info["note"] = "配置里还没记基线"
    else:
        info["ok"] = info.get("sha256") == expected
        info["note"] = "与基线一致" if info["ok"] else "人格被改动了！"
    return info


# --------------------------------------------------------------------- 接口

def api_overview() -> dict:
    c = cfg()
    mem = _connect("memory", "memory.db")
    try:
        by_kind = _rows(mem, "SELECT kind, COUNT(*) n FROM memories "
                             "WHERE status='active' GROUP BY kind ORDER BY n DESC") if mem else []
        by_status = _rows(mem, "SELECT status, COUNT(*) n FROM memories "
                               "GROUP BY status ORDER BY n DESC") if mem else []
        pending = _one(mem, "SELECT COUNT(*) FROM ingest_queue WHERE consumed_at IS NULL") if mem else 0
        oldest = _one(mem, "SELECT MIN(ts) FROM ingest_queue WHERE consumed_at IS NULL") if mem else None
        ledger_total = _one(mem, "SELECT COUNT(*) FROM injection_ledger") if mem else 0
        ledger_today = _one(mem, "SELECT COUNT(*) FROM injection_ledger "
                                 "WHERE substr(ts,1,10)=date('now','localtime')") if mem else 0
        last_inject = _rows(mem, "SELECT ts, SUM(block_chars) chars, MAX(total_chars) total "
                                 "FROM injection_ledger GROUP BY ts, umo "
                                 "ORDER BY ts DESC LIMIT 1") if mem else []
    finally:
        if mem:
            mem.close()

    mood = _q("emotion", "emotion.db",
              "SELECT scope_id, pleasure, arousal, dominance, energy, updated_at "
              "FROM mood_state ORDER BY updated_at DESC LIMIT 5")
    life = _q("life", "life.db",
              "SELECT kind, topic, ts FROM activities ORDER BY id DESC LIMIT 3")
    style_stats = {}
    for r in _q("style", "style.db", "SELECT v FROM style_cache WHERE k='stats'"):
        try:
            style_stats = json.loads(r["v"] or "{}")
        except (TypeError, ValueError):
            style_stats = {}

    return {
        "ts": now_iso(),
        "config": {
            "master": bool(on(c, "master", "enabled", default=True)),
            "shadow": bool(on(c, "master", "shadow_mode", default=False)),
            "budgets": dget(c, "master", "budgets"),
            "layers": {k: bool(on(c, f"L{k}", "enabled", default=True)) for k in (1, 2, 3, 4, 5)},
            "max_reply_chars": on(c, "L4", "max_reply_chars", default=0),
            "ingest": {
                "enabled": bool(on(c, "ingest", "enabled", default=True)),
                "batch_size": on(c, "ingest", "batch_size", default=0),
                "interval_seconds": on(c, "ingest", "interval_seconds", default=0),
                "model": on(c, "ingest", "model", default=""),
            },
        },
        "persona": persona_info(),
        "workers": worker_status(),
        "dbs": db_sizes(),
        "memory": {"by_kind": by_kind, "by_status": by_status},
        "queue": {"pending": pending or 0, "oldest": oldest},
        "ledger": {"total": ledger_total or 0, "today": ledger_today or 0,
                   "last": last_inject[0] if last_inject else None},
        "mood": mood,
        "life": life,
        "style_stats": style_stats,
    }


def api_memories(params: dict) -> dict:
    limit = max(1, min(int(params.get("limit", 200)), 2000))
    where, args = [], []
    kind = (params.get("kind") or "").strip()
    if kind and kind != "all":
        where.append("kind=?")
        args.append(kind)
    status = (params.get("status") or "active").strip()
    if status and status != "all":
        where.append("status=?")
        args.append(status)
    vis = (params.get("vis") or "").strip()
    if vis and vis != "all":
        where.append("visibility=?")
        args.append(vis)
    q = (params.get("q") or "").strip()
    if q:
        where.append("(text LIKE ? OR category LIKE ? OR scope_id LIKE ?)")
        args.extend([f"%{q}%"] * 3)
    sql = ("SELECT id, kind, text, category, importance, created_at, last_access, "
           "access_count, status, source, scope_id, visibility FROM memories")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    rows = _q("memory", "memory.db", sql, args + [limit])
    return {"rows": rows, "count": len(rows), "limit": limit}


def api_moods() -> dict:
    return {
        "state": _q("emotion", "emotion.db",
                    "SELECT scope_id, pleasure, arousal, dominance, energy, updated_at "
                    "FROM mood_state ORDER BY updated_at DESC LIMIT 50"),
        "events": _q("emotion", "emotion.db",
                     "SELECT id, ts, scope_id, trigger, d_p, d_a, d_d, "
                     "before_p, before_a, after_p, after_a, labels, note "
                     "FROM mood_events ORDER BY id DESC LIMIT 60"),
        "labels": _q("emotion", "emotion.db",
                     "SELECT scope_id, label, value, updated_at FROM mood_labels "
                     "ORDER BY updated_at DESC LIMIT 60"),
        "tough": _q("emotion", "emotion.db",
                    "SELECT scope_id, active, until, reason, updated_at FROM tough_mouth"),
    }


def api_life() -> dict:
    return {
        "activities": _q("life", "life.db",
                         "SELECT id, ts, kind, topic, detail, curious, mood_p, mood_a "
                         "FROM activities ORDER BY id DESC LIMIT 60"),
        "notes": _q("life", "life.db",
                    "SELECT id, ts, note, ttl_hours, promoted, memory_id "
                    "FROM life_notes ORDER BY id DESC LIMIT 40"),
        "interests": _q("life", "life.db",
                        "SELECT id, name, weight, source, last_touched FROM interests "
                        "ORDER BY weight DESC LIMIT 40"),
        "state": _q("life", "life.db", "SELECT k, v FROM daemon_state"),
    }


def api_style() -> dict:
    cache = {}
    for r in _q("style", "style.db", "SELECT k, v, updated_at FROM style_cache"):
        try:
            cache[r["k"]] = json.loads(r["v"] or "null")
        except (TypeError, ValueError):
            cache[r["k"]] = r["v"]
    return {
        "stats": _q("style", "style.db",
                    "SELECT id, ts, window_n, median_len, p90_len, no_punct_ratio, "
                    "fillers, emoji_ratio, q_ratio FROM style_stats ORDER BY id DESC LIMIT 10"),
        "cache": cache,
        "feedback": _q("style", "style.db",
                       "SELECT id, ts, rule, before, after, scope_id, note "
                       "FROM style_feedback ORDER BY id DESC LIMIT 40"),
    }


def api_ledger(params: dict) -> dict:
    limit = max(1, min(int(params.get("limit", 120)), 1000))
    rows = _q("memory", "memory.db",
              "SELECT id, ts, layer, block_chars, total_chars, shadow, scope_id, memory_ids "
              "FROM injection_ledger ORDER BY id DESC LIMIT ?", (limit,))
    by_layer = _q("memory", "memory.db",
                  "SELECT layer, COUNT(*) n, ROUND(AVG(block_chars),1) avg_chars "
                  "FROM injection_ledger GROUP BY layer ORDER BY n DESC")
    turns = _q("memory", "memory.db",
               "SELECT ts, scope_id, COUNT(*) n_layer, SUM(block_chars) chars, "
               "MAX(total_chars) total, MAX(shadow) shadow "
               "FROM injection_ledger GROUP BY ts, scope_id ORDER BY ts DESC LIMIT 60")
    return {"rows": rows, "by_layer": by_layer, "turns": turns}


def api_queue(params: dict) -> dict:
    limit = max(1, min(int(params.get("limit", 60)), 500))
    return {
        "pending": _q("memory", "memory.db",
                      "SELECT id, ts, scope_id, role, sender_name, text FROM ingest_queue "
                      "WHERE consumed_at IS NULL ORDER BY id DESC LIMIT ?", (limit,)),
        "recent": _q("memory", "memory.db",
                     "SELECT id, ts, scope_id, role, sender_name, text, consumed_at, batch_id "
                     "FROM ingest_queue WHERE consumed_at IS NOT NULL "
                     "ORDER BY id DESC LIMIT ?", (limit,)),
        "stats": _q("memory", "memory.db",
                    "SELECT COUNT(*) total, "
                    "SUM(CASE WHEN consumed_at IS NULL THEN 1 ELSE 0 END) pending "
                    "FROM ingest_queue"),
    }


def api_feedback() -> dict:
    return {
        "eval_runs": _q("feedback", "feedback.db",
                        "SELECT id, ts, n_case, n_pass, detail FROM eval_runs "
                        "ORDER BY id DESC LIMIT 30"),
        "eval_cases": _q("feedback", "feedback.db",
                         "SELECT id, name, layer, expect_sub, expect_not FROM eval_cases "
                         "ORDER BY id"),
        "corrections": _q("feedback", "feedback.db",
                          "SELECT id, ts, scope_id, user_text, evidence, penalized "
                          "FROM corrections ORDER BY id DESC LIMIT 40"),
    }


# --------------------------------------------------------------------- 星图

def _hub_of(scope_id: str) -> tuple[str, str]:
    """会话 → 星团。群聊 grp_<群号>，私聊 dm_<QQ>。"""
    s = scope_id or ""
    uid = s.rsplit(":", 1)[-1] if ":" in s else (s or "unknown")
    uid = uid or "unknown"
    if "GroupMessage" in s:
        return ("h:grp:" + uid, "grp")
    return ("h:dm:" + uid, "dm")


def _bigrams(text: str) -> set:
    s = "".join((text or "").split())
    if len(s) <= 1:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _iso_ms(s):
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def api_graph(params: dict) -> dict:
    """记忆星图：节点 + 三类边（会话归属 / 共注入 / 文本相似）。只读。"""
    t_start = time.time()
    include_archived = str(params.get("archived", "1")).lower() not in ("0", "false", "no")
    sim_min = float(params.get("sim", 0.45))
    topk = int(params.get("topk", 3))

    rows = _q("memory", "memory.db",
              "SELECT id, kind, category, importance, access_count, created_at, "
              "last_access, scope_id, status, text FROM memories")
    if not include_archived:
        rows = [r for r in rows if r.get("status") == "active"]

    by_id: dict[str, dict] = {}
    hubs: dict[str, dict] = {}
    for r in rows:
        nid = "m%d" % int(r["id"])
        hub_id, hkind = _hub_of(r.get("scope_id"))
        if hub_id not in hubs:
            hubs[hub_id] = {"id": hub_id, "t": "hub", "hk": hkind,
                            "key": (r.get("scope_id") or "").rsplit(":", 1)[-1],
                            "n": 0, "text": ""}
        hubs[hub_id]["n"] += 1
        by_id[nid] = {
            "id": nid, "t": "mem", "kind": r["kind"] or "fact",
            "cat": r.get("category") or "", "imp": float(r.get("importance") or 0),
            "acc": int(r.get("access_count") or 0),
            "ts": _iso_ms(r.get("created_at")),
            "st": r.get("status") or "active",
            "sc": hub_id, "text": (r.get("text") or "").strip(),
        }

    nodes = list(by_id.values()) + list(hubs.values())

    edges: list[dict] = []
    seen: set = set()

    def add_edge(a: str, b: str, rel: str, w: float):
        if a == b:
            return
        if (a not in by_id and a not in hubs) or (b not in by_id and b not in hubs):
            return
        key = (a, b, rel) if a < b else (b, a, rel)
        if key in seen:
            return
        seen.add(key)
        edges.append({"s": a, "t": b, "r": rel, "w": round(w, 3)})

    for node in by_id.values():
        add_edge(node["id"], node["sc"], "scope", 1.0)

    # 共注入：同一次注入里一起进上下文的记忆 —— 真实的"同时想起"
    co: dict[tuple, int] = {}
    for r in _q("memory", "memory.db",
                "SELECT memory_ids FROM injection_ledger "
                "WHERE layer='memory' AND memory_ids NOT IN ('', '[]')"):
        try:
            ids = json.loads(r["memory_ids"])
        except (TypeError, ValueError):
            continue
        mids = sorted({"m%d" % int(i) for i in ids
                       if str(i).isdigit() and ("m%d" % int(i)) in by_id})
        for a, b in itertools.combinations(mids, 2):
            co[(a, b)] = co.get((a, b), 0) + 1
    for (a, b), n in co.items():
        add_edge(a, b, "co", n)

    # 文本相似：字符二元组包含度，每点只留最强 topk 条，避免毛球
    grams = {nid: _bigrams(node["text"]) for nid, node in by_id.items() if node["text"]}
    best: dict[str, list] = {}
    ids = [nid for nid in by_id if nid in grams]
    for a, b in itertools.combinations(ids, 2):
        ga, gb = grams[a], grams[b]
        if not ga or not gb:
            continue
        inter = len(ga & gb)
        if not inter:
            continue
        score = inter / min(len(ga), len(gb))
        if score < sim_min:
            continue
        best.setdefault(a, []).append((score, b))
        best.setdefault(b, []).append((score, a))
    for a, lst in best.items():
        for score, b in sorted(lst, reverse=True)[:topk]:
            add_edge(a, b, "sim", score)

    by_kind: dict[str, int] = {}
    by_hub: dict[str, int] = {}
    for node in by_id.values():
        by_kind[node["kind"]] = by_kind.get(node["kind"], 0) + 1
        by_hub[node["sc"]] = by_hub.get(node["sc"], 0) + 1
    for hub in hubs.values():
        hub["n"] = by_hub.get(hub["id"], hub["n"])

    stamps = [n["ts"] for n in by_id.values() if n["ts"]]
    meta = {
        "memories": len(by_id), "hubs": len(hubs), "edges": len(edges),
        "active": sum(1 for n in by_id.values() if n["st"] == "active"),
        "archived": sum(1 for n in by_id.values() if n["st"] != "active"),
        "by_kind": by_kind,
        "t0": min(stamps) if stamps else None,
        "t1": max(stamps) if stamps else None,
        "now": int(time.time() * 1000),
        "elapsed_ms": int((time.time() - t_start) * 1000),
    }
    return {"ts": now_iso(), "meta": meta, "nodes": nodes, "edges": edges}


ALLOWED_STATUS = ("active", "archived")
ALLOWED_VIS = ("private", "owner", "public", "group")


def api_memory_mark(body: dict) -> dict:
    """面板上唯一的写操作：改 status（active/archived）或 visibility（共享层）。"""
    try:
        mid = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id 不合法"}
    status = str(body.get("status") or "").strip()
    vis = str(body.get("visibility") or "").strip()
    if not status and not vis:
        return {"ok": False, "error": "status 和 visibility 至少给一个"}
    if status and status not in ALLOWED_STATUS:
        return {"ok": False, "error": f"status 只能是 {ALLOWED_STATUS}"}
    if vis and vis not in ALLOWED_VIS:
        return {"ok": False, "error": f"visibility 只能是 {ALLOWED_VIS}"}
    con = _connect("memory", "memory.db", readonly=False)
    if con is None:
        return {"ok": False, "error": "找不到 memory.db"}
    try:
        changed = 0
        if status:
            changed += con.execute(
                "UPDATE memories SET status=? WHERE id=?", (status, mid)).rowcount
        if vis:
            changed += con.execute(
                "UPDATE memories SET visibility=? WHERE id=?", (vis, mid)).rowcount
        con.commit()
        return {"ok": True, "id": mid, "status": status, "visibility": vis,
                "changed": changed}
    finally:
        con.close()


ROUTES_GET = {
    "/api/overview": lambda p: api_overview(),
    "/api/memories": api_memories,
    "/api/moods": lambda p: api_moods(),
    "/api/life": lambda p: api_life(),
    "/api/style": lambda p: api_style(),
    "/api/ledger": api_ledger,
    "/api/queue": api_queue,
    "/api/feedback": lambda p: api_feedback(),
    "/api/graph": api_graph,
}
ROUTES_POST = {"/api/memories/status": api_memory_mark}


# --------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "FishMemWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静点，只留错误
        if str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            sys.stderr.write("[web] %s %s\n" % (args[0], args[1]))

    # ---------------------------------------------------------------- 响应
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            try:
                html = UI_FILE.read_text(encoding="utf-8")
            except OSError as e:
                self._send(500, f"UI 文件读不到：{e}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path in ("/starmap", "/starmap.html", "/star", "/星图"):
            try:
                html = STARMAP_FILE.read_text(encoding="utf-8")
            except OSError as e:
                self._send(500, f"星图文件读不到：{e}".encode("utf-8"),
                           "text/plain; charset=utf-8")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        fn = ROUTES_GET.get(u.path)
        if fn is None:
            self._json({"error": "not found", "path": u.path}, 404)
            return
        try:
            self._json(fn(params))
        except Exception as e:  # 单个接口炸了不能拖垮服务
            import traceback
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------------------------------------------------------- POST
    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin") or ""
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin in (f"http://{host}", f"https://{host}")

    def do_POST(self):
        u = urlparse(self.path)
        # 先把 body 读干净再判断 —— 否则 403/404 早退时残留的 body 会被
        # keep-alive 当成下一个请求行解析（日志里出现 {"id":1,...}GET /api/... 这种鬼东西）
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = b""
        if n > 0:
            raw = self.rfile.read(min(n, 1 << 20))
        fn = ROUTES_POST.get(u.path)
        if fn is None:
            self._json({"error": "not found", "path": u.path}, 404)
            return
        if not self._same_origin():
            self._json({"error": "跨域请求被拒绝"}, 403)
            return
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError("body 必须是对象")
        except Exception as e:
            self._json({"error": f"请求体解析失败：{e}"}, 400)
            return
        try:
            self._json(fn(body))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


class Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 会让第二个进程也能"绑"到同一端口（然后抢连接），
    # 必须关掉，才能靠 bind 失败判断"已经在跑"。
    allow_reuse_address = False
    daemon_threads = True


def serve(host: str, port: int, open_browser: bool) -> int:
    try:
        srv = Server((host, port), Handler)
    except OSError as e:
        # 端口被占：多半是上一次的面板还在跑，直接安静退出，别弹一屏 traceback
        print(f"[fishmem-web] 绑定 {host}:{port} 失败（{e}）—— 面板可能已经在跑了")
        if open_browser:
            webbrowser.open(f"http://{host}:{port}/")
        return 0
    srv.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"[fishmem-web] 面板地址 {url}")
    print(f"[fishmem-web] 数据目录 {paths.PDATA}")
    print("[fishmem-web] Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[fishmem-web] 已退出")
    finally:
        srv.server_close()
    return 0


def selftest() -> int:
    """不起端口，把每个接口都跑一遍，打印条目数。"""
    checks = [
        ("overview", lambda: api_overview()),
        ("memories", lambda: api_memories({"limit": 5})),
        ("moods", lambda: api_moods()),
        ("life", lambda: api_life()),
        ("style", lambda: api_style()),
        ("ledger", lambda: api_ledger({})),
        ("queue", lambda: api_queue({})),
        ("feedback", lambda: api_feedback()),
        ("graph", lambda: api_graph({})),
    ]
    bad = 0
    for name, fn in checks:
        try:
            t0 = time.time()
            r = fn()
            size = len(json.dumps(r, ensure_ascii=False, default=str))
            print(f"  OK   {name:<9} {size:>6} 字节  {(time.time()-t0)*1000:>5.0f}ms")
        except Exception as e:
            bad += 1
            print(f"  FAIL {name:<9} {type(e).__name__}: {e}")
    ui = UI_FILE.exists() and UI_FILE.stat().st_size
    print(f"  {'OK  ' if ui else 'FAIL'} ui        {ui or '缺失'} 字节  {UI_FILE}")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="肥鱼五层记忆本地面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6186)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        rc = selftest()
        print("自检通过" if rc == 0 else "自检有失败项")
        return rc
    return serve(a.host, a.port, not a.no_open)


if __name__ == "__main__":
    sys.exit(main())
