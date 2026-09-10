"""`runtime_v2.effect_sink` 的包内别名。

理由同 `context_segments.py`：`execution` 里 `from .effect_sink import ...` 出现在
函数体内，`.` 的含义是"我所在包的兄弟模块"；代码搬进 `execution_impl/` 包之后用
别名模块保持原语义，避免把 `runtime_v2.effect_sink` 再加载一份。
"""

from __future__ import annotations

from ..effect_sink import build_record_effect_sink  # noqa: F401

__all__ = ["build_record_effect_sink"]
