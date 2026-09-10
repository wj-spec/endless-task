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
from .models import PendingApproval, derive_approval_risk, logger
from ..metrics import ApprovalMetric, RuntimeV2MetricsCollector
from ..trace_observer import RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from ..replay import RunReplayResult, CrashRecoveryReport, ConversationRuntimeSnapshot, RuntimeV2ReplayService


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
        meta = dict(prompt.metadata)
        meta.setdefault("toolName", tool.definition.name)
        meta.setdefault("effect", tool.definition.effect.value)
        meta["risk"] = derive_approval_risk(tool.definition.effect.value, tool.definition.name)
        pending = PendingApproval(
            approval_id=approval_id,
            run_id=model_turn.run_id,
            model_turn_id=model_turn.id,
            tool_execution_id=execution.id,
            tool_name=tool.definition.name,
            summary=prompt.summary,
            reason=prompt.reason,
            metadata=meta,
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
        *,
        modified_arguments: Optional[Mapping[str, object]] = None,
    ) -> bool:
        async with self._lock:
            future = self._waiters.get(approval_id)
            if future is None or future.done():
                return False
            pending = self._pending_approvals.get(approval_id)
            if pending is not None and modified_arguments is not None:
                # A1-modify：把用户修正的参数暂存到 pending，供执行端读取并校验。
                pending.metadata["modifiedArguments"] = dict(modified_arguments)
            if pending is not None and decision in (
                ToolApprovalDecision.APPROVE,
                ToolApprovalDecision.MODIFY,
            ):
                # S5：审批通过后把证据写到执行记录，供 eval approval_gate 判定。
                try:
                    self._repository.mark_tool_execution_approved(
                        pending.tool_execution_id, approval_id
                    )
                except Exception:  # noqa: BLE001 证据写入失败不阻断审批
                    logger.debug("Approval evidence write failed", exc_info=True)
            future.set_result(decision)
            return True

    def modified_arguments_for(self, approval_id: str) -> Optional[Mapping[str, object]]:
        pending = self._pending_approvals.get(approval_id)
        if pending is None:
            return None
        value = pending.metadata.get("modifiedArguments") if isinstance(pending.metadata, dict) else None
        return value if isinstance(value, dict) else None

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
