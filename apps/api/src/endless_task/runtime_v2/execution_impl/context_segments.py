"""`runtime_v2.context_segments` 的包内别名（见 `effect_sink.py` 的说明）。

`execution` 里是**函数体内**的相对导入（`from .context_segments import ...`），
它的语义是"我所在包的兄弟模块"。这段代码搬进 `execution_impl/` 包后，`.` 变成了
新包，于是这条导入会去找 `execution_impl.context_segments`。用"包内别名模块"
把它接回原模块：语义与拆分前完全一致，也不会出现同一模块被加载两份。
"""

from __future__ import annotations

from ..context_segments import (  # noqa: F401
    ContextPlanShadowReport,
    ContextShadowReport,
    build_context_shadow,
    build_plan_shadow,
)

__all__ = [
    "ContextPlanShadowReport",
    "ContextShadowReport",
    "build_context_shadow",
    "build_plan_shadow",
]
