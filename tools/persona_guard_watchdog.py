"""人格防篡改守护：检测 personas.persona_id='1' 是否被改动，若有则还原为批准版本。

每次运行都会把"被谁改的"尽量记录下来：
- 记录改后的内容摘要与哈希
- 记录改前哈希
- 追加到 /root/deploy/persona_guard.log
"""
import hashlib
import sqlite3
import time

DB = "/opt/astrbot/data/data_v4.db"
APPROVED = "/root/deploy/persona_approved.txt"
LOG = "/root/deploy/persona_guard.log"
PERSONA_ID = "1"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main():
    approved = open(APPROVED, encoding="utf-8").read().strip()
    want = sha(approved)
    con = sqlite3.connect(DB, timeout=10)
    row = con.execute(
        "select system_prompt, updated_at from personas where persona_id=?", (PERSONA_ID,)
    ).fetchone()
    if not row:
        con.close()
        return
    current, updated_at = row[0], row[1]
    got = sha(current or "")
    if got == want:
        con.close()
        return

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        "update personas set system_prompt=?, updated_at=? where persona_id=?",
        (approved, now, PERSONA_ID),
    )
    con.commit()
    con.close()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(
            f"[{now}] 检测到人格被改动，已还原。\n"
            f"  原哈希={want[:16]} 被改后的哈希={got[:16]} 长度 {len(current or '')}->{len(approved)}\n"
            f"  数据库里的 updated_at={updated_at}\n"
            f"  被改后内容前 120 字: {str(current)[:120]}\n\n"
        )
    print(f"[{now}] restored persona, drift {len(current or '')} -> {len(approved)}")


if __name__ == "__main__":
    main()
