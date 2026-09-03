"""Tool execution pipeline for the version 2 tool platform.

AP-104 (M1). Orchestrates one resolved tool batch through the §4.7 stage
chain implemented so far (protocol layer, no production wiring):

- input schema validation,
- ``before_tool`` ExtensionBus safety events (approval policy, deny,
  trusted argument replacement),
- scheduler admission honoring each tool's ``execution_mode`` with bounded
  concurrency and abort-before-dispatch,
- around-tool middleware chain around the tool executor,
- output schema validation,
- ``after_tool`` ExtensionBus safety events (accept / replace / block),
- fallback output spill when no spill extension is registered.

Every stage returns a structured ``ToolOutcome``; bare exceptions never cross
the pipeline boundary. Approval ``ask`` requires an injected handler —
without one, an approval question fails closed as ``rejected``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Protocol, Sequence

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    plain_json,
    require_protocol_version,
    require_text,
)
from endless_task.extensions import (
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
    InProcessExtensionBus,
)
from endless_task.runtime_ledger import TraceContext

from .middleware import ToolMiddleware, ToolNext, chain_tool_middleware
from .protocol import (
    TOOL_PROTOCOL_VERSION,
    AgentToolV2,
    ToolDefinitionV2,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)
from .scheduler import ScheduledCall, ToolScheduler
from .spill import spill_outcome

TOOL_PIPELINE_SCHEMA_VERSION = 1

#: Argument keys considered when deriving a canonical path for path-scoped
#: scheduling.
PATH_ARGUMENT_KEYS = ("path", "file_path", "directory", "workspace_path", "target")

_COMPLETED = ToolOutcomeStatus.COMPLETED
_CANCELLED = ToolOutcomeStatus.CANCELLED
_FAILED = ToolOutcomeStatus.FAILED
_REJECTED = ToolOutcomeStatus.REJECTED
_UNKNOWN = ToolOutcomeStatus.UNKNOWN


def canonical_path(arguments: Mapping[str, Any]) -> Optional[str]:
    """Canonical path of a tool call, if its arguments carry one."""
    for key in PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return None


@dataclass(frozen=True)
class ResolvedToolCall:
    """One capability-checked tool call ready for pipeline execution."""

    request: ToolExecutionRequest
    tool: AgentToolV2
    spill_reference: Optional[str] = None
    schema_version: int = TOOL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_PROTOCOL_VERSION,
            protocol="resolved_tool_call",
        )
        if not isinstance(self.request, ToolExecutionRequest):
            raise AgentPlatformError(
                "invalid_resolved_tool_call",
                "Resolved tool call requires a ToolExecutionRequest",
            )
        definition = getattr(self.tool, "definition", None)
        if not isinstance(definition, ToolDefinitionV2) or not callable(
            getattr(self.tool, "execute", None)
        ):
            raise AgentPlatformError(
                "invalid_resolved_tool_call",
                "Resolved tool call requires an AgentToolV2 implementation",
            )
        if self.request.tool_name != definition.name:
            raise AgentPlatformError(
                "invalid_resolved_tool_call",
                "Resolved tool call must match the tool definition name",
            )
        if self.spill_reference is not None:
            object.__setattr__(
                self,
                "spill_reference",
                require_text(
                    self.spill_reference,
                    field_name="spill_reference",
                    max_length=512,
                ),
            )


@dataclass(frozen=True)
class ToolBatchRequest:
    calls: tuple[ResolvedToolCall, ...]
    unattended: bool = False
    trace: Optional[TraceContext] = None
    schema_version: int = TOOL_PIPELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_PIPELINE_SCHEMA_VERSION,
            protocol="tool_batch_request",
        )
        if isinstance(self.calls, (str, bytes)) or not isinstance(self.calls, tuple):
            raise AgentPlatformError(
                "invalid_tool_batch_request",
                "Tool batch requires a tuple of ResolvedToolCall values",
            )
        if not self.calls or any(
            not isinstance(call, ResolvedToolCall) for call in self.calls
        ):
            raise AgentPlatformError(
                "invalid_tool_batch_request",
                "Tool batch requires at least one ResolvedToolCall",
            )
        if not isinstance(self.unattended, bool):
            raise AgentPlatformError(
                "invalid_tool_batch_request",
                "unattended must be boolean",
            )
        if self.trace is not None and not isinstance(self.trace, TraceContext):
            raise AgentPlatformError(
                "invalid_tool_batch_request",
                "trace must use the ledger TraceContext protocol type",
            )


@dataclass(frozen=True)
class ToolBatchResult:
    """Outcomes committed in the model's original call order."""

    outcomes: tuple[ToolOutcome, ...]
    schema_version: int = TOOL_PIPELINE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_PIPELINE_SCHEMA_VERSION,
            protocol="tool_batch_result",
        )
        if not isinstance(self.outcomes, tuple) or any(
            not isinstance(outcome, ToolOutcome) for outcome in self.outcomes
        ):
            raise AgentPlatformError(
                "invalid_tool_batch_result",
                "Tool batch result requires ToolOutcome values",
            )
        object.__setattr__(
            self,
            "outcomes",
            tuple(self.outcomes),
        )

    def by_call_id(self) -> Mapping[str, ToolOutcome]:
        return {outcome.call_id: outcome for outcome in self.outcomes}


