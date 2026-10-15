"""@ 引用会话上下文（批次 C Task 3a 的**后端半场**）。

分层：

- ``mentions.projection``：**纯函数**的有界投影（确定性、零副作用、可单测）。
- ``mentions.service``：读库组装投影 + 校验（只读，无写入）。
- ``mentions.injection``：注入面的**非冻结薄包装**（补偿事务 + 幂等预检）。

投影粒度与预算定义在 ``mentions.projection``，由确定性回归测试约束。
"""

from .projection import (
    ASSISTANT_TURN_CHARS,
    DEFAULT_REFERENCE_COUNT,
    DEFAULT_TOTAL_TOKEN_BUDGET,
    EARLIER_BULLET_CHARS,
    FULL_MODE_TOKEN_CEILING,
    MAX_EARLIER_BULLETS,
    MIN_REFERENCE_TOKENS,
    MODE_FULL,
    MODE_RECENT,
    MODE_SUMMARY,
    MODE_TOKEN_LIMITS,
    RECENT_MODE_TURN_CHOICES,
    REFERENCE_KINDS,
    REFERENCE_MODES,
    SUMMARY_TURN_WINDOW,
    USER_TURN_CHARS,
    ReferenceProjection,
    ReferenceProjectionError,
    TurnView,
    project_reference,
    resolve_recent_turns,
)

__all__ = [
    "ASSISTANT_TURN_CHARS",
    "DEFAULT_REFERENCE_COUNT",
    "DEFAULT_TOTAL_TOKEN_BUDGET",
    "EARLIER_BULLET_CHARS",
    "FULL_MODE_TOKEN_CEILING",
    "MAX_EARLIER_BULLETS",
    "MIN_REFERENCE_TOKENS",
    "MODE_FULL",
    "MODE_RECENT",
    "MODE_SUMMARY",
    "MODE_TOKEN_LIMITS",
    "RECENT_MODE_TURN_CHOICES",
    "REFERENCE_KINDS",
    "REFERENCE_MODES",
    "SUMMARY_TURN_WINDOW",
    "USER_TURN_CHARS",
    "ReferenceProjection",
    "ReferenceProjectionError",
    "TurnView",
    "project_reference",
    "resolve_recent_turns",
]
