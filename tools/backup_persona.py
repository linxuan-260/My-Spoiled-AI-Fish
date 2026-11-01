"""人格与主库一次性冷备份。

用法（在 qqbot 目录下）：
    .\.venv\Scripts\python.exe tools\backup_persona.py

产物：
    backup/<stamp>/persona_1.json      人格行完整 JSON（可直接 UPDATE 回去）
    backup/<stamp>/persona_1.sha256    system_prompt 的 sha256
    backup/<stamp>/persona_1.txt       正文纯文本，人眼可读
    backup/<stamp>/data_v4.db          一致性备份（sqlite .backup，含 WAL 内容）
    backup/<stamp>/cmd_config.json     主配置
    backup/<stamp>/MANIFEST.json       清单 + 时间戳 + 校验值
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

QQBOT = Path(__file__).resolve().parents[1]
DATA = QQBOT / "data"
DB = DATA / "data_v4.db"
PERSONA_ID = "1"


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def main() -> int:
    if not DB.exists():
        print(f"[FAIL] 找不到主库 {DB}")
        return 1

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = QQBOT / "backup" / f"{stamp}-persona"
    out.mkdir(parents=True, exist_ok=True)

    # ---- 1. 人格行 ----
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select * from personas where persona_id = ?", (PERSONA_ID,))]
    if not rows:
        con.close()
        print(f"[FAIL] personas 表里没有 persona_id='{PERSONA_ID}'")
        return 1

    persona = rows[0]
    prompt = persona["system_prompt"]
    h = sha256_text(prompt)

    (out / "persona_1.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "persona_1.sha256").write_text(h + "\n", encoding="utf-8")
    (out / "persona_1.txt").write_text(prompt, encoding="utf-8")

    # ---- 2. 主库一致性备份（必须走 backup API，直接 copy 会丢 WAL）----
    dst = out / "data_v4.db"
    bck = sqlite3.connect(str(dst))
    con.backup(bck)
    bck.close()
    con.close()

    # ---- 3. 配置 ----
    cfg_src = DATA / "cmd_config.json"
    if cfg_src.exists():
        shutil.copy2(cfg_src, out / "cmd_config.json")

    # ---- 4. 清单 ----
    manifest = {
        "created_at": stamp,
        "db_source": str(DB),
        "db_bytes": dst.stat().st_size,
        "persona_id": persona.get("persona_id"),
        "persona_row_id": persona.get("id"),
        "persona_len": len(prompt),
        "persona_sha256": h,
        "persona_updated_at": persona.get("updated_at"),
        "persona_created_at": persona.get("created_at"),
        "begin_dialogs": persona.get("begin_dialogs"),
        "restore_hint": (
            "整库回滚：停进程 → 删 data_v4.db-wal / -shm → "
            "copy data_v4.db 回来 → 起进程。"
            "只回滚人格：UPDATE personas SET system_prompt=?, updated_at=? "
            "WHERE persona_id='1'。"
        ),
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("[OK] 备份完成")
    print(f"     目录        {out}")
    print(f"     人格长度    {len(prompt)} 字")
    print(f"     sha256      {h}")
    print(f"     updated_at  {persona.get('updated_at')}")
    print(f"     主库大小    {dst.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
