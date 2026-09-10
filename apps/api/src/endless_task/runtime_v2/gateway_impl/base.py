"""`RuntimeV2SessionGateway` 的底座：字段、会话接线与只读视图。

方法分组见 `gateway.py` 的说明；读写路径全部走 `self`，mixin 之间只允许
"单向调用底座"，因此不存在循环依赖。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Callable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from endless_task.domain.models import Conversation
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderMessage,
    ProviderToolDefinition,
)
from endless_task.runtime_ledger.pricing import PricingCatalog

from ..domain import (
    LaneEventRecord,
    LaneKind,
    LanePromotionRecord,
    LaneRecord,
    MemoryScope,
    ProductRuntimeEventRecord,
    RunRecord,
    RunStatus,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
    TranscriptEntryType,
)
from ..escalation import DEFAULT_BUDGET_RATIO
from ..execution import ContextCompactionHook, RunExecutionResult, ToolExecutionLimits
from ..lane import RuntimeV2LaneCreationResult, RuntimeV2LaneService
from ..memory import RuntimeV2MemoryService
from ..metrics import ApprovalMetric, RuntimeV2MetricsCollector
from ..replay import ConversationRuntimeSnapshot, CrashRecoveryReport, RuntimeV2ReplayService
from ..trace_observer import RunTraceObserver
from ..user_profile import UserProfileBlock
from endless_task.runtime_ledger.pricing import make_default_catalog
from .approval import GatewayToolApprovalGate
from .projection import ProductRuntimeEventProjection
from .models import (
    V2_CAPABILITIES,
    AgentRuntimeCapabilities,
    PendingApproval,
    RuntimeProviderSelection,
    RuntimeV2RecoveryResult,
    RuntimeV2RegenerateResult,
    RuntimeV2SendResult,
    _ActiveRun,
    derive_approval_risk,
    logger,
)
from .sessions import AgentSessionConnection, AgentSessionDriver
from .views import (
    _approval_json,
    _context_budget_json,
    _entry_json,
    _escalation_json,
    _last_turn_tokens,
    _prefix_fingerprint,
    _recovery_report_json,
    _run_state_json,
    _stuck_json,
    _tool_states_json,
    _usage_json,
    _verification_json,
)

if TYPE_CHECKING:
    from endless_task.storage import (
        SqliteChatRepository,
        SqliteRuntimeV2MemoryRepository,
        SqliteRuntimeV2Repository,
    )


class _GatewayBase:
    def __init__(
        self,
        *,
        chat_repository: "SqliteChatRepository",
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        model: str,
        max_output_tokens: int,
        temperature: Optional[float] = None,
        provider_slot_limit: Optional[int] = None,
        memory_repository: Optional[SqliteRuntimeV2MemoryRepository] = None,
        context_prefix_messages: Sequence[ProviderMessage] = (),
        provider_resolver: Optional[
            Callable[[Conversation], RuntimeProviderSelection]
        ] = None,
        tool_filter_provider: Optional[
            Callable[[str], Optional[Callable[[str], bool]]]
        ] = None,
        tool_definitions_provider: Optional[
            Callable[[str], tuple[ProviderToolDefinition, ...]]
        ] = None,
        v2_pipeline_enabled: bool = False,
        context_engine_v2_enabled: bool = False,
        context_window_tokens: Optional[int] = None,
        compaction_hook: Optional[ContextCompactionHook] = None,
        metrics_collector: Optional[RuntimeV2MetricsCollector] = None,
        agent_timeout_seconds: Optional[float] = None,
        approval_timeout_seconds: Optional[float] = None,
        tool_execution_limits: Optional[ToolExecutionLimits] = None,
        trace_observer: Optional[RunTraceObserver] = None,
        span_recorder: Optional[object] = None,
        # G1 item 5: no-progress StopPolicy enforcement flag passthrough.
        no_progress_enforcement_enabled: bool = False,
        # C4: 升级（无进展/预算将尽）的上下文预算阈值。
        escalation_budget_ratio: float = DEFAULT_BUDGET_RATIO,
        # C1: 独立验证模式（0 关 / 1 全部 / side_effects 仅关键运行）与验证模型。
        verifier_mode: str = "0",
        verifier_model: Optional[str] = None,
        # B5: 用户画像块提供器（conversation_id -> 画像块）；None = 不注入。
        user_profile_provider: Optional[
            Callable[[str], Optional[UserProfileBlock]]
        ] = None,
        # C5: 成本可见（定价表）与可选的单次运行成本上限。
        cost_cap_usd: float = 0.0,
        pricing_catalog: Optional[PricingCatalog] = None,
        provider_retry_evaluator: Optional[object] = None,
        # S8: 只读信任策略（None = 全部按 approval_mode 处理）。
        tool_trust_policy: Optional[object] = None,
    ) -> None:
        self._chat_repository = chat_repository
        self._repository = repository
        self._provider = provider
        self._provider_resolver = provider_resolver
        self._tool_registry = tool_registry
        self._tool_filter_provider = tool_filter_provider
        self._tool_definitions_provider = tool_definitions_provider
        self._v2_pipeline_enabled = v2_pipeline_enabled
        self._context_engine_v2_enabled = context_engine_v2_enabled
        self._context_window_tokens = context_window_tokens
        self._compaction_hook = compaction_hook
        self._metrics = metrics_collector or RuntimeV2MetricsCollector()
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._agent_timeout_seconds = agent_timeout_seconds
        self._approval_timeout_seconds = approval_timeout_seconds
        self._tool_execution_limits = tool_execution_limits
        self._trace_observer = trace_observer
        self._span_recorder = span_recorder
        self._no_progress_enforcement_enabled = bool(
            no_progress_enforcement_enabled
        )
        self._escalation_budget_ratio = float(escalation_budget_ratio)
        self._verifier_mode = verifier_mode
        self._verifier_model = verifier_model
        self._user_profile_provider = user_profile_provider
        self._cost_cap_usd = float(cost_cap_usd)
        self._tool_trust_policy = tool_trust_policy
        self._pricing_catalog = pricing_catalog or make_default_catalog()
        # M3A RS-1 (G1-2): shadow provider-retry wiring; evaluator None =
        # legacy behavior. The observer is bound per run at launch so
        # retry decisions attribute to the correct run.
        self._provider_retry_evaluator = provider_retry_evaluator
        self._temperature = temperature
        self._provider_slot = (
            asyncio.Semaphore(provider_slot_limit)
            if provider_slot_limit is not None
            else None
        )
        self._replay_service = RuntimeV2ReplayService(repository)
        self._projection = ProductRuntimeEventProjection(repository)
        self._lane_service = RuntimeV2LaneService(repository)
        self._memory_repository = memory_repository
        self._context_prefix_messages = tuple(context_prefix_messages)
        self._context_prefix_builder: Optional[
            Callable[
                [str, str, str, str],
                Awaitable[Sequence[ProviderMessage]],
            ]
        ] = None
        self._run_completion_callback: Optional[
            Callable[[str], Awaitable[None]]
        ] = None
        self._active_run_supervisor = BackgroundTaskSupervisor(
            name="runtime-v2-runs",
            logger=logger,
        )
        self._post_run_supervisor = BackgroundTaskSupervisor(
            name="runtime-v2-post-run",
            logger=logger,
        )
        self._memory_service = (
            RuntimeV2MemoryService(
                chat_repository=chat_repository,
                runtime_repository=repository,
                memory_repository=memory_repository,
            )
            if memory_repository is not None
            else None
        )
        self._active_runs: dict[str, _ActiveRun] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._lock = asyncio.Lock()
        self._approval_gate = GatewayToolApprovalGate(
            repository=repository,
            pending_approvals=self._pending_approvals,
            lock=self._lock,
            metrics=self._metrics,
            approval_timeout_seconds=self._approval_timeout_seconds,
        )
        self.driver = AgentSessionDriver(self)

    def set_run_completion_callback(
        self,
        callback: Optional[Callable[[str], Awaitable[None]]],
    ) -> None:
        self._run_completion_callback = callback

    @property
    def _post_run_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return self._post_run_supervisor.tasks

    def set_context_prefix_builder(
        self,
        builder: Optional[
            Callable[[str, str, str, str], Awaitable[Sequence[ProviderMessage]]]
        ],
    ) -> None:
        self._context_prefix_builder = builder

    def validate_session(self, conversation_id: str) -> None:
        self._chat_repository.get_conversation(conversation_id)

    def connect(self, conversation_id: str) -> AgentSessionConnection:
        return self.driver.connect(conversation_id)

    def snapshot(
        self,
        conversation_id: str,
        *,
        lane_id: Optional[str] = None,
    ) -> dict[str, object]:
        self.validate_session(conversation_id)
        product_events = self.project_events(conversation_id)
        last_event_seq = product_events[-1].event_seq if product_events else 0
        pending = self.pending_approvals(conversation_id)
        pointer = self._repository.get_conversation_pointer(conversation_id)
        if pointer is None:
            return {
                "snapshotVersion": 1,
                "conversationId": conversation_id,
                "activeLaneId": None,
                "mainLaneId": None,
                "runningLaneId": None,
                "runningRunId": None,
                "activeRunId": None,
                "activeRunVariantId": None,
                "lastEventSeq": last_event_seq,
                "entries": (),
                "runState": None,
                "pendingApprovals": tuple(
                    _approval_json(item) for item in pending
                ),
                "toolStates": (),
                "contextUsage": {"inputTokens": 0, "outputTokens": 0},
                "contextBudget": _context_budget_json(0, self._context_window_tokens),
                "interruptedRuns": self._recovery_reports_json(conversation_id),
                "stuck": None,
                "escalation": None,
                "verification": None,
                "usage": None,
                "capabilities": V2_CAPABILITIES,
            }

        runtime_snapshot = self._replay_service.build_conversation_snapshot(
            conversation_id,
            lane_id=lane_id,
        )
        return {
            "snapshotVersion": 1,
            "conversationId": conversation_id,
            "activeLaneId": runtime_snapshot.active_lane_id,
            "mainLaneId": runtime_snapshot.main_lane_id,
            "runningLaneId": runtime_snapshot.running_lane_id,
            "runningRunId": runtime_snapshot.running_run_id,
            "activeRunId": runtime_snapshot.active_run_id,
            "activeRunVariantId": runtime_snapshot.active_run_variant_id,
            "lastEventSeq": last_event_seq,
            "entries": tuple(
                _entry_json(entry) for entry in runtime_snapshot.entries
            ),
            "runState": _run_state_json(runtime_snapshot),
            "pendingApprovals": tuple(_approval_json(item) for item in pending),
            "toolStates": _tool_states_json(runtime_snapshot),
            "contextUsage": {
                "inputTokens": runtime_snapshot.input_tokens,
                "outputTokens": runtime_snapshot.output_tokens,
            },
            "contextBudget": _context_budget_json(
                _last_turn_tokens(runtime_snapshot),
                self._context_window_tokens,
                cumulative=(runtime_snapshot.input_tokens or 0)
                + (runtime_snapshot.output_tokens or 0),
            ),
            "interruptedRuns": self._recovery_reports_json(conversation_id),
            "stuck": _stuck_json(
                product_events,
                running_run_id=runtime_snapshot.running_run_id,
            ),
            "escalation": _escalation_json(
                product_events,
                running_run_id=runtime_snapshot.running_run_id,
            ),
            "verification": _verification_json(
                product_events,
                run_id=runtime_snapshot.active_run_id
                or runtime_snapshot.running_run_id,
            ),
            "usage": _usage_json(
                runtime_snapshot.active_run,
                catalog=self._pricing_catalog,
                first_token_latency_ms=(
                    self._metrics.first_token_latency_for_run(
                        runtime_snapshot.active_run_id
                    )
                    if runtime_snapshot.active_run_id is not None
                    else None
                ),
            ),
            "capabilities": V2_CAPABILITIES,
        }

    def recovery_reports(
        self,
        conversation_id: str,
    ) -> tuple[CrashRecoveryReport, ...]:
        self.validate_session(conversation_id)
        active_run_ids = {
            active.run_id
            for active in self._active_runs.values()
            if not active.task.done()
        }
        return tuple(
            report
            for report in self._replay_service.audit_interrupted_runs(
                conversation_id=conversation_id
            )
            if report.record.id not in active_run_ids
        )

    def _recovery_reports_json(
        self,
        conversation_id: str,
    ) -> tuple[dict[str, object], ...]:
        reports = self.recovery_reports(conversation_id)
        return tuple(_recovery_report_json(report) for report in reports)

    def metrics_summary(self) -> dict[str, object]:
        return self._metrics.summary()

    def project_events(
        self,
        conversation_id: str,
    ) -> tuple[ProductRuntimeEventRecord, ...]:
        self.validate_session(conversation_id)
        try:
            return self._projection.project_conversation(conversation_id)
        except Exception:
            logger.exception(
                "Failed to project runtime v2 events",
                extra={"conversation_id": conversation_id},
            )
            return self._repository.list_product_events(conversation_id)

    async def shutdown(self) -> None:
        async with self._lock:
            active_runs = tuple(self._active_runs.values())
        for active in active_runs:
            await active.executor.request_cancel()
        await self._active_run_supervisor.shutdown(cancel=False)
        await self._post_run_supervisor.shutdown(cancel=False)

    def _find_active_run(self, run_id: str) -> Optional[_ActiveRun]:
        for active in self._active_runs.values():
            if active.run_id == run_id and not active.task.done():
                return active
        return None

    def _require_no_active_runs(self, conversation_id: str) -> None:
        active = self._active_runs.get(conversation_id)
        if active is not None and not active.task.done():
            raise ConflictError("Conversation already has an active v2 run")
        persisted_active = self._repository.list_runs(
            conversation_id=conversation_id,
            statuses=(
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.COMPACTING,
                RunStatus.CANCELLING,
            ),
        )
        if persisted_active:
            raise ConflictError(
                "Conversation has an interrupted v2 run requiring recovery"
            )

    def _require_memory_service(self) -> RuntimeV2MemoryService:
        if self._memory_service is None:
            raise ConflictError("Runtime v2 memory service is unavailable")
        return self._memory_service
