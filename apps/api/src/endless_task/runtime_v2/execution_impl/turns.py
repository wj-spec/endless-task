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
from ._base import (
    _iso_now,
    _MODEL_TURN_CANCELLED_STATUS,
    _MODEL_TURN_COMPLETED_STATUS,
    _MODEL_TURN_EXECUTING_STATUS,
    _MODEL_TURN_FAILED_STATUS,
    _MODEL_TURN_STREAMING_STATUS,
    _MODEL_TURN_WAITING_SLOT_STATUS,
)
from ._base import logger
from .progress import _messages_fingerprint
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
