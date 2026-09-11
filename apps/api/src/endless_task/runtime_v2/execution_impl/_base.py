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


#: 日志名与拆分前的 `endless_task.runtime_v2.execution` **保持一致**：测试（如
#: `test_unexpected_tool_exception_is_safely_redacted`）与运维侧按 logger 名断言/过滤，
#: 用 `__name__` 会变成 `...runtime_v2.execution_impl._base` 而使断言失效。
logger = logging.getLogger("endless_task.runtime_v2.execution")


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
