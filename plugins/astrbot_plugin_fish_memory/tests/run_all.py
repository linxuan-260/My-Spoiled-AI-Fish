"""不依赖 pytest 的测试入口。

    cd qqbot
    .venv\\Scripts\\python.exe data/plugins/astrbot_plugin_fish_memory/tests/run_all.py

注意：test_units 会把插件数据根切到临时目录，所以两个文件按顺序在不同进程里跑
（这里用 subprocess 隔离，最干净）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable


def run_file(name: str) -> tuple[bool, str]:
    """在一个独立进程里 import 该测试文件并跑所有 test_* 函数。"""
    runner = f"""
import importlib.util, sys, traceback
spec = importlib.util.spec_from_file_location("t", r"{HERE / name}")
m = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(m)
except Exception:
    traceback.print_exc()
    sys.exit(2)
fns = [(n, f) for n, f in vars(m).items() if n.startswith("test_") and callable(f)]
ok = fail = 0
for n, f in fns:
    try:
        f()
        print("  PASS", n)
        ok += 1
    except Exception as e:
        print("  FAIL", n, "->", type(e).__name__, e)
        fail += 1
print(f"{{ok}}/{{ok+fail}} passed")
sys.exit(0 if fail == 0 else 1)
"""
    p = subprocess.run([PY, "-c", runner], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out


def main() -> int:
    files = ["test_units.py", "test_invariants.py"]
    all_ok = True
    for f in files:
        print(f"\n=== {f} ===")
        ok, out = run_file(f)
        print(out.rstrip())
        all_ok = all_ok and ok
    print("\n=== 总体 ===")
    print("ALL PASS" if all_ok else "有失败")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
