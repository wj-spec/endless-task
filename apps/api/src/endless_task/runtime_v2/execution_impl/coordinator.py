from __future__ import annotations

from endless_task.tool_platform.protocol import ToolOutcome
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
from ._base import _TERMINAL_TOOL_STATUSES, logger
from .tool_gates import (
    ToolApprovalDecision,
    ToolExecutionOutcome,
    WaitingToolApprovalGate,
)
from .tool_limits import ToolExecutionLimits, _json_value_size
from ..context import ContextProjection
from ..domain import ModelTurnRecord, ModelTurnStatus, RunRecord, RunStatus, ToolExecutionRecord, ToolExecutionStatus
from ..escalation import BUDGET_REASON, COST_REASON, DEFAULT_BUDGET_RATIO, NO_PROGRESS_REASON, VERIFICATION_REASON, EscalationBudget, EscalationProgress, build_escalation_report, budget_exhausted
from ..failure_memory import FailureMemoryAccumulator
from ..usage_cost import cost_cap_exceeded, estimate_cost_usd
from ..user_profile import UserProfileBlock
from ..verification import VERIFICATION_PROTOCOL_VERSION, VerificationVerdict, build_verifier_messages, parse_verdict, should_verify
from ..safety import SafetyStopError, SafetyStopReason
from ..metrics import ModelTurnMetric, RunMetric, RuntimeV2MetricsCollector
from ..trace_observer import MIRROR_TIMEOUT_SECONDS, RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from endless_task.runtime_ledger.protocol import (
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)


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
        trust_policy: Optional[ToolTrustPolicy] = None,
    ) -> None:
        self._repository = repository
        self._tool_registry = tool_registry
        self._approval_gate = approval_gate or WaitingToolApprovalGate()
        # S8: 只读信任策略（None = 全部按 approval_mode 处理，行为不变）。
        self._trust_policy = trust_policy
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
                # S8 只读信任：危险命令（force_confirm）永不免确认。
                if needs_approval and not force_confirm:
                    policy = self._trust_policy
                    allows = getattr(policy, "allows", None)
                    if callable(allows):
                        try:
                            waived = bool(allows(tool.definition.name, call))
                            needs_approval = not waived
                            if waived:
                                # 把"策略放行"也记成审批证据：否则 eval 的
                                # approval_gate 会把 REQUIRED 工具的执行判成
                                # "未审批执行"（策略放行不是绕过审批，而是用户
                                # 通过配置做出的全局授权）。
                                try:
                                    self._repository.mark_tool_execution_approved(
                                        item.record_id,
                                        f"policy:{tool.definition.name}",
                                        event_type="tool_execution_policy_approved",
                                    )
                                except Exception:  # noqa: BLE001 证据写入失败不阻断
                                    pass
                        except Exception:
                            needs_approval = True
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
