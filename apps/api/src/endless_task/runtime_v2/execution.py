from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING

from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from endless_task.tooling import (
    ContextualTool,
    JsonValue,
    ProgressReportingTool,
    RegisteredTool,
    ToolCall,
    ToolCallError,
    ToolCallStatus,
    ToolError,
    ToolExecutionContext,
    ToolProgressEvent,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
)
from endless_task.tooling.schema import ToolSchemaError, validate_tool_arguments

from .context import ContextProjection
from .domain import (
    ModelTurnRecord,
    ModelTurnStatus,
    RunRecord,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from .escalation import (
    BUDGET_REASON,
    DEFAULT_BUDGET_RATIO,
    NO_PROGRESS_REASON,
    VERIFICATION_REASON,
    EscalationBudget,
    EscalationProgress,
    build_escalation_report,
    budget_exhausted,
)
from .failure_memory import FailureMemoryAccumulator
from .verification import (
    VERIFICATION_PROTOCOL_VERSION,
    VerificationVerdict,
    build_verifier_messages,
    parse_verdict,
    should_verify,
)
from .safety import SafetyStopError, SafetyStopReason
from .metrics import ModelTurnMetric, RunMetric, RuntimeV2MetricsCollector
from .trace_observer import MIRROR_TIMEOUT_SECONDS, RunTraceObserver
# runtime_ledger span protocol types (imported by path; no storage at top).
from endless_task.runtime_ledger.protocol import (
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)

if TYPE_CHECKING:
    from endless_task.storage.sqlite_runtime_v2_repository import (
        SqliteRuntimeV2Repository,
    )


logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )

