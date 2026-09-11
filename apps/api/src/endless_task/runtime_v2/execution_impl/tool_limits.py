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
