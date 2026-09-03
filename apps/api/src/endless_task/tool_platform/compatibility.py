from __future__ import annotations

import asyncio
from typing import Optional, cast

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.tooling import (
    ToolCall as LegacyToolCall,
    ToolCallStatus as LegacyToolCallStatus,
    ToolError as LegacyToolError,
)
from endless_task.tooling.registry import RegisteredTool

from .legacy_policy import (
    LegacyToolPolicy,
    adapt_legacy_approval,
    adapt_legacy_effect,
    builtin_legacy_tool_policy,
)
from .protocol import (
    AgentToolV2,
    IdempotencyPolicy,
    RetryPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)


class LegacyToolAdapter:
    """Executes a v1 RegisteredTool through the v2 contract without changing behavior.

    Tool semantics not expressed by the v1 definition (execution mode,
    idempotency, capabilities) come from the audited built-in policy table
    (``legacy_policy.py``); unknown tools keep the conservative generic
    mapping.
    """

    def __init__(
        self,
        tool: RegisteredTool,
        *,
        policy: Optional[LegacyToolPolicy] = None,
    ) -> None:
        self._tool = tool
        definition = tool.definition
        effect = adapt_legacy_effect(definition.effect)
        resolved_policy = policy or builtin_legacy_tool_policy(definition.name)
        if resolved_policy is not None:
            execution_mode = resolved_policy.execution_mode
            idempotency = resolved_policy.idempotency
            required_capabilities = resolved_policy.required_capabilities
        else:
            execution_mode = ToolExecutionMode.PARALLEL
            idempotency = (
                IdempotencyPolicy.SAFE
                if effect is ToolEffect.READ_ONLY
                else IdempotencyPolicy.UNKNOWN
            )
            required_capabilities = _generic_capabilities(effect)
        self.definition = ToolDefinitionV2(
            name=definition.name,
            description=definition.description,
            input_schema=definition.input_schema,
            output_schema=None,
            effect=effect,
            approval=adapt_legacy_approval(definition.approval_mode),
            execution_mode=execution_mode,
            idempotency=idempotency,
            retry_policy=RetryPolicy(max_attempts=1),
            required_capabilities=required_capabilities,
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


def _generic_capabilities(effect: ToolEffect) -> frozenset[str]:
    if effect is ToolEffect.READ_ONLY:
        return frozenset()
    if effect is ToolEffect.LOCAL_WRITE:
        return frozenset({"workspace.write"})
    if effect is ToolEffect.EXTERNAL_ACTION:
        return frozenset({"external.action"})
    return frozenset()