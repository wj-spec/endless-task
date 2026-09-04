"""v1-form delegation tools registered into the production ToolRegistry (DR-2).

The runtime-v2 execution path resolves every model tool as a v1
:class:`RegisteredTool` (``call.response_variant_id`` carries the run id,
same mechanism as ``update_plan``). These classes are the actual
registered surface for delegation: each maps the v1 :class:`ToolCall`
onto a :class:`ToolExecutionRequest` and delegates to the same
:class:`DelegationToolHandler` the v2 tools use, so argument validation
and outcome projection stay single-sourced in
:mod:`.agent_tools` / :mod:`.runtime_handler`.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import (
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolValidationError,
)

from .agent_tools import (
    CANCEL_AGENT_NAME,
    QUERY_AGENT_NAME,
    SPAWN_AGENT_NAME,
    DelegationToolHandler,
)

CorrelationResolver = Callable[[ToolCall], str]


def _default_correlation(call: ToolCall) -> str:
    # v2 run execution sets response_variant_id to the run id (same
    # convention update_plan relies on); without a separate correlation id
    # the run id is the stable correlation anchor.
    return call.response_variant_id


class _LegacyDelegationTool(RegisteredTool):
    """Shared v1->v2 bridge for the three delegation operations."""

    handler: DelegationToolHandler
    _definition: ToolDefinition
    _correlation_resolver: CorrelationResolver

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        if call.tool_name != self._definition.name:
            raise ToolValidationError(
                "tool_name_mismatch",
                f"Tool call name {call.tool_name!r} does not match {self._definition.name!r}",
            )
        request = ToolExecutionRequest(
            call_id=call.id,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            conversation_id=call.conversation_id,
            run_id=call.response_variant_id,
            model_turn_id=call.turn_id,
            correlation_id=self._correlation_resolver(call),
            cancellation=cancellation_token,
            created_at=call.created_at,
        )
        try:
            outcome = await self._dispatch(request)
        except Exception as error:
            return _result_from_outcome(
                _tool_failure_outcome(call.id, error),
                call.id,
            )
        return _result_from_outcome(outcome, call.id)

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        raise NotImplementedError


class SpawnAgentLegacyTool(_LegacyDelegationTool):
    """v1 registered form of ``spawn_agent``."""

    def __init__(
        self,
        handler: DelegationToolHandler,
        *,
        correlation_resolver: Optional[CorrelationResolver] = None,
        timeout_seconds: float = 30.0,
        max_output_characters: int = 20_000,
    ) -> None:
        if not callable(getattr(handler, "spawn_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "spawn_agent requires a delegation tool handler",
            )
        self.handler = handler
        self._correlation_resolver = correlation_resolver or _default_correlation
        self._definition = ToolDefinition(
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
                        "maxLength": 8_192,
                        "description": "子代理要执行的独立任务说明。",
                    },
                    "expectedOutputSchema": {
                        "type": "object",
                        "description": "子代理结构化结果的 JSON Schema（可选）。",
                    },
                    "timeoutSeconds": {
                        "type": "number",
                        "minimum": 1.0,
                        "maximum": 86_400.0,
                        "description": "子代理超时秒数，默认 120。",
                    },
                },
                "required": ["task"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        return await self.handler.spawn_child(request)


class QueryAgentLegacyTool(_LegacyDelegationTool):
    """v1 registered form of ``query_agent``."""

    def __init__(
        self,
        handler: DelegationToolHandler,
        *,
        correlation_resolver: Optional[CorrelationResolver] = None,
        timeout_seconds: float = 30.0,
        max_output_characters: int = 20_000,
    ) -> None:
        if not callable(getattr(handler, "query_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "query_agent requires a delegation tool handler",
            )
        self.handler = handler
        self._correlation_resolver = correlation_resolver or _default_correlation
        self._definition = ToolDefinition(
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
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        return await self.handler.query_child(request)


class CancelAgentLegacyTool(_LegacyDelegationTool):
    """v1 registered form of ``cancel_agent``."""

    def __init__(
        self,
        handler: DelegationToolHandler,
        *,
        correlation_resolver: Optional[CorrelationResolver] = None,
        timeout_seconds: float = 30.0,
        max_output_characters: int = 4_000,
    ) -> None:
        if not callable(getattr(handler, "cancel_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "cancel_agent requires a delegation tool handler",
            )
        self.handler = handler
        self._correlation_resolver = correlation_resolver or _default_correlation
        self._definition = ToolDefinition(
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
                        "maxLength": 512,
                        "description": "取消原因（可选）。",
                    },
                },
                "required": ["childRunId"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        return await self.handler.cancel_child(request)


def _tool_failure_outcome(call_id: str, error: Exception) -> ToolOutcome:
    if isinstance(error, AgentPlatformError):
        code = error.code
        message = error.safe_message
    else:
        code = "delegation_tool_error"
        message = "子代理工具执行出错。"
    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.FAILED,
        content=message,
        diagnostic=SafeDiagnostic(code=code, safe_message=message),
    )


def _result_from_outcome(outcome: ToolOutcome, call_id: str) -> ToolResult:
    if outcome.status is ToolOutcomeStatus.COMPLETED:
        return ToolResult(
            tool_call_id=call_id,
            content=outcome.content,
            structured_content=outcome.structured_content,
            is_truncated=outcome.is_truncated,
        )
    code = outcome.diagnostic.code if outcome.diagnostic is not None else "delegation_failed"
    message = (
        outcome.diagnostic.safe_message
        if outcome.diagnostic is not None
        else outcome.content
    )
    error = ToolCallError(
        code=code,
        safe_message=message,
        retryable=False,
    )
    return ToolResult(
        tool_call_id=call_id,
        content=message,
        error=error,
    )


__all__ = [
    "CancelAgentLegacyTool",
    "QueryAgentLegacyTool",
    "SpawnAgentLegacyTool",
]
