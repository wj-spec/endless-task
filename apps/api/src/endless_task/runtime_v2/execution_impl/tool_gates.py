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


class ToolTrustPolicy(Protocol):
    """Optional policy that can waive approval for provably safe calls.

    Consulted **only** when a call would otherwise need approval and is not
    force-confirmed (dangerous commands always ask). Implementations must fail
    closed: any parse error means "not trusted".
    """

    def allows(self, tool_name: str, call: ToolCall) -> bool:  # pragma: no cover - protocol
        ...


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
