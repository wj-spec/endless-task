from __future__ import annotations

import asyncio
from typing import cast

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.tooling import (
    ToolApprovalMode as LegacyApprovalMode,
    ToolCall as LegacyToolCall,
    ToolCallStatus as LegacyToolCallStatus,
    ToolEffect as LegacyToolEffect,
    ToolError as LegacyToolError,
)
from endless_task.tooling.registry import RegisteredTool

from .protocol import (
    AgentToolV2,
    ApprovalPolicy,
    IdempotencyPolicy,
    RetryPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

_EFFECTS = {
    LegacyToolEffect.READ_ONLY: ToolEffect.READ_ONLY,
    LegacyToolEffect.LOCAL_WRITE: ToolEffect.LOCAL_WRITE,
    LegacyToolEffect.EXTERNAL_ACTION: ToolEffect.EXTERNAL_ACTION,
}
_APPROVALS = {
    LegacyApprovalMode.AUTO: ApprovalPolicy.AUTO,
    LegacyApprovalMode.REQUIRED: ApprovalPolicy.REQUIRED,
}


class LegacyToolAdapter:
    """Executes a v1 RegisteredTool through the v2 contract without changing behavior."""

    def __init__(self, tool: RegisteredTool) -> None:
        self._tool = tool
        definition = tool.definition
        effect = _EFFECTS[definition.effect]
        self.definition = ToolDefinitionV2(
            name=definition.name,
            description=definition.description,
            input_schema=definition.input_schema,
            output_schema=None,
            effect=effect,
            approval=_APPROVALS[definition.approval_mode],
            execution_mode=ToolExecutionMode.PARALLEL,
            idempotency=(
                IdempotencyPolicy.SAFE
                if effect is ToolEffect.READ_ONLY
                else IdempotencyPolicy.UNKNOWN
            ),
            retry_policy=RetryPolicy(max_attempts=1),
            required_capabilities=_legacy_capabilities(effect),
            timeout_seconds=definition.timeout_seconds,
            max_output_characters=definition.max_output_characters,
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        if request.tool_name != self.definition.name:
            raise AgentPlatformError(
                "tool_name_mismatch",
                "Tool request name does not match the adapted tool",
            )
        if request.legacy_turn_id is None or request.legacy_response_variant_id is None:
            raise AgentPlatformError(
                "legacy_context_required",
                "Legacy tool execution requires turn and response variant identifiers",
            )
        call = LegacyToolCall(
            id=request.call_id,
            conversation_id=request.conversation_id,
            turn_id=request.legacy_turn_id,
            response_variant_id=request.legacy_response_variant_id,
            tool_name=request.tool_name,
            arguments=request.arguments,
            status=LegacyToolCallStatus.CREATED,
            created_at=request.created_at,
        )
        try:
            result = await self._tool.execute(
                call,
                cast(CancellationToken, request.cancellation),
            )
        except (RuntimeCancelled, asyncio.CancelledError):
            raise
        except LegacyToolError as error:
            return ToolOutcome(
                call_id=request.call_id,
                status=ToolOutcomeStatus.FAILED,
                content=error.safe_message,
                diagnostic=SafeDiagnostic(
                    code=error.code,
                    safe_message=error.safe_message,
                    retryable=error.retryable,
                ),
            )
        except Exception:
            return ToolOutcome(
                call_id=request.call_id,
                status=ToolOutcomeStatus.FAILED,
                content="工具执行失败。",
                diagnostic=SafeDiagnostic(
                    code="legacy_tool_failure",
                    safe_message="工具执行失败。",
                    retryable=False,
                ),
            )

        if result.error is not None:
            diagnostic = SafeDiagnostic(
                code=result.error.code,
                safe_message=result.error.safe_message,
                retryable=result.error.retryable,
                details=result.error.details,
            )
            status = ToolOutcomeStatus.FAILED
        else:
            diagnostic = None
            status = ToolOutcomeStatus.COMPLETED
        return ToolOutcome(
            call_id=result.tool_call_id,
            status=status,
            content=result.content,
            structured_content=result.structured_content,
            diagnostic=diagnostic,
            is_truncated=result.is_truncated,
            terminate=result.terminate,
        )


def adapt_legacy_tool(tool: RegisteredTool) -> AgentToolV2:
    return LegacyToolAdapter(tool)


def _legacy_capabilities(effect: ToolEffect) -> frozenset[str]:
    if effect is ToolEffect.READ_ONLY:
        return frozenset()
    if effect is ToolEffect.LOCAL_WRITE:
        return frozenset({"workspace.write"})
    return frozenset({"external.action"})