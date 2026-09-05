"""Model-callable delegation tools: spawn/query/cancel child agents (DR-2).

DR-2 registers ``spawn_agent / query_agent / cancel_agent`` so the main
agent can create and manage read-only child runs. Following the AP-106
``search_tools`` pattern, each tool is an :class:`AgentToolV2` that stays
declarative: it validates arguments, delegates to an injected
:class:`DelegationToolHandler`, and projects the structured result.

The handler seam is the composition-root bridge: it owns the per-parent
coordinator/scheduler wiring (child kernel factory, capability context,
idempotency keys). Tools never construct a coordinator themselves, so
tool_platform stays free of delegation/runtime imports and the tools are
directly testable with a fake handler.

M4A read-only gate: spawn arguments carry the child task and an optional
expected-output schema; capability/workspace policy is decided by the
handler (M4A = read-only children only), not by the tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    plain_json,
)
from endless_task.tool_platform.protocol import (
    AgentToolV2,
    IdempotencyPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

SPAWN_AGENT_NAME = "spawn_agent"
QUERY_AGENT_NAME = "query_agent"
CANCEL_AGENT_NAME = "cancel_agent"

_MAX_TASK_CHARACTERS = 8_192
_MAX_REASON_CHARACTERS = 512
_DEFAULT_TIMEOUT_SECONDS = 120.0
_MAX_TIMEOUT_SECONDS = 86_400.0


class DelegationToolHandler(Protocol):
    """Composition-root bridge between delegation tools and the runtime.

    Implementations own the coordinator/scheduler that actually executes
    child runs, so the tools stay declarative and independently testable.
    """

    async def spawn_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    async def query_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    async def cancel_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...


@dataclass(frozen=True)
class SpawnAgentTool(AgentToolV2):
    """Creates one read-only child agent and returns its run handle."""

    handler: DelegationToolHandler
    max_task_characters: int = _MAX_TASK_CHARACTERS
    timeout_seconds: float = 30.0
    max_output_characters: int = 20_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "spawn_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "spawn_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=SPAWN_AGENT_NAME,
                description=(
                    "创建 1 个只读子代理执行独立研究/检查任务，并返回 child_run_id 句柄；"
                    "子代理不继承父级写权限，结果可稍后用 query_agent 查询。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self.max_task_characters,
                            "description": "子代理要执行的独立任务说明。",
                        },
                        "expectedOutputSchema": {
                            "type": "object",
                            "description": "子代理结构化结果的 JSON Schema（可选）。",
                        },
                        "timeoutSeconds": {
                            "type": "number",
                            "minimum": 1.0,
                            "maximum": _MAX_TIMEOUT_SECONDS,
                            "description": "子代理超时秒数，默认 120。",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["readonly", "isolated_write"],
                            "description": (
                                "readonly（默认）= 只读子代理；isolated_write = M4B "
                                "隔离写子代理（仅在隔离 scratch 工作区写，不触碰主工作区）。"
                            ),
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.UNKNOWN,
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        task = request.arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            return _tool_failed(
                request.call_id,
                "invalid_delegation_task",
                "spawn_agent 的 task 必须是文本。",
            )
        schema = request.arguments.get("expectedOutputSchema")
        if schema is not None and not isinstance(schema, Mapping):
            return _tool_failed(
                request.call_id,
                "invalid_expected_output_schema",
                "expectedOutputSchema 必须是 JSON Schema 对象。",
            )
        timeout = request.arguments.get("timeoutSeconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 1.0 <= float(timeout) <= _MAX_TIMEOUT_SECONDS
        ):
            return _tool_failed(
                request.call_id,
                "invalid_delegation_timeout",
                "timeoutSeconds 必须在 1 到 86400 之间。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.spawn_child(request)


@dataclass(frozen=True)
class QueryAgentTool(AgentToolV2):
    """Queries one child run outcome by its handle."""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 20_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "query_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "query_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=QUERY_AGENT_NAME,
                description=(
                    "按 child_run_id 查询子代理的结构化结果（completed/failed/cancelled/"
                    "timeout/unknown）。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "childRunId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": "spawn_agent 返回的子代理运行句柄。",
                        },
                    },
                    "required": ["childRunId"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.SAFE,
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        child_run_id = request.arguments.get("childRunId")
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            return _tool_failed(
                request.call_id,
                "invalid_child_run_id",
                "query_agent 的 childRunId 必须是文本。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.query_child(request)


@dataclass(frozen=True)
class CancelAgentTool(AgentToolV2):
    """Cancels one child run by its handle."""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 4_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "cancel_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "cancel_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=CANCEL_AGENT_NAME,
                description="取消一个进行中的子代理运行。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "childRunId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": "要取消的子代理运行句柄。",
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_REASON_CHARACTERS,
                            "description": "取消原因（可选）。",
                        },
                    },
                    "required": ["childRunId"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.UNKNOWN,
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        child_run_id = request.arguments.get("childRunId")
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            return _tool_failed(
                request.call_id,
                "invalid_child_run_id",
                "cancel_agent 的 childRunId 必须是文本。",
            )
        reason = request.arguments.get("reason")
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip()
        ):
            return _tool_failed(
                request.call_id,
                "invalid_cancel_reason",
                "cancel_agent 的 reason 必须是文本。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.cancel_child(request)


def child_run_handle_json(
    child_run_id: str,
    status: str,
    summary: Optional[str] = None,
) -> Mapping[str, Any]:
    """Structured handle row shared by delegation tools and handlers."""
    payload: dict[str, Any] = {"childRunId": child_run_id, "status": status}
    if summary is not None:
        payload["summary"] = summary
    return payload


def _tool_failed(
    call_id: str,
    code: str,
    message: str,
) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.FAILED,
        content=message,
        diagnostic=SafeDiagnostic(code=code, safe_message=message),
    )


__all__ = [
    "CANCEL_AGENT_NAME",
    "QUERY_AGENT_NAME",
    "SPAWN_AGENT_NAME",
    "CancelAgentTool",
    "DelegationToolHandler",
    "QueryAgentTool",
    "SpawnAgentTool",
    "child_run_handle_json",
]
