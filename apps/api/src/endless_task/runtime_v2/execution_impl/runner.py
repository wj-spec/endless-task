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
from ._base import _iso_now, logger
from .coordinator import ToolExecutionCoordinator
from .progress import (
    _evaluate_no_progress_shadow,
    _no_progress_evaluation_for_run,
)
from .turns import ModelTurnRunner
from ..projection import ContextProjection
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
        user_profile_provider: Optional[Callable[[str], Optional[UserProfileBlock]]] = None,
        cost_cap_usd: float = 0.0,
        pricing_catalog: Optional[PricingCatalog] = None,
        context_shadow_observer: Optional[object] = None,
        context_window_tokens: Optional[int] = None,
        agent_timeout_seconds: Optional[float] = None,
        trace_observer: Optional[RunTraceObserver] = None,
        span_recorder: Optional[object] = None,
        tool_trust_policy: Optional[ToolTrustPolicy] = None,
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
        # B5 用户画像：每次运行开始时读一次已存画像（只读，不重算），
        # 放在静态前缀之后 → 同一版本字节一致，provider 前缀缓存可复用。
        self._user_profile_provider = user_profile_provider
        # C5 成本可见：累计成本估算（按定价表）与可选的成本上限。
        self._cost_cap_usd = float(cost_cap_usd)
        self._pricing_catalog = pricing_catalog or make_default_catalog()
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
            trust_policy=tool_trust_policy,
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
        profile = self._load_user_profile(run.conversation_id)
        if profile is not None and not profile.empty:
            provider_messages.append(
                ProviderMessage(role="system", content=profile.content)
            )
        provider_messages.extend(projection.messages)
        run = self._repository.start_run(run.id)
        if profile is not None and not profile.empty:
            # 缓存/成本可观测：把版本与签名记进事件，便于观察前缀变动频率。
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="user_profile_injected",
                payload={
                    "version": profile.version,
                    "signature": profile.signature,
                    "characters": profile.characters,
                    "lines": len(profile.lines),
                    "manual": profile.manual,
                },
            )
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

    def _load_user_profile(self, conversation_id: str) -> Optional[UserProfileBlock]:
        """B5：读取已存画像（只读一行；失败一律当作没有画像）。"""
        provider = self._user_profile_provider
        if provider is None:
            return None
        try:
            return provider(conversation_id)
        except Exception:  # noqa: BLE001 画像读取失败不影响运行
            logger.debug("User profile load failed", exc_info=True)
            return None

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
        cost = self._cumulative_cost(progress)
        cost_exceeded = cost_cap_exceeded(
            cost_usd=cost,
            cap_usd=self._cost_cap_usd,
        )
        if reason is None:
            if COST_REASON not in self._escalated_reasons and cost_exceeded:
                reason = COST_REASON
            elif NO_PROGRESS_REASON not in self._escalated_reasons and any(
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
            cost=(
                {
                    "usedUsd": cost,
                    "capUsd": self._cost_cap_usd,
                    "priced": cost is not None,
                }
                if reason == COST_REASON
                else None
            ),
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_awaiting_user",
            payload=report.as_json(),
        )

    def _cumulative_cost(self, progress: EscalationProgress) -> Optional[float]:
        """按定价表估算本次运行累计成本（未定价模型返回 None）。"""
        return estimate_cost_usd(
            self._pricing_catalog,
            provider=getattr(self._provider, "name", None),
            model=self._model,
            input_tokens=progress.input_tokens,
            output_tokens=progress.output_tokens,
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
