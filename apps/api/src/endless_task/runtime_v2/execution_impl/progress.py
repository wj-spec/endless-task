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
