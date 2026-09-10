"""对外会话网关（聚合模块）。

原先是一个 2 002 行、12 个类的文件；现在实现拆到 `runtime_v2/gateway_impl/`：
`models.py`（数据类/协议/常量）· `approval.py`（`GatewayToolApprovalGate`）·
`projection.py`（`ProductRuntimeEventProjection`）· `sessions.py`（会话连接与驱动）·
`base.py` / `lanes.py` / `runs.py`（`RuntimeV2SessionGateway` 的三个 mixin）·
`_gateway.py`（组合类）· `views.py`（快照/事件 JSON 视图）。这里只做 re-export。
"""

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

from .domain import (
    LaneKind,
    LaneEventRecord,
    LanePromotionRecord,
    LaneRecord,
    MemoryScope,
    ProductRuntimeEventRecord,
    RunRecord,
    RunStatus,
    RuntimeEventRecord,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
    TranscriptEntryType,
)
from .escalation import DEFAULT_BUDGET_RATIO
from .usage_cost import build_run_usage_summary
from .user_profile import UserProfileBlock
from .lane import RuntimeV2LaneCreationResult, RuntimeV2LaneService
from .memory import RuntimeV2MemoryService
from .execution import (
    AgentRunExecutor,
    ContextCompactionHook,
    RunExecutionResult,
    ToolApprovalDecision,
    ToolApprovalGate,
    ToolExecutionLimits,
)
from .metrics import ApprovalMetric, RuntimeV2MetricsCollector
from .trace_observer import RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from .replay import (
    RunReplayResult,
    CrashRecoveryReport,
    ConversationRuntimeSnapshot,
    RuntimeV2ReplayService,
)

if TYPE_CHECKING:
    from endless_task.storage import (
        SqliteChatRepository,
        SqliteRuntimeV2MemoryRepository,
        SqliteRuntimeV2Repository,
    )


from .gateway_impl.models import (  # noqa: F401
    logger,
    V2_CAPABILITIES,
    _SNAPSHOT_UNTRUSTED_MAX_CHARS,
    AgentRuntimeCapabilities,
    RuntimeProviderSelection,
    RuntimeV2SendResult,
    RuntimeV2RecoveryResult,
    RuntimeV2RegenerateResult,
    PendingApproval,
    derive_approval_risk,
    _ActiveRun,
)
from .gateway_impl.approval import (  # noqa: F401
    GatewayToolApprovalGate,
)
from .gateway_impl.projection import (  # noqa: F401
    ProductRuntimeEventProjection,
)
from .gateway_impl.sessions import (  # noqa: F401
    AgentSessionConnection,
    AgentSessionDriver,
)
from .gateway_impl._gateway import (  # noqa: F401
    RuntimeV2SessionGateway,
)
from .gateway_impl.views import (  # noqa: F401
    _prefix_fingerprint,
    product_event_json,
    _entry_json,
    _run_state_json,
    _tool_states_json,
    _context_budget_json,
    _last_turn_tokens,
    _stuck_json,
    _escalation_json,
    _verification_json,
    _usage_json,
    _approval_json,
    _recovery_report_json,
    _string,
    _bounded_text,
    _bounded_json,
)
