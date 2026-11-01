"""pytest 配置：把 data/plugins 加进 sys.path，插件才能按包导入。"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/ -> plugin/ -> plugins/
PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))
