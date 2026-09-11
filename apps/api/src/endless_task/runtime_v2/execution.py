"""执行引擎（聚合模块）。

原先是一个 2 913 行、18 个类的文件；现在实现拆到 `runtime_v2/execution_impl/`：
`_base.py`（logger / 模型轮次状态常量）· `tool_limits.py`（执行限额与 JSON 体积估算）·
`tool_gates.py`（审批与信任策略）· `progress.py`（消息指纹与"无进展"判定）·
`coordinator.py`（`ToolExecutionCoordinator`）· `turns.py`（`ModelTurnRunner`）·
`runner.py`（`AgentRunExecutor`）。这里只做 re-export，既有导入路径不变。
"""

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

from .projection import ContextProjection
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
    COST_REASON,
    DEFAULT_BUDGET_RATIO,
    NO_PROGRESS_REASON,
    VERIFICATION_REASON,
    EscalationBudget,
    EscalationProgress,
    build_escalation_report,
    budget_exhausted,
)
from .failure_memory import FailureMemoryAccumulator
from .usage_cost import cost_cap_exceeded, estimate_cost_usd
from .user_profile import UserProfileBlock
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
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
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


from .execution_impl._base import (  # noqa: F401
    logger,
    _iso_now,
    _MODEL_TURN_WAITING_SLOT_STATUS,
    _MODEL_TURN_STREAMING_STATUS,
    _MODEL_TURN_EXECUTING_STATUS,
    _MODEL_TURN_COMPLETED_STATUS,
    _MODEL_TURN_FAILED_STATUS,
    _MODEL_TURN_CANCELLED_STATUS,
    _TERMINAL_TOOL_STATUSES,
)
from .execution_impl.tool_limits import (  # noqa: F401
    ToolExecutionLimits,
    _json_value_size,
)
from .execution_impl.tool_gates import (  # noqa: F401
    ToolApprovalDecision,
    ToolApprovalGate,
    ContextCompactionResult,
    ContextCompactionHook,
    WaitingToolApprovalGate,
    StaticToolApprovalGate,
    ToolTrustPolicy,
    UnattendedToolApprovalGate,
    ToolExecutionOutcome,
)
from .execution_impl.progress import (  # noqa: F401
    _messages_fingerprint,
    _no_progress_evaluation_for_run,
    _evaluate_no_progress_shadow,
)
from .execution_impl.coordinator import (  # noqa: F401
    _ToolWorkItem,
    _PreparedToolExecution,
    ToolExecutionCoordinator,
)
from .execution_impl.turns import (  # noqa: F401
    ModelTurnOutcome,
    ModelTurnRunner,
)
from .execution_impl.runner import (  # noqa: F401
    RunExecutionResult,
    AgentRunExecutor,
)