class ApprovalHandler(Protocol):
    async def ask(
        self,
        request: ToolExecutionRequest,
        definition: ToolDefinitionV2,
        reason: str,
    ) -> bool: ...


class AroundToolHook(Protocol):
    async def around_tool(
        self,
        event: ExtensionEvent,
        next: ToolNext,
    ) -> ToolOutcome: ...


class ToolPipeline(Protocol):
    async def execute(self, batch: ToolBatchRequest) -> ToolBatchResult: ...


class _AroundMiddleware:
    """Adapts an ``AroundToolHook`` to the middleware chain contract."""

    def __init__(self, hook: AroundToolHook) -> None:
        self._hook = hook

    async def __call__(self, event: object, next: ToolNext) -> ToolOutcome:
        if not isinstance(event, ExtensionEvent):
            raise AgentPlatformError(
                "invalid_extension_event",
                "Around middleware requires an ExtensionEvent",
            )
        return await self._hook.around_tool(event, next)


class DefaultToolPipeline:
    """Conservative, composable pipeline over resolved tool calls."""

    def __init__(
        self,
        *,
        scheduler: Optional[ToolScheduler] = None,
        extension_bus: Optional[InProcessExtensionBus] = None,
        around_hooks: Sequence[AroundToolHook] = (),
        approval_handler: Optional[ApprovalHandler] = None,
        validate_output_schema: bool = True,
    ) -> None:
        self._scheduler = scheduler if scheduler is not None else ToolScheduler()
        if not isinstance(self._scheduler, ToolScheduler):
            raise AgentPlatformError(
                "invalid_scheduler_config",
                "Pipeline requires a ToolScheduler",
            )
        if extension_bus is not None and not isinstance(
            extension_bus, InProcessExtensionBus
        ):
            raise AgentPlatformError(
                "invalid_extension_bus",
                "Pipeline requires an InProcessExtensionBus",
            )
        self._bus = extension_bus
        hooks = tuple(around_hooks)
        if any(not callable(getattr(hook, "around_tool", None)) for hook in hooks):
            raise AgentPlatformError(
                "invalid_around_hooks",
                "Around hooks must implement around_tool(event, next)",
            )
        self._around_middlewares: tuple[ToolMiddleware, ...] = tuple(
            _AroundMiddleware(hook) for hook in hooks
        )
        if approval_handler is not None and not callable(
            getattr(approval_handler, "ask", None)
        ):
            raise AgentPlatformError(
                "invalid_approval_handler",
                "Approval handler must implement ask(request, definition, reason)",
            )
        self._approval_handler = approval_handler
        if not isinstance(validate_output_schema, bool):
            raise AgentPlatformError(
                "invalid_pipeline_config",
                "validate_output_schema must be boolean",
            )
        self._validate_output_schema = validate_output_schema

    async def execute(self, batch: ToolBatchRequest) -> ToolBatchResult:
        if not isinstance(batch, ToolBatchRequest):
            raise AgentPlatformError(
                "invalid_tool_batch_request",
                "Pipeline execution requires a ToolBatchRequest",
            )
        scheduled = tuple(
            ScheduledCall(
                index=index,
                call_id=call.request.call_id,
                tool_name=call.request.tool_name,
                mode=call.tool.definition.execution_mode,
                path=canonical_path(call.request.arguments),
                runner=lambda call=call: self._run_resolved(call, batch),
                cancellation=call.request.cancellation,
            )
            for index, call in enumerate(batch.calls)
        )
        outcomes = await self._scheduler.execute(scheduled)
        return ToolBatchResult(outcomes=outcomes)

    async def _run_resolved(
        self,
        call: ResolvedToolCall,
        batch: ToolBatchRequest,
    ) -> ToolOutcome:
        request = call.request
        definition = call.tool.definition
        try:
            _validate_schema_value(
                definition.input_schema,
                request.arguments,
                error_code="invalid_tool_arguments",
                message="工具参数不符合输入 schema。",
            )
        except AgentPlatformError as error:
            return _outcome_with_diagnostic(
                request.call_id,
                status=_FAILED,
                code=error.code,
                message=error.safe_message,
                details=dict(error.details),
            )

        arguments = request.arguments
        if self._bus is not None:
            decision = await self._before_tool(call, batch, arguments)
            if decision is not None:
                kind = decision.kind
                if kind is ExtensionDecisionKind.DENY:
                    return _outcome_with_diagnostic(
                        request.call_id,
                        status=_REJECTED,
                        code="denied_by_extension",
                        message="工具调用被扩展策略拒绝。",
                        details={"reason": decision.reason},
                    )
                if kind is ExtensionDecisionKind.ASK:
                    approved = await self._ask_approval(request, definition, decision)
                    if not approved:
                        return _outcome_with_diagnostic(
                            request.call_id,
                            status=_REJECTED,
                            code="approval_required",
                            message="工具调用需要用户批准。",
                        )
                elif kind is ExtensionDecisionKind.REPLACE_ARGUMENTS:
                    arguments = decision.replacement or request.arguments

        execution_request = (
            request
            if arguments is request.arguments
            else replace(request, arguments=arguments)
        )
        event = self._around_event(call, batch, execution_request)
        chain = chain_tool_middleware(
            self._around_middlewares,
            lambda: call.tool.execute(execution_request),
        )
        try:
            outcome = await chain(event)
        except asyncio.CancelledError:
            return _outcome_with_diagnostic(
                request.call_id,
                status=_CANCELLED,
                code="cancelled_during_execution",
                message="工具调用在执行中被取消。",
            )
        except Exception as error:
            return _outcome_with_diagnostic(
                request.call_id,
                status=_UNKNOWN,
                code="tool_execution_unknown",
                message="工具执行返回未知结果，副作用状态无法确认。",
                details={"error_type": type(error).__name__},
            )

        if (
            self._validate_output_schema
            and outcome.status is _COMPLETED
            and definition.output_schema is not None
            and outcome.structured_content is not None
        ):
            try:
                _validate_schema_value(
                    definition.output_schema,
                    outcome.structured_content,
                    error_code="output_schema_violation",
                    message="工具结构化结果不符合输出 schema。",
                )
            except AgentPlatformError as error:
                return _reported(outcome, error)

        if self._bus is not None:
            outcome = await self._after_tool(call, batch, outcome)

        return spill_outcome(
            outcome,
            max_characters=definition.max_output_characters,
            spill_reference=call.spill_reference,
        )

    async def _before_tool(
        self,
        call: ResolvedToolCall,
        batch: ToolBatchRequest,
        arguments: Mapping[str, Any],
    ) -> Optional[ExtensionDecision]:
        assert self._bus is not None
        result = await self._bus.dispatch(
            self._tool_event(
                call,
                batch,
                hook=ExtensionHook.BEFORE_TOOL,
                payload={
                    "tool_name": call.request.tool_name,
                    "call_id": call.request.call_id,
                    "approval_mode": call.tool.definition.approval.value,
                    "unattended": batch.unattended,
                    "arguments": dict(arguments),
                },
            )
        )
        return result.decision

    async def _after_tool(
        self,
        call: ResolvedToolCall,
        batch: ToolBatchRequest,
        outcome: ToolOutcome,
    ) -> ToolOutcome:
        assert self._bus is not None
        result = await self._bus.dispatch(
            self._tool_event(
                call,
                batch,
                hook=ExtensionHook.AFTER_TOOL,
                payload={
                    "tool_name": call.request.tool_name,
                    "call_id": call.request.call_id,
                    "status": outcome.status.value,
                    "content": outcome.content,
                    "structured_content": plain_json(outcome.structured_content),
                    "is_truncated": outcome.is_truncated,
                    "max_characters": call.tool.definition.max_output_characters,
                    "spill_reference": call.spill_reference,
                },
            )
        )
        decision = result.decision
        if decision is None or decision.kind is ExtensionDecisionKind.ACCEPT:
            return outcome
        if decision.kind is ExtensionDecisionKind.REPLACE_RESULT:
            replacement = decision.replacement
            if replacement is None or not isinstance(replacement.get("content"), str):
                return _outcome_with_diagnostic(
                    outcome.call_id,
                    status=_REJECTED,
                    code="invalid_result_replacement",
                    message="扩展返回的结果替换缺少 content。",
                )
            return replace(
                outcome,
                content=replacement["content"],
                structured_content=replacement.get(
                    "structured_content", outcome.structured_content
                ),
                is_truncated=bool(replacement.get("is_truncated", outcome.is_truncated)),
            )
        if decision.kind is ExtensionDecisionKind.BLOCK_RESULT:
            return _outcome_with_diagnostic(
                outcome.call_id,
                status=_REJECTED,
                code="result_blocked_by_extension",
                message="工具结果被扩展策略拦截。",
                details={"reason": decision.reason},
            )
        return outcome

    async def _ask_approval(
        self,
        request: ToolExecutionRequest,
        definition: ToolDefinitionV2,
        decision: ExtensionDecision,
    ) -> bool:
        if self._approval_handler is None:
            return False
        return await self._approval_handler.ask(
            request,
            definition,
            decision.reason,
        )

    def _tool_event(
        self,
        call: ResolvedToolCall,
        batch: ToolBatchRequest,
        *,
        hook: ExtensionHook,
        payload: Mapping[str, Any],
    ) -> ExtensionEvent:
        trace = batch.trace
        if trace is None:
            trace = TraceContext(
                trace_id=call.request.run_id,
                run_id=call.request.run_id,
                correlation_id=call.request.correlation_id,
            )
        return ExtensionEvent(
            event_id=f"tool_{hook.value}_{call.request.correlation_id}_{call.request.call_id}",
            hook=hook,
            mode=ExtensionEventMode.SAFETY_DECISION,
            trace=trace,
            payload=payload,
        )

    def _around_event(
        self,
        call: ResolvedToolCall,
        batch: ToolBatchRequest,
        request: ToolExecutionRequest,
    ) -> ExtensionEvent:
        trace = batch.trace
        if trace is None:
            trace = TraceContext(
                trace_id=request.run_id,
                run_id=request.run_id,
                correlation_id=request.correlation_id,
            )
        return ExtensionEvent(
            event_id=f"tool_around_{request.correlation_id}_{request.call_id}",
            hook=ExtensionHook.AROUND_TOOL,
            mode=ExtensionEventMode.OBSERVATION,
            trace=trace,
            payload={
                "tool_name": request.tool_name,
                "call_id": request.call_id,
                "arguments": dict(request.arguments),
            },
        )


