from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SafetyStopReason(str, Enum):
    # 保留的终止原因：由模型/工具/用户决定，而非启发式计数条件。
    TOOL_TERMINATE = "tool_terminate"
    NO_PROGRESS = "no_progress"
    AGENT_TIMEOUT = "agent_timeout"
    TOOL_CALL_LIMIT = "tool_call_limit_exceeded"
    TOOL_ARGUMENT_LIMIT = "tool_argument_limit_exceeded"


class SafetyStopError(RuntimeError):
    def __init__(self, reason: SafetyStopReason, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.code = reason.value
        self.safe_message = safe_message


@dataclass
class SafetyStopState:
    """与 pi 对齐：无计数式安全状态。

    循环终止完全由「模型产出无工具调用的最终回答」「工具主动 terminate」「用户取消」
    决定，不依赖任何轮次/失败/只读计数。该类保留空结构以维持调用方兼容。
    """


@dataclass(frozen=True)
class SafetyStopPolicy:
    """与 pi 对齐：无计数式安全策略。

    pi 的 agent loop 不设连续失败/空回/只读不交付/重复调用等计数条件，循环以
    while-true 运行、由模型自行收敛；仅保留工具超时、用户取消与工具 terminate。
    本类为空，仅维持接口兼容。
    """
