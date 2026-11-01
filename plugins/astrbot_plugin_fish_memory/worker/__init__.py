"""worker 进程。全部独立于 bot 主进程，读同一个 SQLite 库（WAL 并发安全）。"""