def _validate_schema_value(
    schema: Mapping[str, Any],
    value: Any,
    *,
    error_code: str,
    message: str,
) -> None:
    try:
        Draft202012Validator(plain_json(schema)).validate(plain_json(value))
    except ValidationError as error:
        details = {}
        if error.validator is not None:
            details["keyword"] = str(error.validator)
        details["message"] = error.message
        raise AgentPlatformError(
            error_code,
            message,
            details=details,
        ) from error


def _outcome_with_diagnostic(
    call_id: str,
    *,
    status: ToolOutcomeStatus,
    code: str,
    message: str,
    details: Optional[Mapping[str, Any]] = None,
) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        status=status,
        content="",
        diagnostic=SafeDiagnostic(
            code=code,
            safe_message=message,
            retryable=False,
            details=dict(details or {}),
        ),
    )


def _reported(outcome: ToolOutcome, error: AgentPlatformError) -> ToolOutcome:
    return replace(
        outcome,
        status=_FAILED,
        diagnostic=SafeDiagnostic(
            code=error.code,
            safe_message=error.safe_message,
            retryable=False,
            details=dict(error.details),
        ),
        terminate=False,
    )


__all__ = [
    "PATH_ARGUMENT_KEYS",
    "TOOL_PIPELINE_SCHEMA_VERSION",
    "ApprovalHandler",
    "AroundToolHook",
    "DefaultToolPipeline",
    "ResolvedToolCall",
    "ToolBatchRequest",
    "ToolBatchResult",
    "ToolPipeline",
    "canonical_path",
]
