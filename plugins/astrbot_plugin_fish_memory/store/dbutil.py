"""SQLite 连接、时间工具、五库 DDL。"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("fishmem.db")

TZ = _dt.timezone(_dt.timedelta(hours=8))
SCHEMA_VERSION = 3   # 2：memories 加 tier；3：加 speech_gate（发言判断）

# ------------------------------------------------------------------ 时间


def now() -> _dt.datetime:
    return _dt.datetime.now(TZ)


def iso(d: _dt.datetime | None = None) -> str:
    return (d or now()).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> _dt.datetime | None:
    if not s:
        return None
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=TZ)


def age_days(s: str | None) -> float:
    d = parse_iso(s)
    if not d:
        return 1e9
    return max(0.0, (now() - d).total_seconds() / 86400.0)


def age_hours(s: str | None) -> float:
    d = parse_iso(s)
    if not d:
        return 1e9
    return max(0.0, (now() - d).total_seconds() / 3600.0)


# ------------------------------------------------------------------ 连接

_init_lock = threading.Lock()
_ready: set[str] = set()


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    """打开连接。

    只读连接有个坑：WAL 模式的库在最后一个连接关闭时会删掉 -wal/-shm，
    此后再用 mode=ro 打开会因为没有 -shm 而报 "unable to open database file"。
    所以这里先试 mode=ro，失败就退化成 mode=rw + PRAGMA query_only=ON
    （同样写不进去，但能自己建 -shm）。
    """
    p = Path(path)
    if not readonly:
        p.parent.mkdir(parents=True, exist_ok=True)
        return _open(p, "rwc", extra_pragmas=True)

    try:
        con = _open(p, "ro", extra_pragmas=False)
        con.execute("PRAGMA query_only=ON")
        return con
    except sqlite3.OperationalError:
        con = _open(p, "rw", extra_pragmas=False)
        con.execute("PRAGMA query_only=ON")
        return con


def _open(p: Path, mode: str, *, extra_pragmas: bool) -> sqlite3.Connection:
    uri = f"file:{p.as_posix()}?mode={mode}"
    con = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA foreign_keys=ON")
    if extra_pragmas:
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as e:
            log.debug("pragma 设置失败（可能只读介质）：%s", e)
    return con


@contextmanager
def tx(path: Path | str):
    """写事务。yield 连接，正常退出 commit，异常 rollback。"""
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


@contextmanager
def ro(path: Path | str):
    if not Path(path).exists():
        yield None
        return
    con = connect(path, readonly=True)
    try:
        yield con
    finally:
        con.close()


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    r = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
    ).fetchone()
    return r is not None


# ------------------------------------------------------------------ DDL

DDL_MEMORY = """
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL CHECK (kind IN ('episode','fact','reflection','skill','lesson')),
  text        TEXT NOT NULL,
  text_norm   TEXT NOT NULL,
  hash        TEXT NOT NULL UNIQUE,
  category    TEXT NOT NULL DEFAULT 'general',
  importance  REAL NOT NULL DEFAULT 0.50,
  valence     REAL NOT NULL DEFAULT 0.00,
  arousal     REAL,
  dominance   REAL,
  created_at  TEXT NOT NULL,
  last_access TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'active'
              CHECK (status IN ('active','archived','merged','conflicted','deleted')),
  source      TEXT NOT NULL DEFAULT 'chat',
  meta        TEXT NOT NULL DEFAULT '{}',
  visibility  TEXT NOT NULL DEFAULT 'private'
              CHECK (visibility IN ('private','group','public','owner')),
  scope_id    TEXT NOT NULL DEFAULT '*',
  merged_into INTEGER REFERENCES memories(id),
  decayed_at  TEXT,
  -- core  = 沉淀下来的记忆：与这一句话无关也一直在（「本来就知道的」）
  -- normal= 普通记忆：只有被这句检索到才出现
  tier        TEXT NOT NULL DEFAULT 'normal'
);
CREATE INDEX IF NOT EXISTS idx_mem_status   ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_scope    ON memories(scope_id, status);
CREATE INDEX IF NOT EXISTS idx_mem_created  ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_mem_kind     ON memories(kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_cat      ON memories(category);
CREATE INDEX IF NOT EXISTS idx_mem_tier     ON memories(tier, status);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  bigram,
  memory_id UNINDEXED,
  scope_id  UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 0'
);