_MODEL_TURN_WAITING_SLOT_STATUS = ModelTurnStatus.WAITING_PROVIDER_SLOT
_MODEL_TURN_STREAMING_STATUS = ModelTurnStatus.STREAMING
_MODEL_TURN_EXECUTING_STATUS = ModelTurnStatus.EXECUTING_TOOLS
_MODEL_TURN_COMPLETED_STATUS = ModelTurnStatus.COMPLETED
_MODEL_TURN_FAILED_STATUS = ModelTurnStatus.FAILED
_MODEL_TURN_CANCELLED_STATUS = ModelTurnStatus.CANCELLED
_TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.CANCELLED,
        ToolExecutionStatus.REJECTED,
        ToolExecutionStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class ToolExecutionLimits:
    max_calls_per_turn: int = 8
    max_concurrent_calls: int = 4
    max_argument_bytes: int = 64 * 1024
    max_total_argument_bytes: int = 256 * 1024
    max_argument_depth: int = 32
    max_argument_nodes: int = 10_000

    def __post_init__(self) -> None:
        for field_name in (
            "max_calls_per_turn",
            "max_concurrent_calls",
            "max_argument_bytes",
            "max_total_argument_bytes",
            "max_argument_depth",
            "max_argument_nodes",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if self.max_argument_bytes > self.max_total_argument_bytes:
            raise ValueError(
                "max_argument_bytes cannot exceed max_total_argument_bytes"
            )


def _json_value_size(value: Any, *, limits: ToolExecutionLimits) -> int:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_argument_nodes:
            raise SafetyStopError(
                SafetyStopReason.TOOL_ARGUMENT_LIMIT,
                "工具参数结构过于复杂，已安全停止。",
            )
        if depth > limits.max_argument_depth:
            raise SafetyStopError(
                SafetyStopReason.TOOL_ARGUMENT_LIMIT,
                "工具参数嵌套层级过深，已安全停止。",
            )
        if isinstance(current, Mapping):
            stack.extend((nested, depth + 1) for nested in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((nested, depth + 1) for nested in current)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise SafetyStopError(
            SafetyStopReason.TOOL_ARGUMENT_LIMIT,
            "工具参数不是受支持的 JSON 数据，已安全停止。",
        ) from error
    return len(encoded)


class ToolApprovalDecision(str, Enum):
    WAIT = "wait"
    APPROVE = "approve"
    DENY = "deny"
    EXPIRE = "expired"
    MODIFY = "modify"


class ToolApprovalGate(Protocol):
    async def decide(
        self,
        execution: ToolExecutionRecord,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolApprovalDecision:
        ...


@dataclass(frozen=True)
class ContextCompactionResult:
    messages: tuple[ProviderMessage, ...]
    summary_entry_id: Optional[str] = None
    covered_entry_ids: tuple[str, ...] = ()


class ContextCompactionHook(Protocol):
    async def compact(
        self,
        run: RunRecord,
        messages: Sequence[ProviderMessage],
    ) -> Optional[ContextCompactionResult]:
        ...


class WaitingToolApprovalGate:
    async def decide(
        self,
        execution,
        tool,
        call,
        cancellation_token,
    ) -> ToolApprovalDecision:
        del execution, tool, call, cancellation_token
        return ToolApprovalDecision.WAIT

    def modified_arguments_for(self, approval_id: str):
        del approval_id
        return None


class StaticToolApprovalGate:
    def __init__(self, decision: ToolApprovalDecision) -> None:
        self._decision = decision

    async def decide(
        self,
        execution,
        tool,
        call,
        cancellation_token,
    ) -> ToolApprovalDecision:
        del execution, tool, call, cancellation_token
        return self._decision


class UnattendedToolApprovalGate:
    """Fail closed when no interactive approval channel exists."""

    async def decide(
        self,
        execution,
        tool,
        call,
        cancellation_token,
    ) -> ToolApprovalDecision:
        del execution, tool, call, cancellation_token
        return ToolApprovalDecision.DENY


@dataclass(frozen=True)
class ToolExecutionOutcome:
    messages: tuple[ProviderMessage, ...]
    records: tuple[ToolExecutionRecord, ...] = ()
    pending_approval_execution_ids: tuple[str, ...] = ()
    terminal_execution_ids: tuple[str, ...] = ()
    # 本批是否应提前终止循环：所有成功工具结果都请求 terminate（pi 的 terminate 语义）。
    terminate: bool = False


@dataclass
class _ToolWorkItem:
    provider_call: ProviderToolCall
    record_id: str
    status: ToolExecutionStatus = ToolExecutionStatus.CREATED
    content: str = ""
    structured_content: JsonValue = None
    error: Optional[ToolCallError] = None
    succeeded: bool = False
    pending: bool = False
    terminate: bool = False
    result_recorded: bool = False


@dataclass(frozen=True)
class _PreparedToolExecution:
    item: _ToolWorkItem
    tool: RegisteredTool
    call: ToolCall
    needs_approval: bool


def _messages_fingerprint(messages) -> str:
    """Stable sha256 over model-turn messages (M2 prelude context signal)."""
    rows = []
    for message in messages:
        rows.append(
            {
                "role": message.role,
                "content": message.content,
                "tool_call_ids": [
                    call.id
                    for call in (getattr(message, "tool_calls", ()) or ())
                ],
            }
        )
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()



def _no_progress_evaluation_for_run(*, repository, run_id: str):
    """Build and evaluate per-turn evidence for one run (shadow/enforcement)."""
    import hashlib

    from endless_task.reliability.stop import StopPolicyProfile, evaluate_no_progress
    from endless_task.reliability.stop_runtime import TurnEvidence, build_turn_signal

    turns = repository.list_model_turns(run_id)
    if not turns:
        return None
    events = repository.list_runtime_events(run_id)
    fingerprints = [
        getattr(event, "payload", {}).get("fingerprint")
        for event in events
        if getattr(event, "event_type", None) == "context_fingerprint"
    ]
    evidences = []
    for index, turn in enumerate(turns):
        fingerprint = fingerprints[index] if index < len(fingerprints) else "unavailable"
        executions = repository.list_tool_executions(turn.id)
        tool_name = None
        outcome_fingerprint = None
        unresolved = None
        if executions:
            tool_name = getattr(executions[0], "tool_name", None) or "tool"
            contents = []
            for execution in executions:
                status = getattr(execution, "status", None)
                status_value = getattr(status, "value", status)
                # ToolExecutionRecord 的错误是扁平字段（error_code/safe_message），
                # 早先按嵌套 error 对象读取，导致 repeated_failure 检测永不触发。
                error_code = getattr(execution, "error_code", None)
                if error_code:
                    unresolved = error_code
                    continue
                if status_value in ("completed", "COMPLETED"):
                    contents.append(getattr(execution, "content", "") or "")
            if contents:
                outcome_fingerprint = hashlib.sha256(
                    "".join(sorted(contents)).encode("utf-8")
                ).hexdigest()
        evidences.append(
            TurnEvidence(
                context_fingerprint=str(fingerprint),
                tool_signature=tool_name,
                tool_outcome_fingerprint=outcome_fingerprint,
                unresolved_error=unresolved,
            )
        )
    signals = tuple(build_turn_signal(evidence) for evidence in evidences)
    return evaluate_no_progress(signals, StopPolicyProfile())


def _evaluate_no_progress_shadow(*, repository, run_id: str, observer) -> None:
    """M2 prelude Stage 2: evaluate per-turn evidence (observation only)."""
    evaluation = _no_progress_evaluation_for_run(repository=repository, run_id=run_id)
    if evaluation is not None:
        observer(evaluation)

class ToolExecutionCoordinator:
    """Executes one model-turn tool batch while preserving source order."""

    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        tool_registry: ToolRegistry,
        approval_gate: Optional[ToolApprovalGate] = None,
        tool_filter_provider: Optional[
            Callable[[str], Optional[Callable[[str], bool]]]
        ] = None,
        tool_definitions_provider: Optional[
            Callable[[str], tuple[ProviderToolDefinition, ...]]
        ] = None,
        v2_pipeline_enabled: bool = False,
        limits: Optional[ToolExecutionLimits] = None,
        protocol_receipt_sink=None,
        failure_memory: Optional[FailureMemoryAccumulator] = None,
    ) -> None:
        self._repository = repository
        self._tool_registry = tool_registry
        self._approval_gate = approval_gate or WaitingToolApprovalGate()
        # C2 失败记忆：循环的外部状态，逐条累积失败尝试（None = 关闭）。
        self._failure_memory = failure_memory
        # RS-6 slice 2b: optional async sink(dict protocol-receipt fields,
        # TraceContext) receiving completed tool side effects so they land
        # in the runtime ledger as safety-critical records. None = off.
        self._protocol_receipt_sink = protocol_receipt_sink
        self._tool_filter_provider = tool_filter_provider
        # AP-107 dual-path seam: when provided (feature flag on) the model
        # surface comes from the version-2 planner instead of the legacy
        # predicate; the default None keeps the legacy path byte-identical.
        self._tool_definitions_provider = tool_definitions_provider
        # AP-107 Stage 4c: when enabled, the auto-approved subset of each
        # batch runs through the version-2 pipeline (scheduler + adapters);
        # approval-needed items keep the legacy WAIT/resume path.
        if not isinstance(v2_pipeline_enabled, bool):
            raise ValueError("v2_pipeline_enabled must be boolean")
        self._v2_pipeline_enabled = v2_pipeline_enabled
        self._limits = limits or ToolExecutionLimits()
        self._execution_slots = asyncio.Semaphore(self._limits.max_concurrent_calls)

    def definitions(self, conversation_id: str) -> tuple[ProviderToolDefinition, ...]:
        if self._tool_definitions_provider is not None:
            return self._tool_definitions_provider(conversation_id)
        predicate = (
            self._tool_filter_provider(conversation_id)
            if self._tool_filter_provider is not None
            else None
        )
        return tuple(
            ProviderToolDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )
            for definition in self._tool_registry.definitions()
            if predicate is None or predicate(definition.name)
        )

    @staticmethod
    def _model_failure_content(error: ToolCallError) -> str:
        details: list[str] = []
        if error.path is not None:
            details.append(f"位置：{error.path}")
        if error.keyword is not None:
            details.append(f"规则：{error.keyword}")
        if error.expected is not None:
            expected_value = (
                dict(error.expected)
                if isinstance(error.expected, Mapping)
                else error.expected
            )
            expected = json.dumps(
                expected_value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            if len(expected) > 512:
                expected = expected[:511] + "…"
            details.append(f"期望：{expected}")
        detail_text = f" {'；'.join(details)}。" if details else ""
        recovery = (
            "请根据错误信息调整参数后重试。"
            if error.retryable
            else "请不要原样重复同一调用。"
        )
        return (
            f"工具执行失败（{error.code}）：{error.safe_message}"
            f"{detail_text} {recovery}"
        )

    @classmethod
    def _fail_item(
        cls,
        item: _ToolWorkItem,
        error: ToolCallError,
        *,
        status: ToolExecutionStatus = ToolExecutionStatus.FAILED,
        content: Optional[str] = None,
    ) -> None:
        item.status = status
        item.error = error
        item.content = content or cls._model_failure_content(error)

    async def execute(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        provider_calls: Sequence[ProviderToolCall],
        assistant_content: str,
        cancellation_token: CancellationToken,
    ) -> ToolExecutionOutcome:
        if len(provider_calls) > self._limits.max_calls_per_turn:
            raise SafetyStopError(
                SafetyStopReason.TOOL_CALL_LIMIT,
                "模型单轮请求的工具数量超过限制，已安全停止。",
            )

        argument_sizes = [
            _json_value_size(provider_call.arguments, limits=self._limits)
            for provider_call in provider_calls
        ]
        if any(
            size > self._limits.max_argument_bytes for size in argument_sizes
        ) or sum(argument_sizes) > self._limits.max_total_argument_bytes:
            raise SafetyStopError(
                SafetyStopReason.TOOL_ARGUMENT_LIMIT,
                "模型返回的工具参数超过大小限制，已安全停止。",
            )

        cancellation_token.raise_if_cancelled()

        items: list[_ToolWorkItem] = []
        for provider_call in provider_calls:
            record, _entry = self._repository.record_tool_call(
                model_turn_id=model_turn.id,
                call_id=provider_call.id,
                tool_name=provider_call.name,
                arguments=provider_call.arguments,
            )
            items.append(
                _ToolWorkItem(
                    provider_call=provider_call,
                    record_id=record.id,
                )
            )

        try:
            prepared_items = self._prepare_tool_batch(
                run=run,
                model_turn=model_turn,
                items=items,
                cancellation_token=cancellation_token,
            )
            async with asyncio.TaskGroup() as task_group:
                if self._v2_pipeline_enabled:
                    v2_auto = tuple(
                        prepared
                        for prepared in prepared_items
                        if not prepared.needs_approval
                    )
                    if v2_auto:
                        task_group.create_task(
                            self._execute_v2_auto_batch(
                                run=run,
                                model_turn=model_turn,
                                prepared_items=v2_auto,
                                cancellation_token=cancellation_token,
                            )
                        )
                    legacy_items = tuple(
                        prepared
                        for prepared in prepared_items
                        if prepared.needs_approval
                    )
                else:
                    legacy_items = prepared_items
                for prepared_item in legacy_items:
                    task_group.create_task(
                        self._execute_prepared_item(
                            run=run,
                            model_turn=model_turn,
                            prepared=prepared_item,
                            cancellation_token=cancellation_token,
                        )
                    )
        except BaseException as error:
            if self._is_runtime_cancellation(error):
                self._record_interrupted_items(
                    items,
                    error=ToolCallError(
                        code="cancelled",
                        safe_message="工具执行已被取消。",
                        retryable=False,
                    ),
                    status=ToolExecutionStatus.CANCELLED,
                    content="工具执行已被用户取消。",
                )
                raise RuntimeCancelled() from error
            if self._contains_asyncio_cancellation(error):
                self._record_interrupted_items(
                    items,
                    error=ToolCallError(
                        code="cancelled",
                        safe_message="工具执行已被取消。",
                        retryable=False,
                    ),
                    status=ToolExecutionStatus.CANCELLED,
                    content="工具执行已被取消。",
                )
                raise
            self._record_interrupted_items(
                items,
                error=ToolCallError(
                    code="tool_batch_failed",
                    safe_message="工具批次执行出现内部错误。",
                    retryable=False,
                    correlation_id=run.correlation_id,
                ),
                status=ToolExecutionStatus.FAILED,
                content="工具批次执行出现内部错误。",
            )
            raise

        terminal_ids: list[str] = []
        pending_ids: list[str] = []
        result_messages: list[ProviderMessage] = [] if not provider_calls else [
            ProviderMessage(
                role="assistant",
                content=assistant_content,
                tool_calls=tuple(provider_calls),
            )
        ]

        for item in items:
            if item.pending:
                pending_ids.append(item.record_id)
                continue
            event_type = self._result_event(item.status)
            record, _result_entry = self._repository.record_tool_result(
                item.record_id,
                status=item.status,
                content=item.content,
                event_type=event_type,
                error=item.error,
                structured_content=item.structured_content,
            )
            if self._failure_memory is not None:
                # C2：失败尝试进入外部状态（成功一次即清零该工具的连续失败）。
                self._failure_memory.record(record)
            if self._protocol_receipt_sink is not None:
                await self._emit_protocol_receipt(
                    run=run,
                    item=item,
                )
            item.result_recorded = True
            terminal_ids.append(record.id)
            result_messages.append(
                ProviderMessage(
                    role="tool",
                    content=item.content,
                    tool_call_id=item.provider_call.id,
                    name=item.provider_call.name,
                )
            )

        terminal_items = [item for item in items if not item.pending]
        # terminate 语义（pi shouldTerminateToolBatch）：
        # 存在已终结工具结果，且全部请求 terminate → 提前终止循环。
        request_terminate = bool(terminal_items) and all(
            item.terminate for item in terminal_items
        )
        return ToolExecutionOutcome(
            messages=tuple(result_messages),
            records=tuple(
                self._repository.get_tool_execution(item.record_id)
                for item in items
            ),
            pending_approval_execution_ids=tuple(pending_ids),
            terminal_execution_ids=tuple(terminal_ids),
            terminate=request_terminate,
        )

    async def _emit_protocol_receipt(
        self,
        *,
        run: RunRecord,
        item: _ToolWorkItem,
    ) -> None:
        """Forward a completed tool's workspace effect to the protocol sink.

        Reads ``structured_content["effect"]`` (the workspace receipt the
        tool embedded), maps it to protocol EffectReceipt fields, and hands
        both to the sink with a run-scoped trace. Fail-open: observability
        must not change the tool result already recorded above.
        """
        if item.status is not ToolExecutionStatus.COMPLETED:
            return
        structured = item.structured_content
        if not isinstance(structured, Mapping):
            return
        effect = structured.get("effect")
        if not isinstance(effect, Mapping):
            return
        try:
            from endless_task.workspace_runtime.effect_log import (
                EffectReceipt as WorkspaceReceipt,
            )
            from endless_task.workspace_runtime.effect_protocol import (
                to_protocol_fields,
            )
            from endless_task.runtime_ledger.protocol import TraceContext

            workspace_receipt = WorkspaceReceipt(
                kind=str(effect.get("kind", "")),
                path=str(effect.get("path", "")),
                sha256=str(effect.get("sha256", "")),
                executed_at=str(effect.get("executedAt", "")),
                exit_code=effect.get("exitCode"),
                timed_out=bool(effect.get("timedOut", False)),
                truncated=bool(effect.get("truncated", False)),
                unknown_outcome=bool(effect.get("unknownOutcome", False)),
            )
            fields = to_protocol_fields(
                workspace_receipt,
                effect_id=item.provider_call.id,
                tool_call_id=item.provider_call.id,
                backend="workspace_tool",
            )
            trace = TraceContext(
                trace_id=run.correlation_id or run.id,
                run_id=run.id,
                correlation_id=run.correlation_id or run.id,
                span_id=item.record_id,
                tool_execution_id=item.record_id,
            )
            await self._protocol_receipt_sink(fields, trace)
        except Exception:
            # Best-effort protocol audit; the workspace audit log (and the
            # tool result just persisted) remain authoritative.
            return

    async def _execute_v2_auto_batch(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        prepared_items: Sequence["_PreparedToolExecution"],
        cancellation_token: CancellationToken,
    ) -> None:
        """Run the auto-approved subset through the v2 pipeline.

        Execution of each tool still goes through the same registered tool
        via ``LegacyToolAdapter``; per-item repository transitions mirror the
        legacy runner so results, events and replay stay identical.
        """
        from endless_task.tool_platform import (
            DefaultToolPipeline,
            LegacyToolAdapter,
            ResolvedToolCall,
            ToolBatchRequest,
            ToolExecutionRequest,
            ToolScheduler,
        )

        for prepared in prepared_items:
            self._repository.transition_tool_execution_status(
                prepared.item.record_id,
                ToolExecutionStatus.RUNNING,
                event_type="tool_execution_started",
                payload={"toolExecutionId": prepared.item.record_id},
            )
            prepared.item.status = ToolExecutionStatus.RUNNING
        calls: list[ResolvedToolCall] = []
        for prepared in prepared_items:
            call = self._tool_call(prepared.item, run, model_turn)
            adapter = LegacyToolAdapter(prepared.tool)
            request = ToolExecutionRequest(
                call_id=call.id,
                tool_name=call.tool_name,
                arguments=call.arguments,
                conversation_id=call.conversation_id,
                run_id=run.id,
                model_turn_id=model_turn.id,
                correlation_id=run.correlation_id or run.id,
                cancellation=cancellation_token,
                created_at=call.created_at,
                legacy_turn_id=call.turn_id,
                legacy_response_variant_id=call.response_variant_id,
            )
            calls.append(ResolvedToolCall(request=request, tool=adapter))
        pipeline = DefaultToolPipeline(
            scheduler=ToolScheduler(
                max_concurrent=self._limits.max_concurrent_calls
            )
        )
        result = await pipeline.execute(ToolBatchRequest(calls=tuple(calls)))
        for prepared, outcome in zip(prepared_items, result.outcomes):
            self._apply_v2_outcome(prepared.item, outcome)

    def _apply_v2_outcome(
        self,
        item: _ToolWorkItem,
        outcome: ToolOutcome,
    ) -> None:
        """Mirror a v2 pipeline outcome onto a legacy work item."""
        status_value = outcome.status.value
        if status_value == "completed":
            item.status = ToolExecutionStatus.COMPLETED
            item.content = outcome.content
            item.structured_content = outcome.structured_content
            item.error = None
            item.terminate = outcome.terminate
            return
        diagnostic = outcome.diagnostic
        code = diagnostic.code if diagnostic is not None else "tool_execution_unknown"
        safe_message = (
            diagnostic.safe_message
            if diagnostic is not None
            else "工具执行返回未知结果，副作用状态无法确认。"
        )
        retryable = diagnostic.retryable if diagnostic is not None else False
        if status_value == "cancelled":
            self._fail_item(
                item,
                ToolCallError(
                    code=code,
                    safe_message=safe_message,
                    retryable=retryable,
                ),
                status=ToolExecutionStatus.CANCELLED,
            )
            return
        self._fail_item(
            item,
            ToolCallError(
                code=code,
                safe_message=safe_message,
                retryable=retryable,
            ),
            status=(
                ToolExecutionStatus.REJECTED
                if status_value == "rejected"
                else ToolExecutionStatus.FAILED
            ),
        )

    def _prepare_tool_batch(

        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        items: Sequence[_ToolWorkItem],
        cancellation_token: CancellationToken,
    ) -> list[_PreparedToolExecution]:
        prepared_items: list[_PreparedToolExecution] = []
        for item in items:
            self._repository.transition_tool_execution_status(
                item.record_id,
                ToolExecutionStatus.VALIDATING,
                event_type="tool_execution_status_changed",
                payload={
                    "toolExecutionId": item.record_id,
                    "status": ToolExecutionStatus.VALIDATING.value,
                    "callId": item.provider_call.id,
                    "toolName": item.provider_call.name,
                    "arguments": item.provider_call.arguments,
                },
            )
            cancellation_token.raise_if_cancelled()
            try:
                if item.provider_call.parse_error:
                    self._fail_item(
                        item,
                        ToolCallError(
                            code="invalid_tool_arguments",
                            safe_message=item.provider_call.parse_error,
                            retryable=True,
                            path="$",
                            keyword="json",
                            expected="JSON object",
                        ),
                    )
                    continue
                tool = self._resolve_tool(item.provider_call.name)
                call = self._tool_call(item, run, model_turn)
                self._validate_arguments(tool, item.provider_call.arguments)
                force_confirm = False
                confirmation_judge = getattr(
                    tool, "requires_explicit_confirmation", None
                )
                if callable(confirmation_judge):
                    try:
                        force_confirm = bool(confirmation_judge(call))
                    except Exception:
                        force_confirm = True
                needs_approval = (
                    tool.definition.approval_mode.value == "required"
                    or force_confirm
                )
            except ToolValidationError as error:
                self._fail_item(
                    item,
                    ToolCallError(
                        code=error.code,
                        safe_message="模型提供的工具调用或参数不符合要求。",
                        retryable=error.retryable,
                        path=error.path,
                        keyword=error.keyword,
                        expected=error.expected,
                    ),
                )
                continue
            prepared_items.append(
                _PreparedToolExecution(
                    item=item,
                    tool=tool,
                    call=call,
                    needs_approval=needs_approval,
                )
            )
        return prepared_items

    def _record_interrupted_items(
        self,
        items: Sequence[_ToolWorkItem],
        *,
        error: ToolCallError,
        status: ToolExecutionStatus,
        content: str,
    ) -> None:
        for item in items:
            if item.result_recorded:
                continue
            current = self._repository.get_tool_execution(item.record_id)
            if (
                current.status in _TERMINAL_TOOL_STATUSES
                or current.result_entry_id is not None
            ):
                item.result_recorded = True
                continue
            if item.status not in _TERMINAL_TOOL_STATUSES:
                self._fail_item(
                    item,
                    error,
                    status=status,
                    content=content,
                )
                item.pending = False
            self._repository.record_tool_result(
                item.record_id,
                status=item.status,
                content=item.content,
                event_type=self._result_event(item.status),
                error=item.error,
                structured_content=item.structured_content,
            )
            item.result_recorded = True

    @classmethod
    def _is_runtime_cancellation(cls, error: BaseException) -> bool:
        leaf_errors = tuple(cls._leaf_exceptions(error))
        return bool(leaf_errors) and all(
            isinstance(leaf, RuntimeCancelled) for leaf in leaf_errors
        )

    @classmethod
    def _contains_asyncio_cancellation(cls, error: BaseException) -> bool:
        return any(
            isinstance(leaf, asyncio.CancelledError)
            for leaf in cls._leaf_exceptions(error)
        )

    @classmethod
    def _leaf_exceptions(cls, error: BaseException):
        if isinstance(error, BaseExceptionGroup):
            for nested in error.exceptions:
                yield from cls._leaf_exceptions(nested)
            return
        yield error

    async def _execute_prepared_item(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        prepared: _PreparedToolExecution,
        cancellation_token: CancellationToken,
    ) -> None:
        item = prepared.item
        tool = prepared.tool
        call = prepared.call
        if prepared.needs_approval:
            decision = await self._approval_gate.decide(
                self._repository.get_tool_execution(item.record_id),
                tool,
                call,
                cancellation_token,
            )
            if decision is ToolApprovalDecision.WAIT:
                self._repository.transition_tool_execution_status(
                    item.record_id,
                    ToolExecutionStatus.WAITING_APPROVAL,
                    event_type="tool_execution_status_changed",
                    payload={
                        "toolExecutionId": item.record_id,
                        "status": ToolExecutionStatus.WAITING_APPROVAL.value,
                    },
                )
                item.status = ToolExecutionStatus.WAITING_APPROVAL
                item.pending = True
                return
            if decision is ToolApprovalDecision.DENY:
                self._fail_item(
                    item,
                    ToolCallError(
                        code="approval_denied",
                        safe_message="用户未授权这项操作。",
                        retryable=False,
                    ),
                    status=ToolExecutionStatus.REJECTED,
                    content=(
                        "用户未授权这项操作。不要重复请求相同操作；"
                        "请说明未执行，或在无需该操作的情况下继续。"
                    ),
                )
                return
            if decision is ToolApprovalDecision.EXPIRE:
                self._fail_item(
                    item,
                    ToolCallError(
                        code="approval_timeout",
                        safe_message="等待用户授权已超时。",
                        retryable=False,
                    ),
                    status=ToolExecutionStatus.EXPIRED,
                    content=(
                        "等待用户授权已超时，本次工具调用未执行。"
                        "不要原样重复请求相同操作；请说明未执行，或等待用户重新发起。"
                    ),
                )
                return
            if decision is ToolApprovalDecision.MODIFY:
                modified = self._approval_gate.modified_arguments_for(
                    item.record_id,
                )
                if modified is None:
                    self._fail_item(
                        item,
                        ToolCallError(
                            code="invalid_modified_arguments",
                            safe_message="用户修改后的参数为空。",
                            retryable=False,
                        ),
                        status=ToolExecutionStatus.REJECTED,
                        content=(
                            "用户修改后的参数为空，未执行该操作。请说明未执行。"
                        ),
                    )
                    return
                try:
                    self._validate_arguments(tool, modified)
                except ToolValidationError as error:
                    self._fail_item(
                        item,
                        ToolCallError(
                            code=error.code,
                            safe_message="用户修改后的参数不符合要求。",
                            retryable=error.retryable,
                        ),
                        status=ToolExecutionStatus.REJECTED,
                        content=(
                            "用户修改后的参数不符合要求，未执行该操作。"
                            "请说明未执行或改用其它方式。"
                        ),
                    )
                    return
                # A1-modify：写回修正参数（重放一致），并用修正参数构建新的 call。
                self._repository.update_tool_execution_arguments(
                    item.record_id,
                    modified,
                )
                call = ToolCall(
                    id=call.id,
                    conversation_id=call.conversation_id,
                    turn_id=call.turn_id,
                    response_variant_id=call.response_variant_id,
                    tool_name=call.tool_name,
                    arguments=modified,
                    status=call.status,
                    created_at=call.created_at,
                )

        async with self._execution_slots:
            await self._execute_approved_item(
                run=run,
                model_turn=model_turn,
                item=item,
                tool=tool,
                call=call,
                cancellation_token=cancellation_token,
            )

    async def _execute_approved_item(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        item: _ToolWorkItem,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> None:
        self._repository.transition_tool_execution_status(
            item.record_id,
            ToolExecutionStatus.RUNNING,
            event_type="tool_execution_started",
            payload={"toolExecutionId": item.record_id},
        )
        item.status = ToolExecutionStatus.RUNNING
        try:
            result = await self._execute_tool_with_progress(
                run=run,
                model_turn=model_turn,
                tool=tool,
                call=call,
                cancellation_token=cancellation_token,
                item=item,
            )
        except RuntimeCancelled:
            self._fail_item(
                item,
                ToolCallError(
                    code="cancelled",
                    safe_message="工具执行已被取消。",
                    retryable=False,
                ),
                status=ToolExecutionStatus.CANCELLED,
                content="工具执行已被用户取消。",
            )
            raise
        except ToolError as error:
            self._fail_item(item, error.failure)
            return
        except Exception:
            logger.exception(
                "Tool execution raised an unexpected error",
                extra={
                    "tool_name": item.provider_call.name,
                    "tool_execution_id": item.record_id,
                },
            )
            self._fail_item(
                item,
                ToolCallError(
                    code="tool_execution_failed",
                    safe_message="工具执行出现内部错误。",
                    retryable=False,
                    correlation_id=run.correlation_id,
                ),
            )
            return

        if result.error is not None:
            self._fail_item(item, result.error)
            return

        bounded_result = self._bounded_result(
            result,
            max_characters=tool.definition.max_output_characters,
        )
        item.status = ToolExecutionStatus.COMPLETED
        item.content = bounded_result.content
        item.structured_content = bounded_result.structured_content
        item.succeeded = True
        item.terminate = bounded_result.terminate

    def _resolve_tool(self, name: str) -> RegisteredTool:
        return self._tool_registry.resolve(name)

    @staticmethod
    def _tool_call(
        item: _ToolWorkItem,
        run: RunRecord,
        model_turn: ModelTurnRecord,
    ) -> ToolCall:
        return ToolCall(
            id=item.provider_call.id,
            conversation_id=run.conversation_id,
            turn_id=model_turn.id,
            response_variant_id=model_turn.run_id,
            tool_name=item.provider_call.name,
            arguments=item.provider_call.arguments,
            status=ToolCallStatus.RUNNING,
            created_at=model_turn.created_at,
        )

    @staticmethod
    def _validate_arguments(tool: RegisteredTool, arguments) -> None:
        try:
            validate_tool_arguments(tool.definition.input_schema, arguments)
        except ToolSchemaError as error:
            raise ToolValidationError(
                "invalid_tool_arguments",
                "模型提供的工具参数不符合要求。",
                retryable=True,
                path=error.path,
                keyword=error.keyword,
                expected=error.expected,
            ) from error

    async def _execute_tool_with_progress(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
        item: _ToolWorkItem,
    ) -> ToolResult:
        timeout_seconds = tool.definition.timeout_seconds
        context = self._tool_execution_context(
            run=run,
            model_turn=model_turn,
            item=item,
            cancellation_token=cancellation_token,
            timeout_seconds=timeout_seconds,
        )
        if isinstance(tool, ContextualTool):
            return await self._await_tool_result(
                tool.execute_with_context(call, context),
                call,
                cancellation_token,
                timeout_seconds=timeout_seconds,
            )
        if isinstance(tool, ProgressReportingTool):

            def on_progress(message: str, percent: Optional[float] = None) -> None:
                self._append_tool_progress_event(
                    run=run,
                    model_turn=model_turn,
                    item=item,
                    event=ToolProgressEvent(message=message, percent=percent),
                )

            return await self._await_tool_result(
                tool.execute_with_progress(
                    call,
                    cancellation_token,
                    on_progress=on_progress,
                ),
                call,
                cancellation_token,
                timeout_seconds=timeout_seconds,
            )
        return await self._execute_tool(
            tool,
            call,
            cancellation_token,
            timeout_seconds=timeout_seconds,
        )

    def _tool_execution_context(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        item: _ToolWorkItem,
        cancellation_token: CancellationToken,
        timeout_seconds: float,
    ) -> ToolExecutionContext:
        async def report_progress(event: ToolProgressEvent) -> None:
            self._append_tool_progress_event(
                run=run,
                model_turn=model_turn,
                item=item,
                event=event,
            )

        return ToolExecutionContext(
            cancellation_token=cancellation_token,
            deadline_monotonic=time.monotonic() + timeout_seconds,
            correlation_id=item.record_id,
            progress_reporter=report_progress,
        )

    def _append_tool_progress_event(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        item: _ToolWorkItem,
        event: ToolProgressEvent,
    ) -> None:
        self._repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=model_turn.id,
            event_type="tool_progress_update",
            payload={
                "toolExecutionId": item.record_id,
                "message": event.message,
                "percent": event.percent,
            },
        )

    @staticmethod
    async def _execute_tool(
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        return await ToolExecutionCoordinator._await_tool_result(
            tool.execute(call, cancellation_token),
            call,
            cancellation_token,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    async def _await_tool_result(
        awaitable: Awaitable[ToolResult],
        call: ToolCall,
        cancellation_token: CancellationToken,
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        execution = asyncio.ensure_future(awaitable)
        cancellation = asyncio.create_task(cancellation_token.wait())
        try:
            done, _pending = await asyncio.wait(
                (execution, cancellation),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                raise RuntimeCancelled()
            if execution not in done:
                raise ToolError(
                    "tool_timeout",
                    "工具执行超时，可以重试。",
                    retryable=True,
                )
            result = execution.result()
            if not isinstance(result, ToolResult) or result.tool_call_id != call.id:
                raise ToolError(
                    "invalid_tool_result",
                    "工具返回了无效结果。",
                    retryable=False,
                )
            return result
        finally:
            for task in (execution, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, cancellation, return_exceptions=True)

    @staticmethod
    def _bounded_result(result: ToolResult, *, max_characters: int) -> ToolResult:
        if len(result.content) <= max_characters:
            return result
        return ToolResult(
            tool_call_id=result.tool_call_id,
            content=result.content[:max_characters],
            structured_content=result.structured_content,
            is_truncated=True,
            terminate=result.terminate,
            error=result.error,
        )

    @staticmethod
    def _result_event(status: ToolExecutionStatus) -> str:
        if status is ToolExecutionStatus.COMPLETED:
            return "tool_execution_completed"
        if status is ToolExecutionStatus.FAILED:
            return "tool_execution_failed"
        if status is ToolExecutionStatus.CANCELLED:
            return "tool_execution_cancelled"
        if status is ToolExecutionStatus.REJECTED:
            return "tool_execution_rejected"
        if status is ToolExecutionStatus.EXPIRED:
            return "tool_execution_expired"
        return "tool_execution_status_changed"


@dataclass(frozen=True)
class ModelTurnOutcome:
    model_turn_id: str
    content: str
    finish_reason: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    messages: tuple[ProviderMessage, ...]
    tool_calls: tuple[ProviderToolCall, ...]
    pending_approval_execution_ids: tuple[str, ...]
    # 本批工具结果请求提前终止循环（pi terminate 语义）。
    terminate: bool = False


class ModelTurnRunner:
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_coordinator: ToolExecutionCoordinator,
        model: str,
        max_output_tokens: int,
        temperature: Optional[float] = None,
        provider_slot: Optional[asyncio.Semaphore] = None,
        metrics: Optional["RuntimeV2MetricsCollector"] = None,
        provider_retry_evaluator: Optional[object] = None,
        provider_retry_observer: Optional[object] = None,
        span_recorder: Optional[object] = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._tool_coordinator = tool_coordinator
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._provider_slot = provider_slot
        self._metrics = metrics
        # M6 W6-7: optional trace-span recorder (RuntimeLedger-shaped:
        # start_span/end). None = byte-identical legacy behavior.
        self._span_recorder = span_recorder
        # M3A RS-1: optional provider retry orchestration (default off =
        # legacy behavior; shadow observers may still be attached).
        self._provider_retry_evaluator = provider_retry_evaluator
        self._provider_retry_observer = provider_retry_observer
        self._last_first_event_at: Optional[float] = None
        self._last_span_ended = False

    def _open_model_turn_span(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        turn_started: float,
    ):
        """Open a MODEL span for one model turn when a recorder is wired.

        Returns a :class:`SpanHandle` or ``None`` (no recorder / no
        end-to-end trace context available). The handle's ``end`` is
        idempotent, so multiple terminal transitions stay safe.
        """
        recorder = self._span_recorder
        if recorder is None or not callable(getattr(recorder, "start_span", None)):
            return None
        trace_id = run.correlation_id or run.id
        started_at = model_turn.started_at or _iso_now()
        try:
            return recorder.start_span(
                SpanSpec(
                    trace=TraceContext(
                        trace_id=trace_id,
                        run_id=run.id,
                        correlation_id=trace_id,
                        span_id=model_turn.id,
                        model_turn_id=model_turn.id,
                    ),
                    kind=SpanKind.MODEL,
                    name=f"model_turn:{model_turn.turn_index}",
                    started_at=started_at,
                    monotonic_started=turn_started,
                    attributes={
                        "provider": self._provider.name,
                        "model": self._model,
                        "gen_ai.system": self._provider.name,
                        "gen_ai.model.name": self._model,
                        "gen_ai.operation.name": "generate",
                    },
                )
            )
        except Exception:
            logger.exception(
                "Failed to open model-turn trace span",
                extra={"model_turn_id": model_turn.id},
            )
            return None

    async def run(
        self,
        *,
        run: RunRecord,
        messages: Sequence[ProviderMessage],
        cancellation_token: CancellationToken,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> ModelTurnOutcome:
        model_turn = self._repository.start_model_turn(
            run.id,
            provider=self._provider.name,
            model=self._model,
        )
        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_WAITING_SLOT_STATUS,
            event_type="model_turn_status_changed",
            payload={"status": _MODEL_TURN_WAITING_SLOT_STATUS.value},
        )

        span_handle = self._open_model_turn_span(
            run=run,
            model_turn=model_turn,
            turn_started=time.monotonic(),
        )

        provider_calls: list[ProviderToolCall] = []
        completion: Optional[ProviderCompleted] = None
        content_parts: list[str] = []
        request_id = f"{run.id}:{model_turn.turn_index}"
        turn_started = time.monotonic()

        def _record_turn_metric(
            *,
            finish_reason: str,
            input_tokens: Optional[int],
            output_tokens: Optional[int],
        ) -> None:
            if self._metrics is None:
                return
            first_latency: Optional[int] = None
            if self._last_first_event_at is not None:
                first_latency = int(
                    (self._last_first_event_at - turn_started) * 1000
                )
            self._metrics.record_model_turn(
                ModelTurnMetric(
                    run_id=run.id,
                    turn_index=model_turn.turn_index,
                    first_token_latency_ms=first_latency,
                    duration_ms=int((time.monotonic() - turn_started) * 1000),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    finish_reason=finish_reason,
                )
            )

        async def _close_turn_span(status_value: str) -> None:
            if span_handle is None:
                return
            handle = span_handle
            try:
                await handle.end(
                    SpanStatus(status_value),
                    ended_at=_iso_now(),
                    monotonic_ended=time.monotonic(),
                )
            except Exception:
                logger.exception(
                    "Failed to close model-turn trace span",
                    extra={"model_turn_id": model_turn.id},
                )

        request = ProviderRequest(
            request_id=request_id,
            model=self._model,
            messages=tuple(messages),
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            tools=self._tool_coordinator.definitions(run.conversation_id),
        )
        if self._metrics is not None:
            fingerprint = _messages_fingerprint(messages)
            self._metrics.record_prefix_fingerprint(
                conversation_id=run.conversation_id,
                fingerprint=fingerprint,
            )
            # Durable per-turn fingerprint event (M2 prelude Stage 2) so the
            # no-progress shadow evaluation can read it back per run.
            self._repository.append_runtime_event(
                run_id=run.id,
                model_turn_id=model_turn.id,
                event_type="context_fingerprint",
                payload={"fingerprint": fingerprint},
            )

        try:
            if self._provider_slot is not None:
                async with self._provider_slot:
                    completion = await self._consume_provider(
                        run=run,
                        model_turn=model_turn,
                        request=request,
                        cancellation_token=cancellation_token,
                        provider_calls=provider_calls,
                        content_parts=content_parts,
                        on_text_delta=on_text_delta,
                        turn_started=turn_started,
                    )
            else:
                completion = await self._consume_provider(
                    run=run,
                    model_turn=model_turn,
                    request=request,
                    cancellation_token=cancellation_token,
                    provider_calls=provider_calls,
                    content_parts=content_parts,
                    on_text_delta=on_text_delta,
                    turn_started=turn_started,
                )
        except RuntimeCancelled:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_CANCELLED_STATUS,
                event_type="model_turn_cancelled",
            )
            await _close_turn_span("cancelled")
            raise
        except asyncio.CancelledError:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_CANCELLED_STATUS,
                event_type="model_turn_cancelled",
            )
            await _close_turn_span("cancelled")
            raise
        except BaseException as error:
            error_code = "provider_failed"
            safe_message = "模型执行失败。"
            if isinstance(error, ProviderError):
                error_code = error.code
                safe_message = error.safe_message
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code=error_code,
                safe_message=safe_message,
            )
            await _close_turn_span("failed")
            raise

        content = "".join(content_parts)

        if completion is None:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code="invalid_provider_response",
                safe_message="模型未返回完成事件。",
            )
            await _close_turn_span("failed")
            raise ProviderError(
                "invalid_provider_response",
                "模型未返回完成事件。",
                retryable=False,
            )
        if completion.finish_reason == "tool_calls" and not provider_calls:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code="invalid_provider_response",
                safe_message="模型声明工具调用但未返回调用内容。",
            )
            await _close_turn_span("failed")
            raise ProviderError(
                "invalid_provider_response",
                "模型声明工具调用但未返回调用内容。",
                retryable=False,
            )

        if provider_calls:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_EXECUTING_STATUS,
                event_type="model_turn_status_changed",
                payload={"status": _MODEL_TURN_EXECUTING_STATUS.value},
            )
            try:
                tool_outcome = await self._tool_coordinator.execute(
                    run=run,
                    model_turn=model_turn,
                    provider_calls=tuple(provider_calls),
                    assistant_content=content,
                    cancellation_token=cancellation_token,
                )
            except RuntimeCancelled:
                self._repository.transition_model_turn_status(
                    model_turn.id,
                    _MODEL_TURN_CANCELLED_STATUS,
                    event_type="model_turn_cancelled",
                )
                await _close_turn_span("cancelled")
                raise
            except asyncio.CancelledError:
                self._repository.transition_model_turn_status(
                    model_turn.id,
                    _MODEL_TURN_CANCELLED_STATUS,
                    event_type="model_turn_cancelled",
                )
                await _close_turn_span("cancelled")
                raise
            except BaseException:
                self._repository.transition_model_turn_status(
                    model_turn.id,
                    _MODEL_TURN_FAILED_STATUS,
                    event_type="model_turn_failed",
                    error_code="tool_execution_failed",
                    safe_message="工具执行未能继续。",
                )
                await _close_turn_span("failed")
                raise
            if tool_outcome.pending_approval_execution_ids:
                self._repository.transition_run_status(
                    run.id,
                    RunStatus.WAITING_APPROVAL,
                    event_type="run_status_changed",
                    payload={"status": RunStatus.WAITING_APPROVAL.value},
                )
                _record_turn_metric(
                    finish_reason=completion.finish_reason,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                )
                await _close_turn_span("completed")
                return ModelTurnOutcome(
                    model_turn_id=model_turn.id,
                    content=content,
                    finish_reason=completion.finish_reason,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    messages=(),
                    tool_calls=tuple(provider_calls),
                    pending_approval_execution_ids=(
                        tool_outcome.pending_approval_execution_ids
                    ),
                )
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_COMPLETED_STATUS,
                event_type="model_turn_completed",
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            _record_turn_metric(
                finish_reason=completion.finish_reason,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            await _close_turn_span("completed")
            return ModelTurnOutcome(
                model_turn_id=model_turn.id,
                content=content,
                finish_reason=completion.finish_reason,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                messages=tool_outcome.messages,
                tool_calls=tuple(provider_calls),
                pending_approval_execution_ids=(),
                terminate=tool_outcome.terminate,
            )

        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_COMPLETED_STATUS,
            event_type="model_turn_completed",
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        _record_turn_metric(
            finish_reason=completion.finish_reason,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        await _close_turn_span("completed")
        return ModelTurnOutcome(
            model_turn_id=model_turn.id,
            content=content,
            finish_reason=completion.finish_reason,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            messages=(),
            tool_calls=(),
            pending_approval_execution_ids=(),
        )

    async def _consume_provider(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
        provider_calls: list[ProviderToolCall],
        content_parts: list[str],
        on_text_delta: Optional[Callable[[str], Awaitable[None]]],
        turn_started: float,
    ) -> Optional[ProviderCompleted]:
        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_STREAMING_STATUS,
            event_type="model_turn_started",
        )
        evaluator = self._provider_retry_evaluator
        observer = self._provider_retry_observer
        attempts_used = 0
        while True:
            first_event_at: Optional[float] = None
            normal_end = False
            try:
                async for provider_event in self._provider.stream(
                    request,
                    cancellation_token,
                ):
                    cancellation_token.raise_if_cancelled()
                    if first_event_at is None:
                        first_event_at = time.monotonic()
                        self._last_first_event_at = first_event_at
                    if isinstance(provider_event, ProviderTextDelta):
                        if not provider_event.text:
                            continue
                        content_parts.append(provider_event.text)
                        self._repository.append_runtime_event(
                            run_id=run.id,
                            model_turn_id=model_turn.id,
                            event_type="model_text_delta",
                            payload={"delta": provider_event.text},
                        )
                        if on_text_delta is not None:
                            await on_text_delta(provider_event.text)
                        continue
                    if isinstance(provider_event, ProviderToolCall):
                        provider_calls.append(provider_event)
                        continue
                    if isinstance(provider_event, ProviderCompleted):
                        return provider_event
                normal_end = True
            except ProviderError as error:
                if evaluator is not None:
                    record = evaluator.evaluate(
                        error_code=error.code,
                        attempts_used=attempts_used,
                        emitted_events=first_event_at is not None,
                        retry_after_ms=error.retry_after_ms,
                    )
                    if observer is not None:
                        observer(record)
                    if record.would_retry:
                        attempts_used += 1
                        await asyncio.sleep(record.delay_seconds)
                        cancellation_token.raise_if_cancelled()
                        continue
                raise
            if normal_end:
                return None


@dataclass(frozen=True)
class RunExecutionResult:
    status: RunStatus
    run: RunRecord
    content: str
    input_tokens: int
    output_tokens: int
    model_turn_ids: tuple[str, ...]
    pending_approval_execution_ids: tuple[str, ...]
    follow_up_messages: tuple[str, ...] = ()
    assistant_entry_id: Optional[str] = None


class AgentRunExecutor:
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        model: str,
        max_output_tokens: int = 8192,
        temperature: Optional[float] = None,
        approval_gate: Optional[ToolApprovalGate] = None,
        provider_slot: Optional[asyncio.Semaphore] = None,
        compaction_hook: Optional[ContextCompactionHook] = None,
        context_prefix_messages: Sequence[ProviderMessage] = (),
        tool_filter_provider: Optional[
            Callable[[str], Optional[Callable[[str], bool]]]
        ] = None,
        tool_definitions_provider: Optional[
            Callable[[str], tuple[ProviderToolDefinition, ...]]
        ] = None,
        v2_pipeline_enabled: bool = False,
        context_engine_v2_enabled: bool = False,
        tool_execution_limits: Optional[ToolExecutionLimits] = None,
        metrics: Optional["RuntimeV2MetricsCollector"] = None,
        provider_retry_evaluator: Optional[object] = None,
        provider_retry_observer: Optional[object] = None,
        no_progress_observer: Optional[object] = None,
        no_progress_enforcement_enabled: bool = False,
        escalation_budget_ratio: float = DEFAULT_BUDGET_RATIO,
        verifier_mode: str = "0",
        verifier_model: Optional[str] = None,
        context_shadow_observer: Optional[object] = None,
        context_window_tokens: Optional[int] = None,
        agent_timeout_seconds: Optional[float] = None,
        trace_observer: Optional[RunTraceObserver] = None,
        span_recorder: Optional[object] = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._tool_registry = tool_registry
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._provider_slot = provider_slot
        self._compaction_hook = compaction_hook
        self._agent_timeout_seconds = agent_timeout_seconds
        self._context_prefix_messages = tuple(context_prefix_messages)
        self._metrics = metrics
        self._run_compacted = False
        self._provider_retry_evaluator = provider_retry_evaluator
        self._provider_retry_observer = provider_retry_observer
        self._no_progress_observer = no_progress_observer
        self._no_progress_enforcement_enabled = bool(no_progress_enforcement_enabled)
        # C2 失败记忆：本次运行的失败尝试（外部状态），随工具执行增量累积。
        self._failure_memory = FailureMemoryAccumulator()
        # 已经就"哪一组重复失败"提示过模型 / 发过 run.stuck 事件。
        self._stuck_signature: Optional[tuple[tuple[str, str, str], ...]] = None
        self._stuck_emitted = False
        # 已注入过失败记忆的（工具，错误码）组合，避免每轮重复注入。
        self._injected_failure_keys: set[tuple[str, str]] = set()
        # 已通过 no-progress 评估发出的卡住级别（避免每轮重复发事件）。
        self._no_progress_stuck_level: Optional[str] = None
        # C4 升级：预算阈值与已升级原因（同一原因只提请注意一次）。
        self._escalation_budget_ratio = float(escalation_budget_ratio)
        self._escalated_reasons: set[str] = set()
        # C1 制造者—检查者分离：独立验证模式（0 关 / 1 全部 / side_effects 仅关键运行）。
        self._verifier_mode = verifier_mode
        self._verifier_model = verifier_model
        self._context_shadow_observer = context_shadow_observer
        self._context_window_tokens = context_window_tokens
        self._context_engine_v2_enabled = context_engine_v2_enabled
        self._trace_observer = trace_observer
        self._span_recorder = span_recorder
        protocol_receipt_sink = None
        if self._span_recorder is not None and callable(
            getattr(self._span_recorder, "record_effect", None)
        ):
            # RS-6 slice 2b: completed tool side effects land in the
            # runtime ledger as safety-critical records (only when trace
            # wiring is on; default off = current behavior).
            from .effect_sink import build_record_effect_sink

            protocol_receipt_sink = build_record_effect_sink(self._span_recorder)
        self._tool_coordinator = ToolExecutionCoordinator(
            repository=repository,
            tool_registry=tool_registry,
            approval_gate=approval_gate,
            tool_filter_provider=tool_filter_provider,
            tool_definitions_provider=tool_definitions_provider,
            v2_pipeline_enabled=v2_pipeline_enabled,
            limits=tool_execution_limits,
            protocol_receipt_sink=protocol_receipt_sink,
            failure_memory=self._failure_memory,
        )
        self._model_turn_runner = ModelTurnRunner(
            repository=repository,
            provider=provider,
            tool_coordinator=self._tool_coordinator,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            provider_slot=provider_slot,
            metrics=metrics,
            provider_retry_evaluator=self._provider_retry_evaluator,
            provider_retry_observer=self._provider_retry_observer,
            span_recorder=self._span_recorder,
        )
        self._context_projection = ContextProjection()
        self._steering_messages: list[str] = []
        self._follow_up_messages: list[str] = []
        self._queue_lock = asyncio.Lock()
        self._active_run_id: Optional[str] = None
        self._cancellation_token: Optional[CancellationToken] = None

    def _open_run_span(self, run: RunRecord, *, monotonic_started: float):
        """Open a run-level INTERNAL span when a recorder is wired."""
        recorder = self._span_recorder
        if recorder is None or not callable(getattr(recorder, "start_span", None)):
            return None
        trace_id = run.correlation_id or run.id
        try:
            return recorder.start_span(
                SpanSpec(
                    trace=TraceContext(
                        trace_id=trace_id,
                        run_id=run.id,
                        correlation_id=trace_id,
                        span_id=run.id,
                    ),
                    kind=SpanKind.INTERNAL,
                    name=f"run:{run.conversation_id}",
                    started_at=run.started_at or _iso_now(),
                    monotonic_started=monotonic_started,
                    attributes={},
                )
            )
        except Exception:
            logger.exception(
                "Failed to open run trace span",
                extra={"run_id": run.id},
            )
            return None

    async def execute(
        self,
        run_id: str,
        *,
        cancellation_token: CancellationToken,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> RunExecutionResult:
        run = self._repository.get_run(run_id)
        self._active_run_id = run.id
        self._cancellation_token = cancellation_token
        self._run_compacted = False
        run_started = time.monotonic()
        entries = self._repository.list_entry_context_entries(run.trigger_entry_id)
        if run.trigger_content_override is not None:
            # P0-2b：编辑重跑只覆盖本轮的触发用户文案，不新增/改写 transcript 条目
            # （事实源保持 append-only，链语义与 regenerate 一致）。
            from dataclasses import replace as replace_entry

            entries = tuple(
                replace_entry(
                    entry,
                    payload={**entry.payload, "content": run.trigger_content_override},
                )
                if entry.id == run.trigger_entry_id
                else entry
                for entry in entries
            )
        from endless_task.context_engine import ContextBudget

        from .context_segments import build_plan_shadow

        budget = None
        if (
            self._context_window_tokens is not None
            and self._context_window_tokens > self._max_output_tokens
        ):
            budget = ContextBudget(
                window_tokens=self._context_window_tokens,
                reserved_output_tokens=self._max_output_tokens,
                safety_margin_tokens=0,
            )
        plan_shadow = build_plan_shadow(entries, budget=budget)
        projection_entries = entries
        if self._context_engine_v2_enabled and budget is not None:
            excluded = set(plan_shadow.excluded_source_ids)
            if excluded:
                projection_entries = tuple(
                    entry for entry in entries if entry.id not in excluded
                )
        projection = self._context_projection.project(projection_entries)
        if self._context_shadow_observer is not None:
            self._context_shadow_observer(plan_shadow)
        provider_messages: list[ProviderMessage] = list(
            self._context_prefix_messages
        )
        provider_messages.extend(projection.messages)
        run = self._repository.start_run(run.id)
        cancellation_token.raise_if_cancelled()
        run_span = self._open_run_span(run, monotonic_started=run_started)

        content_parts: list[str] = []
        model_turn_ids: list[str] = []
        input_tokens = 0
        output_tokens = 0
        pending_approval_ids: tuple[str, ...] = ()
        # C4 升级报告所需的运行进展计数（局部累计，无需回查数据库）。
        tool_calls_total = 0
        produced_characters = 0
        # C1 验证所需：本次运行实际执行过的"有副作用"工具（关键运行的判据）。
        side_effect_evidence: list[str] = []

        try:
            while True:
                remaining_timeout: Optional[float] = None
                if self._agent_timeout_seconds is not None:
                    elapsed = time.monotonic() - run_started
                    remaining_timeout = self._agent_timeout_seconds - elapsed
                    if remaining_timeout <= 0:
                        raise SafetyStopError(
                            SafetyStopReason.AGENT_TIMEOUT,
                            "助手运行超时，已安全停止。",
                        )

                async def run_next_model_turn() -> ModelTurnOutcome:
                    provider_messages.extend(await self._drain_steering_messages(run))
                    await self._maybe_compact_context(run, provider_messages)
                    return await self._model_turn_runner.run(
                        run=run,
                        messages=provider_messages,
                        cancellation_token=cancellation_token,
                        on_text_delta=on_text_delta,
                    )

                try:
                    if remaining_timeout is None:
                        outcome = await run_next_model_turn()
                    else:
                        async with asyncio.timeout(remaining_timeout):
                            outcome = await run_next_model_turn()
                except TimeoutError as error:
                    raise SafetyStopError(
                        SafetyStopReason.AGENT_TIMEOUT,
                        "助手运行超时，已安全停止。",
                    ) from error
                model_turn_ids.append(outcome.model_turn_id)
                content_parts.append(outcome.content)
                input_tokens += outcome.input_tokens or 0
                output_tokens += outcome.output_tokens or 0
                tool_calls_total += len(outcome.tool_calls)
                produced_characters += len(outcome.content)
                side_effect_evidence.extend(
                    self._side_effect_evidence(outcome.tool_calls)
                )
                pending_approval_ids = outcome.pending_approval_execution_ids
                if pending_approval_ids:
                    return self._result(
                        RunStatus.WAITING_APPROVAL,
                        run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        pending_approval_ids,
                    )

                provider_messages.extend(outcome.messages)
                self._observe_failure_memory(run)
                # C4：无进展（连续同因失败）或预算将尽 → 提请人工决策。
                self._maybe_escalate(
                    run,
                    progress=EscalationProgress(
                        model_turns=len(model_turn_ids),
                        tool_calls=tool_calls_total,
                        tool_failures=self._failure_memory.snapshot.total,
                        produced_characters=produced_characters,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    used_tokens=(outcome.input_tokens or 0)
                    + (outcome.output_tokens or 0),
                )
                delivered = not outcome.tool_calls and bool(outcome.content.strip())
                # 工具级「提前终止」：本批所有工具结果都请求 terminate → 停循环。
                if outcome.tool_calls and outcome.terminate:
                    await self._maybe_verify(
                        run,
                        candidate="".join(content_parts),
                        side_effects=side_effect_evidence,
                        progress=EscalationProgress(
                            model_turns=len(model_turn_ids),
                            tool_calls=tool_calls_total,
                            tool_failures=self._failure_memory.snapshot.total,
                            produced_characters=produced_characters,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        ),
                        cancellation_token=cancellation_token,
                    )
                    final_run, assistant_entry = self._repository.finalize_run(
                        run.id,
                        content="".join(content_parts),
                        finish_reason=outcome.finish_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return self._result(
                        RunStatus.COMPLETED,
                        final_run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        (),
                        assistant_entry_id=assistant_entry.id,
                    )
                if delivered:
                    await self._maybe_verify(
                        run,
                        candidate="".join(content_parts),
                        side_effects=side_effect_evidence,
                        progress=EscalationProgress(
                            model_turns=len(model_turn_ids),
                            tool_calls=tool_calls_total,
                            tool_failures=self._failure_memory.snapshot.total,
                            produced_characters=produced_characters,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        ),
                        cancellation_token=cancellation_token,
                    )
                    final_run, assistant_entry = self._repository.finalize_run(
                        run.id,
                        content="".join(content_parts),
                        finish_reason=outcome.finish_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return self._result(
                        RunStatus.COMPLETED,
                        final_run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        (),
                        assistant_entry_id=assistant_entry.id,
                    )
                if self._no_progress_enforcement_enabled:
                    from endless_task.reliability.stop import StopLevel
                    from endless_task.reliability.stop_runtime import no_progress_guidance

                    evaluation = _no_progress_evaluation_for_run(
                        repository=self._repository,
                        run_id=run.id,
                    )
                    self._observe_no_progress_stuck(run, evaluation)
                    if evaluation is not None and evaluation.level is StopLevel.STOP:
                        self._maybe_escalate(
                            run,
                            progress=EscalationProgress(
                                model_turns=len(model_turn_ids),
                                tool_calls=tool_calls_total,
                                tool_failures=self._failure_memory.snapshot.total,
                                produced_characters=produced_characters,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            ),
                            used_tokens=input_tokens + output_tokens,
                            reason=NO_PROGRESS_REASON,
                            will_stop=True,
                        )
                        guidance = no_progress_guidance(evaluation.level, evaluation.reasons)
                        content_parts.append("\n\n" + guidance)
                        await self._maybe_verify(
                            run,
                            candidate="".join(content_parts),
                            side_effects=side_effect_evidence,
                            progress=EscalationProgress(
                                model_turns=len(model_turn_ids),
                                tool_calls=tool_calls_total,
                                tool_failures=self._failure_memory.snapshot.total,
                                produced_characters=produced_characters,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            ),
                            cancellation_token=cancellation_token,
                        )
                        final_run, assistant_entry = self._repository.finalize_run(
                            run.id,
                            content="".join(content_parts),
                            finish_reason="no_progress_stop",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                        return self._result(
                            RunStatus.COMPLETED,
                            final_run,
                            "".join(content_parts),
                            input_tokens,
                            output_tokens,
                            tuple(model_turn_ids),
                            (),
                            assistant_entry_id=assistant_entry.id,
                        )
                    if evaluation is not None and (
                        evaluation.level is StopLevel.REMIND
                        or evaluation.level is StopLevel.RESTRICT
                    ):
                        self._steering_messages.append(
                            no_progress_guidance(evaluation.level, evaluation.reasons)
                        )
        except RuntimeCancelled:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.CANCELLED,
                event_type="run_cancelled",
                cancelled_by="user",
            )
            return self._result(
                RunStatus.CANCELLED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except SafetyStopError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="safety_stop",
                payload={
                    "reason": error.reason.value,
                    "errorCode": error.code,
                    "safeMessage": error.safe_message,
                },
            )
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except ProviderError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except Exception:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            logger.exception("Runtime v2 run failed unexpectedly", extra={"run_id": run.id})
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={
                    "errorCode": "runtime_internal_error",
                    "safeMessage": "执行过程出现内部错误。",
                },
                error_code="runtime_internal_error",
                safe_message="执行过程出现内部错误。",
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        finally:
            self._active_run_id = None
            self._cancellation_token = None
            if self._metrics is not None:
                final_run = self._repository.get_run(run.id)
                self._metrics.record_run(
                    RunMetric(
                        run_id=run.id,
                        conversation_id=run.conversation_id,
                        status=final_run.status.value,
                        duration_ms=int((time.monotonic() - run_started) * 1000),
                        model_turn_count=len(model_turn_ids),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        compacted=self._run_compacted,
                        compaction_released_tokens=0,
                    )
                )
            if run_span is not None:
                try:
                    final_status_run = self._repository.get_run(run.id)
                    await run_span.end(
                        SpanStatus(final_status_run.status.value)
                        if final_status_run.status.value in {
                            "completed",
                            "failed",
                            "cancelled",
                        }
                        else SpanStatus.FAILED,
                        ended_at=final_status_run.finished_at or _iso_now(),
                        monotonic_ended=time.monotonic(),
                    )
                except Exception:
                    logger.exception(
                        "Failed to close run trace span",
                        extra={"run_id": run.id},
                    )
            if self._trace_observer is not None:
                # Observability projection (M6 W6-1): mirror terminal usage
                # into the runtime ledger under a hard timeout. The observer
                # is fail-open (errors swallowed, non-terminal/idempotent
                # runs skipped), so this can never break or hang a run.
                try:
                    await asyncio.wait_for(
                        self._trace_observer.on_run_terminal(run.id),
                        timeout=MIRROR_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.exception(
                        "Terminal trace observer failed for run",
                        extra={"run_id": run.id},
                    )

    async def enqueue_user_message(self, content: str) -> bool:
        async with self._queue_lock:
            if self._active_run_id is None:
                self._follow_up_messages.append(content)
                return False
            self._steering_messages.append(content)
            return True

    async def drain_follow_up_messages(self) -> tuple[str, ...]:
        async with self._queue_lock:
            messages = tuple(self._follow_up_messages)
            self._follow_up_messages.clear()
            return messages

    async def request_cancel(self, *, cancelled_by: str = "user") -> None:
        if self._cancellation_token is not None:
            self._cancellation_token.cancel()
        if self._active_run_id is None:
            return
        run = self._repository.get_run(self._active_run_id)
        if run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            return
        self._repository.transition_run_status(
            run.id,
            RunStatus.CANCELLING,
            event_type="run_cancel_requested",
            cancelled_by=cancelled_by,
        )

    def _observe_failure_memory(self, run: RunRecord) -> None:
        """C2：把"连续同因失败"变成外部状态，并作为下一轮决策的输入。

        * 一组新的重复失败出现 → 发 ``run_stuck`` 事件（产品层可见/可审计），
          并把精简的失败记忆注入下一轮：模型不该原样重复已失败的调用；
        * 该工具成功一次（连续失败清零）→ 发 ``run_progress_resumed`` 清掉卡住态。
        """
        memory = self._failure_memory.snapshot
        if not memory.repeated:
            if self._stuck_emitted:
                self._append_progress_resumed(run)
                self._stuck_emitted = False
                self._stuck_signature = None
                self._injected_failure_keys.clear()
                # 恢复后允许下一次卡住重新升级。
                self._escalated_reasons.clear()
            return
        signature = tuple(
            (
                item.tool_name,
                item.error_code,
                "restrict" if item.count >= 3 else "remind",
            )
            for item in memory.repeated
        )
        if signature == self._stuck_signature:
            return
        self._stuck_signature = signature
        self._stuck_emitted = True
        guidance = memory.guidance()
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_stuck",
            payload={
                "level": "restrict"
                if any(item.count >= 3 for item in memory.repeated)
                else "remind",
                "detector": "repeated_failure",
                "reasons": [
                    f"{item.tool_name} 已连续失败 {item.count} 次"
                    f"（{item.error_code}）"
                    for item in memory.repeated
                ],
                "repeatedFailures": [
                    item.as_json() for item in memory.repeated
                ],
                "attempts": list(memory.digest(limit=5)),
                "guidance": guidance,
            },
        )
        # 同一组（工具，错误码）只注入一次，避免每轮重复占上下文。
        new_keys = {
            (item.tool_name, item.error_code)
            for item in memory.repeated
            if (item.tool_name, item.error_code) not in self._injected_failure_keys
        }
        if guidance and new_keys:
            self._injected_failure_keys.update(new_keys)
            self._steering_messages.append(guidance)

    def _observe_no_progress_stuck(self, run: RunRecord, evaluation: object) -> None:
        """C2：no-progress 评估（启用时）也落到同一条 run.stuck 通道。

        只在级别变化时发事件，级别回落 ``none`` 时发恢复事件，避免每轮刷屏。
        """
        level = getattr(getattr(evaluation, "level", None), "value", None)
        if level in (None, "none"):
            if self._no_progress_stuck_level is not None:
                self._no_progress_stuck_level = None
                self._append_progress_resumed(run)
            return
        if level == self._no_progress_stuck_level:
            return
        self._no_progress_stuck_level = level
        memory = self._failure_memory.snapshot
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_stuck",
            payload={
                "level": level,
                "detector": getattr(evaluation, "detector", None),
                "reasons": list(getattr(evaluation, "reasons", ()) or ()),
                "consecutive": getattr(evaluation, "consecutive", None),
                "repeatedFailures": [
                    item.as_json() for item in memory.repeated
                ],
                "attempts": list(memory.digest(limit=5)),
                "guidance": memory.guidance(),
            },
        )

    def _side_effect_evidence(self, tool_calls: Sequence[ProviderToolCall]) -> list[str]:
        """本次运行里"有副作用"的工具调用（local_write / external_action）。"""
        evidence: list[str] = []
        for call in tool_calls:
            try:
                definition = self._tool_registry.resolve(call.name).definition
            except Exception:
                continue
            effect = getattr(definition.effect, "value", definition.effect)
            if effect and str(effect) != "read_only":
                evidence.append(f"{call.name}（{effect}）")
        return evidence

    def _verification_goal(self, run: RunRecord) -> str:
        """验证器的"目标"：本次运行的触发内容。"""
        if run.trigger_content_override:
            return run.trigger_content_override
        try:
            entry = self._repository.get_entry(run.trigger_entry_id)
        except Exception:
            return ""
        content = entry.payload.get("content") if isinstance(entry.payload, dict) else None
        return content if isinstance(content, str) else ""

    async def _maybe_verify(
        self,
        run: RunRecord,
        *,
        candidate: str,
        side_effects: Sequence[str],
        progress: EscalationProgress,
        cancellation_token: CancellationToken,
    ) -> Optional[VerificationVerdict]:
        """C1：制造者产出后，由独立验证者（独立上下文）判定结果。

        验证器只看目标 + 产出 + 工具证据，不共享制造者历史；判定写入
        ``run_verified`` 事件；判 fail 时同时走 C4 升级通道（带结论重试/接管）。
        验证失败（网络/超时）不改变本次运行结果——只记 uncertain。
        """
        if not should_verify(
            self._verifier_mode,
            has_side_effects=bool(side_effects),
            candidate=candidate,
        ):
            return None
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_verifying",
            payload={"runId": run.id, "model": self._verifier_model or self._model},
        )
        messages = build_verifier_messages(
            goal=self._verification_goal(run),
            candidate=candidate,
            evidence=tuple(side_effects),
        )
        model = self._verifier_model or self._model
        request = ProviderRequest(
            request_id=f"{run.id}:verify",
            model=model,
            messages=messages,
            max_output_tokens=min(512, self._max_output_tokens),
            temperature=0.0,
            tools=(),
        )
        started = time.monotonic()
        text_parts: list[str] = []
        completion: Optional[ProviderCompleted] = None
        try:
            async for event in self._provider.stream(request, cancellation_token):
                cancellation_token.raise_if_cancelled()
                if isinstance(event, ProviderTextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, ProviderCompleted):
                    completion = event
        except Exception:
            logger.exception(
                "Verifier call failed",
                extra={"run_id": run.id},
            )
            verdict = VerificationVerdict(
                verdict="uncertain",
                reasons=("验证器调用失败，未能得到独立结论。",),
                model=model,
            )
        else:
            verdict = parse_verdict("".join(text_parts), model=model)
        verdict = replace(
            verdict,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=getattr(completion, "input_tokens", None),
            output_tokens=getattr(completion, "output_tokens", None),
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_verified",
            payload=verdict.as_json(),
        )
        if verdict.failed:
            self._escalate_verification_failure(run, verdict, progress=progress)
        return verdict

    def _escalate_verification_failure(
        self,
        run: RunRecord,
        verdict: VerificationVerdict,
        *,
        progress: EscalationProgress,
    ) -> None:
        """验证不通过 → 升级人工（C4），报告里带上验证结论。"""
        if VERIFICATION_REASON in self._escalated_reasons:
            return
        self._escalated_reasons.add(VERIFICATION_REASON)
        memory = self._failure_memory.snapshot
        report = build_escalation_report(
            reason=VERIFICATION_REASON,
            progress=progress,
            budget=EscalationBudget(used_tokens=0, limit_tokens=None),
            memory=memory,
            will_stop=False,
            verdict=verdict,
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_awaiting_user",
            payload=report.as_json(),
        )

    def _maybe_escalate(
        self,
        run: RunRecord,
        *,
        progress: EscalationProgress,
        used_tokens: int,
        reason: Optional[str] = None,
        will_stop: bool = False,
    ) -> None:
        """C4：无进展或预算将尽时发 ``run_awaiting_user``（同一原因只提一次）。

        升级不是暂停运行：报告给出进展/卡点/可选项，运行继续推进；若安全停止
        策略将结束本次运行，报告里 ``willStop`` 会说明。
        """
        memory = self._failure_memory.snapshot
        if reason is None:
            if NO_PROGRESS_REASON not in self._escalated_reasons and any(
                item.count >= 3 for item in memory.repeated
            ):
                reason = NO_PROGRESS_REASON
            elif BUDGET_REASON not in self._escalated_reasons and budget_exhausted(
                used_tokens=used_tokens,
                limit_tokens=self._escalation_limit_tokens(),
                ratio=self._escalation_budget_ratio,
            ):
                reason = BUDGET_REASON
        if reason is None or reason in self._escalated_reasons:
            return
        self._escalated_reasons.add(reason)
        report = build_escalation_report(
            reason=reason,
            progress=progress,
            budget=EscalationBudget(
                used_tokens=used_tokens,
                limit_tokens=self._escalation_limit_tokens(),
            ),
            memory=memory,
            will_stop=will_stop,
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_awaiting_user",
            payload=report.as_json(),
        )

    def _escalation_limit_tokens(self) -> Optional[int]:
        """可用上下文上限：窗口减去必须留给输出的配额。"""
        if self._context_window_tokens is None:
            return None
        limit = self._context_window_tokens - self._max_output_tokens
        return limit if limit > 0 else None

    def _append_progress_resumed(self, run: RunRecord) -> None:
        """C2：卡住态解除（失败记忆清零或 no-progress 级别回落）。"""
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_progress_resumed",
            payload={"runId": run.id},
        )

    async def _drain_steering_messages(self, run: RunRecord) -> list[ProviderMessage]:
        async with self._queue_lock:
            messages = self._steering_messages
            self._steering_messages = []
        projected: list[ProviderMessage] = []
        for message in messages:
            projected.append(ProviderMessage(role="user", content=message))
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="steer_injected",
                payload={"content": message},
            )
        return projected

    async def _maybe_compact_context(
        self,
        run: RunRecord,
        provider_messages: list[ProviderMessage],
    ) -> None:
        if self._compaction_hook is None:
            return
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_started",
            payload={},
        )
        result = await self._compaction_hook.compact(
            run,
            tuple(provider_messages),
        )
        if result is None:
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="compaction_completed",
                payload={"changed": False},
            )
            return
        self._run_compacted = True
        if result.summary_entry_id is not None:
            # entry 层面压缩:summary entry 已固化到 lane,从当前 leaf 重建投影。
            # covered entries 由 ContextProjection 跳过,当前 run 立即收敛;
            # 后续 Run 的投影同样命中同一规则。
            entries = self._repository.list_lane_context_entries(run.lane_id)
            projection = self._context_projection.project(entries)
            provider_messages[:] = [
                *self._context_prefix_messages,
                *projection.messages,
            ]
        else:
            provider_messages[:] = list(result.messages)
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_completed",
            payload={
                "changed": True,
                "summaryEntryId": result.summary_entry_id,
                "coveredEntryIds": list(result.covered_entry_ids),
            },
        )

    def _append_current_model_turn_ids(
        self,
        run_id: str,
        model_turn_ids: list[str],
    ) -> None:
        existing = set(model_turn_ids)
        model_turn_ids.extend(
            turn.id
            for turn in self._repository.list_model_turns(run_id)
            if turn.id not in existing
        )

    def _result(
        self,
        status: RunStatus,
        run: RunRecord,
        content: str,
        input_tokens: int,
        output_tokens: int,
        model_turn_ids: tuple[str, ...],
        pending_approval_ids: tuple[str, ...],
        *,
        assistant_entry_id: Optional[str] = None,
    ) -> RunExecutionResult:
        if self._no_progress_observer is not None:
            _evaluate_no_progress_shadow(
                repository=self._repository,
                run_id=run.id,
                observer=self._no_progress_observer,
            )
        return RunExecutionResult(
            status=status,
            run=run,
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_turn_ids=model_turn_ids,
            pending_approval_execution_ids=pending_approval_ids,
            follow_up_messages=tuple(self._follow_up_messages),
            assistant_entry_id=assistant_entry_id,
        )
