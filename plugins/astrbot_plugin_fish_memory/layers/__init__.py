"""五层。每层一个 build(ctx, cfg) -> (Block|None, list[int])。

约定：
    - 任何异常都在层内部吞掉并返回 (None, [])，绝不影响对话。
    - 层不写 system_prompt，只产出一段正文，由 main 统一拼 marker。
"""

from __future__ import annotations

__all__ = ["l1_memory", "l2_mood", "l3_life", "l4_style"]
