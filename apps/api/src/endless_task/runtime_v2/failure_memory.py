"""C2 失败记忆：循环的"外部状态"里最容易被漏掉的一环。

《Agentic AI Guide》第 27 章「循环工程 — 外部状态：循环的记忆」把外部状态
拆成三件事：进度追踪、**失败记忆**（哪些方法试过并失败，防止循环在同一死胡同
里震荡）、交接上下文（升级人类时的审计轨迹）。本模块只做第二件：

* 以工具执行为最小单位，记录「试过什么（工具 + 参数指纹）、为什么失败（错误码 +
  安全文案）、第几次」；
* 只把**连续同因失败**升级为 ``RepeatedFailure``——成功一次即清零，避免把早已
  恢复的失败永远当成"卡住"；
* 提供 ``guidance()``，把失败记忆压成一小段可注入后续决策的文本（必须精简：
  只带最近、相关的失败，避免占用上下文）。

设计约束（与 ``reliability/stop.py`` 一致）：纯函数、无时钟、无 I/O，输入是
工具执行记录的序列，输出是不可变快照，便于单测与重放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

FAILURE_MEMORY_PROTOCOL_VERSION = 1

#: 同一（工具，错误码）连续失败达到该次数即视为"重复失败"。
DEFAULT_REPEAT_THRESHOLD = 2

#: ``guidance()`` 最多引用的失败种类数，避免提示词膨胀。
_MAX_GUIDANCE_ITEMS = 3

#: ``digest()``/``as_json()`` 保留的最近尝试条数上限。
_MAX_RETAINED_ATTEMPTS = 20


def _status_value(execution: object) -> Optional[str]:
    status = getattr(execution, "status", None)
    if status is None:
        return None
    value = getattr(status, "value", status)
    return str(value) if value is not None else None


def _string_attribute(execution: object, name: str) -> Optional[str]:
    value = getattr(execution, name, None)
    if value is None:
        return None
    text = str(value)
    return text or None


def _is_failed_execution(execution: object) -> bool:
    """失败尝试的判定：显式 ``failed`` 状态，或缺失状态但带错误码。"""
    status = _status_value(execution)
    if status is not None:
        return status == "failed"
    return _string_attribute(execution, "error_code") is not None


@dataclass(frozen=True)
class FailedAttempt:
    """一次失败尝试（工具 + 原因 + 第几次）。"""

    tool_name: str
    error_code: str
    safe_message: str
    attempt: int
    retryable: bool = False
    model_turn_id: Optional[str] = None
    tool_execution_id: Optional[str] = None
    arguments_digest: Optional[str] = None
    schema_version: int = FAILURE_MEMORY_PROTOCOL_VERSION

    def as_json(self) -> dict[str, object]:
        return {
            "toolName": self.tool_name,
            "errorCode": self.error_code,
            "safeMessage": self.safe_message,
            "attempt": self.attempt,
            "retryable": self.retryable,
            "modelTurnId": self.model_turn_id,
            "toolExecutionId": self.tool_execution_id,
            "argumentsDigest": self.arguments_digest,
        }


@dataclass(frozen=True)
class RepeatedFailure:
    """连续同因失败（成功一次即清零）。"""

    tool_name: str
    error_code: str
    count: int
    safe_message: str = ""
    schema_version: int = FAILURE_MEMORY_PROTOCOL_VERSION

    def as_json(self) -> dict[str, object]:
        return {
            "toolName": self.tool_name,
            "errorCode": self.error_code,
            "count": self.count,
            "safeMessage": self.safe_message,
        }


@dataclass(frozen=True)
class FailureMemory:
    """不可变失败记忆快照。"""

    attempts: tuple[FailedAttempt, ...] = ()
    repeated: tuple[RepeatedFailure, ...] = ()
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD

    @property
    def total(self) -> int:
        return len(self.attempts)

    @property
    def is_repeating(self) -> bool:
        return bool(self.repeated)

    def digest(self, *, limit: int = 5) -> tuple[dict[str, object], ...]:
        """最近 ``limit`` 条失败尝试（新→旧排序取尾部，保持时间顺序）。"""
        if limit <= 0:
            return ()
        return tuple(item.as_json() for item in self.attempts[-limit:])

    def guidance(self) -> str:
        """压缩成一小段可注入后续决策的失败记忆；无重复失败时为空串。"""
        if not self.repeated:
            return ""
        parts: list[str] = []
        for item in self.repeated[:_MAX_GUIDANCE_ITEMS]:
            detail = f"（{item.error_code}"
            if item.safe_message:
                detail += f"：{item.safe_message}"
            detail += "）"
            parts.append(f"`{item.tool_name}` 已连续失败 {item.count} 次{detail}")
        return (
            "失败记忆：" + "；".join(parts) + "。"
            "请更换方法或参数，不要原样重复同一调用。"
        )

    def as_json(self) -> dict[str, object]:
        return {
            "total": self.total,
            "repeated": tuple(item.as_json() for item in self.repeated),
            "attempts": tuple(
                item.as_json() for item in self.attempts[-_MAX_RETAINED_ATTEMPTS:]
            ),
        }


class FailureMemoryAccumulator:
    """按时间顺序累积失败尝试，并给出不可变快照。

    与批量构建（``build_failure_memory``）等价，但用于运行期增量记录：
    工具协调器每落库一条终态执行就 ``record()`` 一次，执行循环随时读取
    ``snapshot``，避免每轮重新扫描事件日志。
    """

    def __init__(self, *, repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD) -> None:
        if (
            not isinstance(repeat_threshold, int)
            or isinstance(repeat_threshold, bool)
            or repeat_threshold < 1
        ):
            raise AgentPlatformError(
                "invalid_failure_memory_threshold",
                "repeat_threshold must be a positive integer",
            )
        self._repeat_threshold = repeat_threshold
        self._attempts: list[FailedAttempt] = []
        self._streaks: dict[tuple[str, str], int] = {}

    @property
    def snapshot(self) -> FailureMemory:
        repeated = tuple(
            RepeatedFailure(
                tool_name=tool_name,
                error_code=error_code,
                count=count,
                safe_message=self._last_message(tool_name, error_code),
            )
            for (tool_name, error_code), count in self._streaks.items()
            if count >= self._repeat_threshold
        )
        return FailureMemory(
            attempts=tuple(self._attempts),
            repeated=repeated,
            repeat_threshold=self._repeat_threshold,
        )

    def record(self, execution: object) -> FailureMemory:
        """记录一条工具执行（失败计入记忆，成功清零同工具的连续失败）。"""
        status = _status_value(execution)
        if status == "completed":
            tool_name = _string_attribute(execution, "tool_name") or "tool"
            for key in [key for key in self._streaks if key[0] == tool_name]:
                del self._streaks[key]
            return self.snapshot
        if not _is_failed_execution(execution):
            return self.snapshot
        tool_name = _string_attribute(execution, "tool_name") or "tool"
        error_code = _string_attribute(execution, "error_code") or "tool_error"
        key = (tool_name, error_code)
        attempt = self._streaks.get(key, 0) + 1
        self._streaks[key] = attempt
        self._attempts.append(
            FailedAttempt(
                tool_name=tool_name,
                error_code=error_code,
                safe_message=_string_attribute(execution, "safe_message") or "",
                attempt=attempt,
                retryable=bool(getattr(execution, "retryable", False)),
                model_turn_id=_string_attribute(execution, "model_turn_id"),
                tool_execution_id=_string_attribute(execution, "id"),
                arguments_digest=_string_attribute(execution, "arguments_hash"),
            )
        )
        return self.snapshot

    def restore(self, executions: Iterable[object]) -> FailureMemory:
        """从历史执行记录重建（崩溃恢复 / 重放路径）。"""
        for execution in executions:
            self.record(execution)
        return self.snapshot

    def _last_message(self, tool_name: str, error_code: str) -> str:
        for attempt in reversed(self._attempts):
            if attempt.tool_name == tool_name and attempt.error_code == error_code:
                return attempt.safe_message
        return ""


def build_failure_memory(
    executions: Sequence[object] | Iterable[object],
    *,
    repeat_threshold: int = DEFAULT_REPEAT_THRESHOLD,
) -> FailureMemory:
    """从按时间排序的工具执行记录构建失败记忆。"""
    if isinstance(executions, (str, bytes)) or isinstance(executions, Mapping):
        raise AgentPlatformError(
            "invalid_failure_memory_input",
            "executions must be a sequence of tool execution records",
        )
    return FailureMemoryAccumulator(repeat_threshold=repeat_threshold).restore(
        executions
    )


__all__ = [
    "DEFAULT_REPEAT_THRESHOLD",
    "FAILURE_MEMORY_PROTOCOL_VERSION",
    "FailedAttempt",
    "FailureMemory",
    "FailureMemoryAccumulator",
    "RepeatedFailure",
    "build_failure_memory",
]
