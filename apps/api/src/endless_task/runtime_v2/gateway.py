from __future__ import annotations

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
from .replay import (
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


logger = logging.getLogger(__name__)


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


@dataclass
class _ActiveRun:
    run_id: str
    task: asyncio.Task[object]
    executor: AgentRunExecutor
    cancellation_token: CancellationToken


class GatewayToolApprovalGate(ToolApprovalGate):
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        pending_approvals: dict[str, PendingApproval],
        lock: asyncio.Lock,
        metrics: Optional[RuntimeV2MetricsCollector] = None,
        approval_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._repository = repository
        self._pending_approvals = pending_approvals
        self._lock = lock
        self._metrics = metrics
        if approval_timeout_seconds is not None and approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")
        self._approval_timeout_seconds = approval_timeout_seconds
        self._waiters: dict[str, asyncio.Future[ToolApprovalDecision]] = {}

    async def decide(
        self,
        execution: ToolExecutionRecord,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolApprovalDecision:
        approval_id = execution.id
        model_turn = self._repository.get_model_turn(execution.model_turn_id)
        prompt = self._approval_prompt(tool, call)
        pending = PendingApproval(
            approval_id=approval_id,
            run_id=model_turn.run_id,
            model_turn_id=model_turn.id,
            tool_execution_id=execution.id,
            tool_name=tool.definition.name,
            summary=prompt.summary,
            reason=prompt.reason,
            metadata=dict(prompt.metadata),
        )
        future = asyncio.get_running_loop().create_future()
        async with self._lock:
            if approval_id in self._pending_approvals:
                raise ConflictError("Tool execution is already waiting for approval")
            self._pending_approvals[approval_id] = pending
            self._waiters[approval_id] = future

        self._repository.transition_tool_execution_status(
            execution.id,
            ToolExecutionStatus.WAITING_APPROVAL,
            event_type="tool_execution_status_changed",
            payload={
                "toolExecutionId": execution.id,
                "status": ToolExecutionStatus.WAITING_APPROVAL.value,
            },
        )
        self._repository.transition_run_status(
            model_turn.run_id,
            RunStatus.WAITING_APPROVAL,
            event_type="run_status_changed",
            payload={"status": RunStatus.WAITING_APPROVAL.value},
        )
        self._repository.append_runtime_event(
            run_id=model_turn.run_id,
            model_turn_id=model_turn.id,
            event_type="approval_requested",
            payload={
                "approvalId": approval_id,
                "toolExecutionId": execution.id,
                "summary": pending.summary,
                "reason": pending.reason,
                "metadata": pending.metadata,
            },
        )

        wait_started = asyncio.get_running_loop().time()
        decision: Optional[ToolApprovalDecision] = None
        try:
            decision = await self._wait_for_decision(
                approval_id,
                future,
                execution,
                cancellation_token,
            )
        finally:
            if self._metrics is not None:
                self._metrics.record_approval(
                    ApprovalMetric(
                        run_id=model_turn.run_id,
                        approval_id=approval_id,
                        wait_ms=int(
                            (asyncio.get_running_loop().time() - wait_started) * 1000
                        ),
                        decision=(
                            decision.value
                            if decision is not None
                            else "cancelled"
                        ),
                    )
                )
            async with self._lock:
                self._pending_approvals.pop(approval_id, None)
                self._waiters.pop(approval_id, None)

        if decision is ToolApprovalDecision.EXPIRE:
            self._repository.append_runtime_event(
                run_id=model_turn.run_id,
                model_turn_id=model_turn.id,
                event_type="approval_expired",
                payload={
                    "approvalId": approval_id,
                    "toolExecutionId": execution.id,
                },
            )
        else:
            self._repository.append_runtime_event(
                run_id=model_turn.run_id,
                model_turn_id=model_turn.id,
                event_type="approval_resolved",
                payload={
                    "approvalId": approval_id,
                    "decision": decision.value,
                    "toolExecutionId": execution.id,
                },
            )
        self._repository.transition_run_status(
            model_turn.run_id,
            RunStatus.RUNNING,
            event_type="run_status_changed",
            payload={"status": RunStatus.RUNNING.value},
        )
        return decision

    async def resolve(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
    ) -> bool:
        async with self._lock:
            future = self._waiters.get(approval_id)
            if future is None or future.done():
                return False
            future.set_result(decision)
            return True

    async def _wait_for_decision(
        self,
        approval_id: str,
        future: asyncio.Future[ToolApprovalDecision],
        execution: ToolExecutionRecord,
        cancellation_token: CancellationToken,
    ) -> ToolApprovalDecision:
        del approval_id, execution
        decision_task = asyncio.ensure_future(future)
        cancellation_task = asyncio.ensure_future(cancellation_token.wait())
        tasks = (decision_task, cancellation_task)
        try:
            if self._approval_timeout_seconds is None:
                done, _pending = await asyncio.wait(
                    tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                async with asyncio.timeout(self._approval_timeout_seconds):
                    done, _pending = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            if cancellation_task in done:
                raise RuntimeCancelled()
            return decision_task.result()
        except TimeoutError:
            return ToolApprovalDecision.EXPIRE
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _approval_prompt(
        tool: RegisteredTool,
        call: ToolCall,
    ) -> ToolApprovalPrompt:
        factory = getattr(tool, "approval_prompt", None)
        if callable(factory):
            prompt = factory(call)
            if isinstance(prompt, ToolApprovalPrompt):
                return prompt
            raise ToolValidationError(
                "invalid_approval_prompt",
                "Tool approval prompt must use the runtime protocol.",
            )
        impact = (
            "这会修改本机数据。"
            if tool.definition.effect is ToolEffect.LOCAL_WRITE
            else "这会向本机之外的服务发起操作。"
        )
        return ToolApprovalPrompt(
            summary=f"允许 Endless 执行“{tool.definition.name}”吗？",
            reason=f"{tool.definition.description} {impact}",
            metadata={
                "toolName": tool.definition.name,
                "effect": tool.definition.effect.value,
            },
        )


class ProductRuntimeEventProjection:
    def __init__(self, repository: "SqliteRuntimeV2Repository") -> None:
        self._repository = repository

    def project_conversation(
        self,
        conversation_id: str,
    ) -> tuple[ProductRuntimeEventRecord, ...]:
        source_events = self._repository.list_conversation_runtime_events(
            conversation_id
        )
        lane_events = self._repository.list_conversation_lane_events(conversation_id)
        known_ids = self._repository.list_product_event_source_ids(conversation_id)
        runs = {
            run.id: run
            for run in self._repository.list_runs(conversation_id=conversation_id)
        }
        sources: tuple[
            tuple[str, str, RuntimeEventRecord | LaneEventRecord],
            ...,
        ] = (
            *((event.occurred_at, event.event_id, event) for event in source_events),
            *((event.occurred_at, event.event_id, event) for event in lane_events),
        )
        for _, _, event in sorted(sources, key=lambda item: (item[0], item[1])):
            if event.event_id in known_ids:
                continue
            if isinstance(event, LaneEventRecord):
                event_type = event.event_type
                data = dict(event.data)
                run_id = None
                lane_id = event.lane_id
                occurred_at = event.occurred_at
            else:
                run = runs.get(event.run_id)
                projection = self._project_event(event, run)
                if projection is None:
                    continue
                event_type, data = projection
                run_id = event.run_id
                lane_id = run.lane_id if run is not None else None
                occurred_at = event.occurred_at
            self._repository.append_product_event(
                conversation_id=conversation_id,
                event_type=event_type,
                run_id=run_id,
                lane_id=lane_id,
                source_event_id=event.event_id,
                occurred_at=occurred_at,
                data=data,
            )
        return self._repository.list_product_events(conversation_id)

    @staticmethod
    def _project_event(
        event: RuntimeEventRecord,
        run: Optional[RunRecord],
    ) -> Optional[tuple[str, dict[str, object]]]:
        event_type = event.event_type
        payload = event.payload
        if event_type == "run_started":
            return "run.started", {"runId": event.run_id, "status": "running"}
        if event_type == "run_status_changed":
            return "run.status_changed", {
                "runId": event.run_id,
                "status": _string(payload, "status"),
            }
        if event_type == "run_completed":
            return "run.finished", {
                "runId": event.run_id,
                "assistantEntryId": _string(payload, "assistantEntryId"),
            }
        if event_type == "run_failed":
            return "run.failed", {
                "runId": event.run_id,
                "errorCode": _string(payload, "errorCode"),
                "safeMessage": _string(payload, "safeMessage"),
            }
        if event_type == "run_cancelled":
            return "run.cancelled", {"runId": event.run_id}
        if event_type == "run_variant_selected":
            return "run_variant.changed", {
                "runId": event.run_id,
                "assistantEntryId": _string(payload, "assistantEntryId"),
                "siblingGroupId": _string(payload, "siblingGroupId"),
            }
        if event_type in {"model_turn_status_changed"}:
            return "model_turn.updated", {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "status": _string(payload, "status"),
            }
        if event_type == "model_turn_started":
            return "model_turn.started", {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
            }
        if event_type == "model_turn_completed":
            return "model_turn.completed", {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "inputTokens": payload.get("inputTokens"),
                "outputTokens": payload.get("outputTokens"),
            }
        if event_type in {"model_turn_failed", "model_turn_cancelled"}:
            product_type = (
                "model_turn.failed"
                if event_type == "model_turn_failed"
                else "model_turn.cancelled"
            )
            return product_type, {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "errorCode": _string(payload, "errorCode"),
                "safeMessage": _string(payload, "safeMessage"),
            }
        if event_type == "model_text_delta":
            delta = _string(payload, "delta") or _string(payload, "text") or ""
            return "message.updated", {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "delta": delta,
            }
        if event_type.startswith("tool_execution_"):
            suffix = event_type.removeprefix("tool_execution_")
            if suffix in {"created", "status_changed"}:
                product_type = "tool_execution.updated"
            elif suffix in {
                "started",
                "completed",
                "failed",
                "cancelled",
                "rejected",
                "expired",
            }:
                product_type = f"tool_execution.{suffix}"
            else:
                return None
            return product_type, {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "toolExecutionId": _string(payload, "toolExecutionId"),
                "status": _string(payload, "status"),
                "resultEntryId": _string(payload, "resultEntryId"),
                # 实时展示增强：把工具名/调用 id/参数/结果错误随事件下发给前端，
                # 让运行中的工具卡片不等快照刷新即可显示。
                "callId": payload.get("callId"),
                "toolName": _string(payload, "toolName"),
                "arguments": payload.get("arguments"),
                "content": _string(payload, "content"),
                "errorCode": _string(payload, "errorCode"),
                "safeMessage": _string(payload, "safeMessage"),
                "retryable": payload.get("retryable"),
                "correlationId": _string(payload, "correlationId"),
                "errorDetails": payload.get("errorDetails"),
            }
        if event_type == "tool_progress_update":
            return "tool_execution.progress", {
                "runId": event.run_id,
                "modelTurnId": event.model_turn_id,
                "toolExecutionId": _string(payload, "toolExecutionId"),
                "message": _string(payload, "message"),
                "percent": payload.get("percent"),
            }
        if event_type == "approval_requested":
            return "approval.requested", {
                "runId": event.run_id,
                "approvalId": _string(payload, "approvalId"),
                "toolExecutionId": _string(payload, "toolExecutionId"),
                "summary": _string(payload, "summary"),
                "reason": _string(payload, "reason"),
            }
        if event_type == "approval_resolved":
            return "approval.resolved", {
                "runId": event.run_id,
                "approvalId": _string(payload, "approvalId"),
                "toolExecutionId": _string(payload, "toolExecutionId"),
                "decision": _string(payload, "decision"),
            }
        if event_type == "approval_expired":
            return "approval.expired", {
                "runId": event.run_id,
                "approvalId": _string(payload, "approvalId"),
                "toolExecutionId": _string(payload, "toolExecutionId"),
            }
        if event_type == "compaction_started":
            return "context.compaction_started", {"runId": event.run_id}
        if event_type == "compaction_completed":
            return "context.compaction_completed", {
                "runId": event.run_id,
                "changed": payload.get("changed", False),
                "summaryEntryId": _string(payload, "summaryEntryId"),
            }
        if event_type == "compaction_failed":
            return "context.compaction_failed", {
                "runId": event.run_id,
                "safeMessage": _string(payload, "safeMessage"),
            }
        if event_type == "steer_injected":
            return "steer.injected", {
                "runId": event.run_id,
                "content": _string(payload, "content"),
            }
        if event_type == "plan_updated":
            return "plan.updated", {
                "runId": event.run_id,
                "planEntryId": _string(payload, "planEntryId"),
                "title": _string(payload, "title"),
                "steps": payload.get("steps"),
                "currentStepIndex": payload.get("currentStepIndex"),
            }
        if event_type == "safety_stop":
            return "run.status_changed", {
                "runId": event.run_id,
                "status": "failed",
                "reason": _string(payload, "reason"),
                "safeMessage": _string(payload, "safeMessage"),
            }
        del run
        return None


class AgentSessionConnection:
    def __init__(self, gateway: "RuntimeV2SessionGateway", conversation_id: str):
        self._gateway = gateway
        self.conversation_id = conversation_id

    async def send(
        self,
        content: str,
        *,
        lane_id: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> RuntimeV2SendResult:
        return await self._gateway.send(
            self.conversation_id,
            content,
            lane_id=lane_id,
            client_request_id=client_request_id,
        )

    async def steer(self, run_id: str, content: str) -> bool:
        return await self._gateway.steer(run_id, content)

    async def cancel(self, run_id: str) -> bool:
        return await self._gateway.cancel(run_id)

    async def resolve_approval(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
    ) -> bool:
        return await self._gateway.resolve_approval(approval_id, decision)

    def snapshot(self) -> dict[str, object]:
        return self._gateway.snapshot(self.conversation_id)

    def events(self) -> tuple[ProductRuntimeEventRecord, ...]:
        return self._gateway.project_events(self.conversation_id)


class AgentSessionDriver:
    driver_type = "endless-agent-v2"
    capabilities = AgentRuntimeCapabilities(
        driver_type="endless-agent-v2",
        capabilities=V2_CAPABILITIES,
    )

    def __init__(self, gateway: "RuntimeV2SessionGateway") -> None:
        self._gateway = gateway

    def validate_session(self, conversation_id: str) -> None:
        self._gateway.validate_session(conversation_id)

    def connect(self, conversation_id: str) -> AgentSessionConnection:
        self.validate_session(conversation_id)
        return AgentSessionConnection(self._gateway, conversation_id)


class RuntimeV2SessionGateway:
    """In-process Session Gateway for the v2 runtime."""

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

    async def send(
        self,
        conversation_id: str,
        content: str,
        *,
        lane_id: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> RuntimeV2SendResult:
        content = content.strip()
        if not content:
            raise ConflictError("Message content cannot be empty")
        request_id = (client_request_id or f"runtime-v2-{uuid.uuid4().hex}").strip()
        if not request_id:
            raise ConflictError("Idempotency key cannot be empty")
        if len(request_id) > 200:
            raise ConflictError("Idempotency key is too long")
        self.validate_session(conversation_id)
        async with self._lock:
            submission = self._repository.create_message_submission(
                conversation_id=conversation_id,
                lane_id=lane_id,
                content=content,
                client_request_id=request_id,
            )
            run = submission.run
            if submission.created:
                run = await self._launch_run(run)
            return RuntimeV2SendResult(
                conversation_id=conversation_id,
                lane_id=submission.lane.id,
                run_id=run.id,
                user_message_id=submission.user_entry.id,
            )

    async def create_lane_branch(
        self,
        *,
        conversation_id: str,
        source_lane_id: str,
        base_entry_id: str,
        kind: LaneKind = LaneKind.PERSISTENT_BRANCH,
        display_name: Optional[str] = None,
    ) -> RuntimeV2LaneCreationResult:
        self.validate_session(conversation_id)
        if kind is not LaneKind.PERSISTENT_BRANCH:
            raise ConflictError("Temporary conversations are created with the temporary conversation API")
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            return self._lane_service.create_branch(
                conversation_id=conversation_id,
                source_lane_id=source_lane_id,
                base_entry_id=base_entry_id,
                display_name=display_name,
            )

    def list_lanes(
        self,
        conversation_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[LaneRecord, ...]:
        self.validate_session(conversation_id)
        return self._repository.list_lanes(
            conversation_id,
            include_archived=include_archived,
        )

    def list_memories(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        run_id: Optional[str] = None,
    ) -> tuple[RuntimeV2MemoryRecord, ...]:
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.query(
            conversation_id=conversation_id,
            lane_id=lane_id,
            run_id=run_id,
        ).records

    def create_lane_memory(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        kind: str,
        content: str,
        source_entry_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> RuntimeV2MemoryRecord:
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.create_lane_memory(
            conversation_id=conversation_id,
            lane_id=lane_id,
            kind=kind,
            content=content,
            source_entry_id=source_entry_id,
            expires_at=expires_at,
        )

    def create_run_memory(
        self,
        *,
        conversation_id: str,
        run_id: str,
        kind: str,
        content: str,
        source_entry_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> RuntimeV2MemoryRecord:
        self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.create_run_memory(
            conversation_id=conversation_id,
            run_id=run_id,
            kind=kind,
            content=content,
            source_entry_id=source_entry_id,
            expires_at=expires_at,
        )

    def create_memory_promotion(
        self,
        *,
        memory_id: str,
        target_scope: MemoryScope,
        target_lane_id: Optional[str] = None,
    ) -> RuntimeV2MemoryPromotion:
        self._require_memory_service()
        memory = self._memory_repository.get_memory(memory_id)
        self.validate_session(memory.conversation_id)
        return self._memory_service.create_promotion(
            memory_id=memory_id,
            target_scope=target_scope,
            target_lane_id=target_lane_id,
        )

    def list_memory_promotions(
        self,
        *,
        conversation_id: Optional[str] = None,
        source_memory_id: Optional[str] = None,
        include_resolved: bool = False,
    ) -> tuple[RuntimeV2MemoryPromotion, ...]:
        if conversation_id is not None:
            self.validate_session(conversation_id)
        self._require_memory_service()
        return self._memory_service.list_promotions(
            conversation_id=conversation_id,
            source_memory_id=source_memory_id,
            include_resolved=include_resolved,
        )

    def resolve_memory_promotion(
        self,
        promotion_id: str,
        *,
        accept: bool,
    ) -> tuple[RuntimeV2MemoryPromotion, Optional[RuntimeV2MemoryRecord]]:
        self._require_memory_service()
        promotion = self._memory_repository.get_promotion(promotion_id)
        memory = self._memory_repository.get_memory(promotion.source_memory_id)
        self.validate_session(memory.conversation_id)
        return self._memory_service.resolve_promotion(
            promotion_id,
            accept=accept,
        )

    async def promote_lane(
        self,
        *,
        conversation_id: str,
        target_lane_id: str,
    ) -> LanePromotionRecord:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            return self._lane_service.promote_lane(
                conversation_id=conversation_id,
                target_lane_id=target_lane_id,
            )

    async def rename_lane(
        self,
        lane_id: str,
        display_name: Optional[str],
    ) -> LaneRecord:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.rename_lane(lane_id, display_name)

    async def archive_lane(
        self,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.archive_lane(
                conversation_id=lane.conversation_id,
                lane_id=lane_id,
            )

    async def restore_lane(
        self,
        lane_id: str,
    ) -> tuple[LaneRecord, ...]:
        lane = self._repository.get_lane(lane_id)
        self.validate_session(lane.conversation_id)
        async with self._lock:
            self._require_no_active_runs(lane.conversation_id)
            return self._lane_service.restore_lane(
                conversation_id=lane.conversation_id,
                lane_id=lane_id,
            )

    async def create_temporary_conversation(
        self,
        *,
        source_conversation_id: str,
        source_lane_id: str,
        source_leaf_entry_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[str, LaneRecord]:
        self.validate_session(source_conversation_id)
        pointer = self._repository.get_conversation_pointer(source_conversation_id)
        if pointer is None:
            raise ConflictError("Conversation has no v2 main lane")
        if source_lane_id != pointer.active_lane_id:
            raise ConflictError(
                "Temporary conversations must snapshot the current main lane"
            )
        main_lane = self._repository.get_lane(pointer.active_lane_id)
        if source_leaf_entry_id not in (None, main_lane.leaf_entry_id):
            raise ConflictError(
                "Temporary conversations must snapshot the complete main lane path"
            )
        source_leaf_entry_id = main_lane.leaf_entry_id
        async with self._lock:
            self._require_no_active_runs(source_conversation_id)
            conversation_id, lane, _provenance = (
                self._repository.create_temporary_conversation_from_lane(
                    source_conversation_id=source_conversation_id,
                    source_lane_id=source_lane_id,
                    source_leaf_entry_id=source_leaf_entry_id,
                    title=title,
                )
            )
            return conversation_id, lane

    async def promote_temporary_conversation(
        self,
        conversation_id: str,
    ) -> None:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            self._repository.promote_temporary_conversation(conversation_id)

    async def delete_temporary_conversation(self, conversation_id: str) -> None:
        self.validate_session(conversation_id)
        async with self._lock:
            self._require_no_active_runs(conversation_id)
            self._repository.delete_temporary_conversation(conversation_id)

    async def resolve_recovery(
        self,
        run_id: str,
        *,
        retry: bool,
    ) -> RuntimeV2RecoveryResult:
        self._chat_repository.get_conversation(
            self._repository.get_run(run_id).conversation_id
        )
        async with self._lock:
            run = self._repository.get_run(run_id)
            if self._find_active_run(run_id) is not None:
                raise ConflictError("Interrupted run is still active in this process")
            recovery = self._replay_service.classify_crash_recovery(run_id)
            if retry and not recovery.can_auto_resume:
                raise ConflictError("Interrupted run cannot be safely retried")
            self._repository.finalize_interrupted_run(
                run_id,
                deactivate_variant=retry,
            )
            if not retry:
                return RuntimeV2RecoveryResult(
                    run_id=run_id,
                    action="mark_failed",
                    lane_id=run.lane_id,
                )

            new_run = await self._start_run(
                conversation_id=run.conversation_id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )
            return RuntimeV2RecoveryResult(
                run_id=run_id,
                action="retry",
                lane_id=run.lane_id,
                new_run_id=new_run.id,
            )

    def list_run_variants(self, run_id: str) -> tuple[RunRecord, ...]:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        return self._repository.list_run_variants(run_id)

    async def regenerate_run(self, run_id: str) -> RuntimeV2RegenerateResult:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        async with self._lock:
            self._require_no_active_runs(run.conversation_id)
            if run.status not in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                raise ConflictError("Only a terminal run can be regenerated")
            new_run = await self._start_run(
                conversation_id=run.conversation_id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )
            return RuntimeV2RegenerateResult(
                old_run_id=run.id,
                new_run_id=new_run.id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )

    async def select_run_variant(self, run_id: str) -> RunRecord:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        async with self._lock:
            self._require_no_active_runs(run.conversation_id)
            if run.status is not RunStatus.COMPLETED:
                raise ConflictError("Only a completed run variant can be selected")
            if run.assistant_entry_id is None:
                raise ConflictError("Run variant has no assistant entry")
            selected = self._repository.set_active_run_variant(run_id)
            self._repository.append_runtime_event(
                run_id=selected.id,
                event_type="run_variant_selected",
                payload={
                    "assistantEntryId": selected.assistant_entry_id,
                    "siblingGroupId": selected.sibling_group_id,
                },
            )
            return selected

    async def steer(self, run_id: str, content: str) -> bool:
        content = content.strip()
        if not content:
            raise ConflictError("Steering content cannot be empty")
        active = self._find_active_run(run_id)
        if active is None:
            return False
        return await active.executor.enqueue_user_message(content)

    async def cancel(self, run_id: str) -> bool:
        active = self._find_active_run(run_id)
        if active is None:
            return False
        await active.executor.request_cancel()
        return True

    async def resolve_approval(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
    ) -> bool:
        return await self._approval_gate.resolve(approval_id, decision)

    def has_active_run(self, conversation_id: str) -> bool:
        active = self._active_runs.get(conversation_id)
        return active is not None and not active.task.done()

    def pending_approvals(self, conversation_id: str) -> tuple[PendingApproval, ...]:
        return tuple(
            approval
            for approval in self._pending_approvals.values()
            if self._repository.get_run(approval.run_id).conversation_id
            == conversation_id
        )

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
                "interruptedRuns": self._recovery_reports_json(conversation_id),
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
            "interruptedRuns": self._recovery_reports_json(conversation_id),
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

    async def _start_run(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        trigger_entry_id: str,
        sibling_group_id: Optional[str] = None,
    ) -> RunRecord:
        run = self._repository.create_run(
            conversation_id=conversation_id,
            lane_id=lane_id,
            trigger_entry_id=trigger_entry_id,
            sibling_group_id=sibling_group_id,
            is_active_variant=True,
        )
        return await self._launch_run(run)

    async def _launch_run(self, run: RunRecord) -> RunRecord:
        conversation_id = run.conversation_id
        lane_id = run.lane_id
        trigger_entry = self._repository.get_entry(run.trigger_entry_id)
        trigger_content = trigger_entry.payload.get("content", "")
        product_context_messages = (
            await self._context_prefix_builder(
                conversation_id,
                lane_id,
                run.id,
                trigger_content if isinstance(trigger_content, str) else "",
            )
            if self._context_prefix_builder is not None
            else ()
        )
        token = CancellationToken()
        memory_context_messages = (
            self._memory_service.context_messages(
                conversation_id=conversation_id,
                lane_id=lane_id,
                run_id=run.id,
            )
            if self._memory_service is not None
            else ()
        )
        context_prefix_messages = (
            *self._context_prefix_messages,
            *product_context_messages,
            *memory_context_messages,
        )
        self._metrics.record_prefix_fingerprint(
            conversation_id=conversation_id,
            fingerprint=_prefix_fingerprint(context_prefix_messages),
        )
        selected_provider = self._provider
        selected_model = self._model
        if self._provider_resolver is not None:
            selection = self._provider_resolver(
                self._chat_repository.get_conversation(conversation_id)
            )
            selected_provider = selection.provider
            selected_model = selection.model
        executor = AgentRunExecutor(
            repository=self._repository,
            provider=selected_provider,
            tool_registry=self._tool_registry,
            model=selected_model,
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            approval_gate=self._approval_gate,
            provider_slot=self._provider_slot,
            context_prefix_messages=context_prefix_messages,
            tool_filter_provider=self._tool_filter_provider,
            tool_definitions_provider=self._tool_definitions_provider,
            v2_pipeline_enabled=self._v2_pipeline_enabled,
            context_engine_v2_enabled=self._context_engine_v2_enabled,
            context_window_tokens=self._context_window_tokens,
            compaction_hook=self._compaction_hook,
            metrics=self._metrics,
            agent_timeout_seconds=self._agent_timeout_seconds,
            tool_execution_limits=self._tool_execution_limits,
        )
        task = self._active_run_supervisor.spawn(
            executor.execute(
                run.id,
                cancellation_token=token,
            ),
            name=f"runtime-v2-run:{run.id}",
            on_done=(
                lambda finished, key=conversation_id: self._on_run_done(
                    key,
                    finished,
                )
            ),
        )
        self._active_runs[conversation_id] = _ActiveRun(
            run_id=run.id,
            task=task,
            executor=executor,
            cancellation_token=token,
        )
        return run

    def _on_run_done(self, conversation_id: str, task: asyncio.Task[object]) -> None:
        active = self._active_runs.get(conversation_id)
        if active is not None and active.task is task:
            self._active_runs.pop(conversation_id, None)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            return
        if (
            isinstance(result, RunExecutionResult)
            and result.status is RunStatus.COMPLETED
            and self._run_completion_callback is not None
        ):
            self._post_run_supervisor.spawn(
                self._post_run_completed(result.run.id),
                name=f"runtime-v2-post-run:{result.run.id}",
            )

    async def _post_run_completed(self, run_id: str) -> None:
        callback = self._run_completion_callback
        if callback is None:
            return
        try:
            await callback(run_id)
        except Exception:
            logger.exception(
                "Runtime v2 post-run processing failed",
                extra={"run_id": run_id},
            )


def _prefix_fingerprint(
    messages: Sequence[ProviderMessage],
) -> str:
    """前两条 system 前缀消息的内容指纹,用于估计前缀缓存稳定度。"""
    import hashlib

    stable_prefix = [message for message in messages if message.role == "system"][:2]
    joined = "\n".join(message.content for message in stable_prefix)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def product_event_json(event: ProductRuntimeEventRecord) -> dict[str, object]:
    return {
        "eventId": event.id,
        "eventSeq": event.event_seq,
        "type": event.event_type,
        "conversationId": event.conversation_id,
        "laneId": event.lane_id,
        "runId": event.run_id,
        "createdAt": event.occurred_at,
        "data": event.data,
    }


def _entry_json(entry: TranscriptEntryRecord) -> dict[str, object]:
    data: dict[str, object] = {}
    if entry.type in {
        TranscriptEntryType.USER_MESSAGE,
        TranscriptEntryType.ASSISTANT_MESSAGE,
        TranscriptEntryType.CONTEXT_SUMMARY,
    }:
        data["content"] = entry.payload.get("content", "")
    elif entry.type is TranscriptEntryType.TOOL_CALL:
        data["toolName"] = entry.payload.get("toolName", "")
        data["arguments"] = _bounded_json(entry.payload.get("arguments", {}))
        data["callId"] = entry.payload.get("callId")
        data["toolExecutionId"] = entry.display.get("toolExecutionId")
    elif entry.type is TranscriptEntryType.TOOL_RESULT:
        data["toolName"] = entry.payload.get("toolName", "")
        data["content"] = _bounded_text(entry.payload.get("content", ""))
        data["errorCode"] = entry.payload.get("errorCode")
        data["trustLevel"] = "untrusted"
        data["callId"] = entry.payload.get("callId")
        data["structuredContent"] = entry.payload.get("structuredContent")
    else:
        data["reference"] = entry.payload
    return {
        "id": entry.id,
        "type": entry.type.value,
        "actor": entry.actor.value,
        "status": entry.status.value,
        "createdAt": entry.created_at,
        "sourceRunId": entry.source_run_id,
        "inherited": isinstance(entry.display.get("sourceEntryId"), str),
        "data": data,
    }


def _run_state_json(snapshot: ConversationRuntimeSnapshot) -> Optional[dict[str, object]]:
    if snapshot.active_run is None:
        return None
    replay = snapshot.active_run
    return {
        "runId": replay.record.id,
        "status": replay.record.status.value,
        "partialContent": replay.partial_content,
        "errorCode": replay.record.error_code,
        "safeMessage": replay.record.safe_message,
        "modelTurns": tuple(
            {
                "id": turn.record.id,
                "index": turn.record.turn_index,
                "status": turn.record.status.value,
                "partialContent": turn.partial_content,
                "inputTokens": turn.record.input_tokens,
                "outputTokens": turn.record.output_tokens,
                "toolExecutions": tuple(
                    {
                        "id": tool.record.id,
                        "callId": tool.record.call_id,
                        "toolName": tool.record.tool_name,
                        "status": tool.record.status.value,
                        "errorCode": tool.record.error_code,
                        "safeMessage": tool.record.safe_message,
                        "retryable": tool.record.retryable,
                        "correlationId": tool.record.correlation_id,
                        "errorDetails": tool.record.error_details,
                        "resultEntryId": tool.record.result_entry_id,
                    }
                    for tool in turn.tool_executions
                ),
            }
            for turn in replay.model_turns
        ),
    }


def _tool_states_json(
    snapshot: ConversationRuntimeSnapshot,
) -> tuple[dict[str, object], ...]:
    if snapshot.active_run is None:
        return ()
    return tuple(
        {
            "id": tool.record.id,
            "runId": snapshot.active_run.record.id,
            "modelTurnId": turn.record.id,
            "callId": tool.record.call_id,
            "toolName": tool.record.tool_name,
            "status": tool.record.status.value,
            "errorCode": tool.record.error_code,
            "safeMessage": tool.record.safe_message,
            "retryable": tool.record.retryable,
            "correlationId": tool.record.correlation_id,
            "errorDetails": tool.record.error_details,
            "resultEntryId": tool.record.result_entry_id,
        }
        for turn in snapshot.active_run.model_turns
        for tool in turn.tool_executions
    )


def _approval_json(approval: PendingApproval) -> dict[str, object]:
    return {
        "id": approval.approval_id,
        "runId": approval.run_id,
        "modelTurnId": approval.model_turn_id,
        "toolExecutionId": approval.tool_execution_id,
        "toolName": approval.tool_name,
        "summary": approval.summary,
        "reason": approval.reason,
    }


def _recovery_report_json(report: CrashRecoveryReport) -> dict[str, object]:
    return {
        "runId": report.record.id,
        "status": report.record.status.value,
        "classification": report.classification.value,
        "action": report.action.value,
        "findings": tuple(
            {
                "reason": finding.reason.value,
                "message": finding.message,
                "modelTurnId": finding.model_turn_id,
                "toolExecutionId": finding.tool_execution_id,
            }
            for finding in report.findings
        ),
    }


def _string(payload: Mapping[str, object], key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _bounded_text(value: object) -> str:
    text = value if isinstance(value, str) else ""
    if len(text) <= _SNAPSHOT_UNTRUSTED_MAX_CHARS:
        return text
    return text[: _SNAPSHOT_UNTRUSTED_MAX_CHARS - 1] + "…"


def _bounded_json(value: object) -> object:
    try:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": "<无法序列化的参数>"}
    if len(serialized) <= _SNAPSHOT_UNTRUSTED_MAX_CHARS:
        return value
    return {
        "truncated": True,
        "preview": serialized[: _SNAPSHOT_UNTRUSTED_MAX_CHARS - 1] + "…",
    }