CREATE TABLE IF NOT EXISTS memory_vectors (
  memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
  model     TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  vec       BLOB NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_edges (
  src_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  dst_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  rel    TEXT NOT NULL DEFAULT 'related',
  weight REAL NOT NULL DEFAULT 0.50,
  created_at TEXT NOT NULL,
  PRIMARY KEY (src_id, dst_id, rel)
);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON memory_edges(dst_id);

CREATE TABLE IF NOT EXISTS memory_conflicts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,
  reason TEXT, score REAL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  UNIQUE (a_id, b_id)
);

CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,            -- image / audio / video
  path TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  transcript TEXT,
  meta TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_queue (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_id TEXT NOT NULL DEFAULT '*',
  umo TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL,             -- user / assistant
  sender_id TEXT DEFAULT '',
  sender_name TEXT DEFAULT '',
  msg_id TEXT DEFAULT '',
  text TEXT NOT NULL,
  ts TEXT NOT NULL,
  consumed_at TEXT,
  batch_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_iq_pending ON ingest_queue(consumed_at, id);

CREATE TABLE IF NOT EXISTS ingest_state (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injection_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '*',
  umo TEXT DEFAULT '',
  layer TEXT NOT NULL,
  memory_ids TEXT DEFAULT '[]',
  block_chars INTEGER DEFAULT 0,
  total_chars INTEGER DEFAULT 0,
  shadow INTEGER NOT NULL DEFAULT 0,
  query TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON injection_ledger(ts);
CREATE INDEX IF NOT EXISTS idx_ledger_scope ON injection_ledger(scope_id, ts);

-- 发言判断：群聊里每一次「该不该主动接」的决定与理由（gate.py）
CREATE TABLE IF NOT EXISTS speech_gate (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  scope_id TEXT NOT NULL DEFAULT '*',
  decision TEXT NOT NULL,          -- speak / hush
  reason TEXT NOT NULL,            -- 深夜 / 刚说过话 / 提到我 / ...
  score REAL DEFAULT 0,
  features TEXT DEFAULT '{}'       -- 现场特征（JSON）：热度、上次开口间隔、命中词
);
CREATE INDEX IF NOT EXISTS idx_gate_ts ON speech_gate(ts);
CREATE INDEX IF NOT EXISTS idx_gate_scope ON speech_gate(scope_id, ts);

-- 被要求闭嘴：爹说「别说话了」之后，这段时间内不主动开口（'*' = 所有会话）
CREATE TABLE IF NOT EXISTS speech_mute (
  scope_id TEXT PRIMARY KEY,
  until_ts TEXT NOT NULL,
  reason TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);
"""

DDL_EMOTION = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS mood_state (
  scope_id TEXT PRIMARY KEY,
  pleasure REAL NOT NULL DEFAULT 0.10,
  arousal  REAL NOT NULL DEFAULT 0.30,
  dominance REAL NOT NULL DEFAULT 0.10,
  energy   REAL NOT NULL DEFAULT 0.60,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mood_labels (
  scope_id TEXT NOT NULL,
  label    TEXT NOT NULL,
  value    REAL NOT NULL DEFAULT 0.0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (scope_id, label)
);

CREATE TABLE IF NOT EXISTS mood_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_id TEXT NOT NULL DEFAULT '*',
  ts TEXT NOT NULL,
  trigger TEXT,
  d_p REAL, d_a REAL, d_d REAL,
  before_p REAL, before_a REAL, before_d REAL,
  after_p REAL, after_a REAL, after_d REAL,
  labels TEXT DEFAULT '{}',
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_moodev_scope ON mood_events(scope_id, ts);

CREATE TABLE IF NOT EXISTS tough_mouth (
  scope_id TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 0,
  until TEXT,
  reason TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);
"""

DDL_LIFE = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS interests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  weight REAL NOT NULL DEFAULT 0.50,
  source TEXT DEFAULT 'chat',
  last_touched TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  topic TEXT NOT NULL,
  detail TEXT DEFAULT '',
  curious INTEGER DEFAULT 0,
  mood_p REAL, mood_a REAL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_act_ts ON activities(ts);

CREATE TABLE IF NOT EXISTS life_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  note TEXT NOT NULL,
  ttl_hours INTEGER NOT NULL DEFAULT 24,
  promoted INTEGER NOT NULL DEFAULT 0,
  memory_id INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_note_ttl ON life_notes(created_at);

CREATE TABLE IF NOT EXISTS daemon_state (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
);
"""

DDL_STYLE = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS style_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  window_n INTEGER NOT NULL,
  median_len REAL, p90_len REAL,
  no_punct_ratio REAL, fillers TEXT DEFAULT '[]',
  emoji_ratio REAL, q_ratio REAL,
  raw TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sty_ts ON style_stats(ts);

CREATE TABLE IF NOT EXISTS style_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  rule TEXT,
  before TEXT, after TEXT,
  scope_id TEXT DEFAULT '*',
  note TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_styfb_ts ON style_feedback(ts);

CREATE TABLE IF NOT EXISTS style_cache (
  k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TEXT NOT NULL
);
"""

DDL_FEEDBACK = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS corrections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  scope_id TEXT DEFAULT '*',
  user_text TEXT NOT NULL,
  evidence TEXT DEFAULT '[]',
  penalized TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corr_ts ON corrections(ts);

CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  expect_sub TEXT DEFAULT '',
  expect_not TEXT DEFAULT '',
  layer TEXT DEFAULT 'all',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  n_case INTEGER NOT NULL,
  n_pass INTEGER NOT NULL,
  detail TEXT DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_state (
  k TEXT PRIMARY KEY, v TEXT NOT NULL
);
"""

SCHEMAS = {
    "memory": DDL_MEMORY,
    "emotion": DDL_EMOTION,
    "life": DDL_LIFE,
    "style": DDL_STYLE,
    "feedback": DDL_FEEDBACK,
}

# 老库补列：CREATE TABLE IF NOT EXISTS 不会动已存在的表，只能显式 ALTER。
# SQLite 的 ADD COLUMN 要求有常量默认值，这里给的就是。
_ADD_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "memory": [
        ("tier", "TEXT NOT NULL DEFAULT 'normal'"),
    ],
}


def _migrate(con: sqlite3.Connection, key: str) -> None:
    """把旧表补到当前 schema。幂等：列在了就跳过。"""
    spec = _ADD_COLUMNS.get(key)
    if not spec:
        return
    for col, decl in spec:
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
        except sqlite3.Error:
            return
        if not cols or col in cols:
            continue
        con.execute(f"ALTER TABLE memories ADD COLUMN {col} {decl}")
        log.info("memory.db 补列 %s（%s）", col, decl)


def ensure_schema(db_file: Path, ddl: str, key: str) -> None:
    key_s = str(db_file)
    if key_s in _ready:
        return
    with _init_lock:
        if key_s in _ready:
            return
        con = connect(db_file)
        try:
            # 老库得先补列：DDL 里有基于新列的索引，缺列时 executescript 会失败
            if table_exists(con, "memories"):
                _migrate(con, key)
            con.executescript(ddl)
            _migrate(con, key)
            con.execute(
                "INSERT INTO meta(k, v) VALUES('schema_version', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SCHEMA_VERSION),),
            )
        finally:
            con.close()
        _ready.add(key_s)
        log.debug("schema ready: %s", db_file)


def kv_get(con, key: str, default: str | None = None) -> str | None:
    if not table_exists(con, "meta"):
        return default
    r = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(con, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )


def state_get(con, table: str, key: str, default: str | None = None) -> str | None:
    try:
        r = con.execute(f"SELECT v FROM {table} WHERE k=?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return r[0] if r else default


def state_set(con, table: str, key: str, value: str) -> None:
    con.execute(
        f"INSERT INTO {table}(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, str(value)),
    )
