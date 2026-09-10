from __future__ import annotations

from __future__ import annotations
from endless_task.runtime.provider import ProviderToolDefinition
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING
from endless_task.domain.models import Conversation
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import ModelProvider, ProviderMessage
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolEffect,
    ToolRegistry,
    ToolValidationError,
)
from ..domain import LaneKind, LaneEventRecord, LanePromotionRecord, LaneRecord, MemoryScope, ProductRuntimeEventRecord, RunRecord, RunStatus, RuntimeEventRecord, RuntimeV2MemoryPromotion, RuntimeV2MemoryRecord, ToolExecutionRecord, ToolExecutionStatus, TranscriptEntryRecord, TranscriptEntryType
from ..escalation import DEFAULT_BUDGET_RATIO
from ..usage_cost import build_run_usage_summary
from ..user_profile import UserProfileBlock
from ..lane import RuntimeV2LaneCreationResult, RuntimeV2LaneService
from ..memory import RuntimeV2MemoryService
from ..execution import AgentRunExecutor, ContextCompactionHook, RunExecutionResult, ToolApprovalDecision, ToolApprovalGate, ToolExecutionLimits
from ..metrics import ApprovalMetric, RuntimeV2MetricsCollector
from ..trace_observer import RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from ..replay import RunReplayResult, CrashRecoveryReport, ConversationRuntimeSnapshot, RuntimeV2ReplayService


#: 日志名与拆分前的 `endless_task.runtime_v2.gateway` **保持一致**：测试与运维按
#: logger 名断言/过滤，用 `__name__` 会变成 `...gateway_impl.models` 而静默失效。
logger = logging.getLogger("endless_task.runtime_v2.gateway")


V2_CAPABILITIES = (
    "run_turn_streaming",
    "tool_execution",
    "parallel_tools",
    "approval",
    "steering",
    "compaction",
    "context_usage",
    "lane_branching",
    "run_variants",
    "memory_scopes",
)


_SNAPSHOT_UNTRUSTED_MAX_CHARS = 4_000


@dataclass(frozen=True)
class AgentRuntimeCapabilities:
    driver_type: str
    capabilities: tuple[str, ...]


class RuntimeProviderSelection(Protocol):
    provider: ModelProvider
    model: str


@dataclass(frozen=True)
class RuntimeV2SendResult:
    conversation_id: str
    lane_id: str
    run_id: str
    user_message_id: str


@dataclass(frozen=True)
class RuntimeV2RecoveryResult:
    run_id: str
    action: str
    lane_id: str
    new_run_id: Optional[str] = None


@dataclass(frozen=True)
class RuntimeV2RegenerateResult:
    old_run_id: str
    new_run_id: str
    lane_id: str
    trigger_entry_id: str
    sibling_group_id: str


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    run_id: str
    model_turn_id: str
    tool_execution_id: str
    tool_name: str
    summary: str
    reason: str
    metadata: dict


def derive_approval_risk(effect: str, tool_name: str) -> str:
    """按可逆性/影响范围推导审批风险等级（低/中/高）。

    只读不触发审批；本地写=中；外部动作/删除类=高。供前端做风险色提示。
    """
    name = (tool_name or "").lower()
    if effect == ToolEffect.EXTERNAL_ACTION.value:
        return "high"
    if any(
        token in name
        for token in ("delete", "remove", "rm ", "drop", "unlink", "truncate", "wipe")
    ):
        return "high"
    if effect == ToolEffect.LOCAL_WRITE.value:
        return "medium"
    return "low"


@dataclass
class _ActiveRun:
    run_id: str
    task: asyncio.Task[object]
    executor: AgentRunExecutor
    cancellation_token: CancellationToken
