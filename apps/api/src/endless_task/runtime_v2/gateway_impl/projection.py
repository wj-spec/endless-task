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
from .models import derive_approval_risk
from .views import _string


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
            meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            effect = str(meta.get("effect") or "")
            tool_name = str(meta.get("toolName") or "")
            return "approval.requested", {
                "runId": event.run_id,
                "approvalId": _string(payload, "approvalId"),
                "toolExecutionId": _string(payload, "toolExecutionId"),
                "toolName": tool_name,
                "summary": _string(payload, "summary"),
                "reason": _string(payload, "reason"),
                "effect": effect,
                "risk": str(meta.get("risk") or derive_approval_risk(effect, tool_name)),
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
        if event_type == "run_auto_restored":
            return "run.auto_restored", {
                "runId": event.run_id,
                "errorCode": _string(payload, "errorCode"),
                "workspaces": payload.get("workspaces"),
                "restored": payload.get("restored"),
                "skipped": payload.get("skipped"),
                "trigger": _string(payload, "trigger"),
            }
        if event_type == "run_stuck":
            return "run.stuck", {
                "runId": event.run_id,
                "level": _string(payload, "level"),
                "detector": _string(payload, "detector"),
                "reasons": list(payload.get("reasons") or ()),
                "consecutive": payload.get("consecutive"),
                "repeatedFailures": list(payload.get("repeatedFailures") or ()),
                "attempts": list(payload.get("attempts") or ()),
                "guidance": _string(payload, "guidance"),
            }
        if event_type == "run_progress_resumed":
            return "run.progress_resumed", {"runId": event.run_id}
        if event_type == "run_verifying":
            return "run.verifying", {
                "runId": event.run_id,
                "model": _string(payload, "model"),
            }
        if event_type == "run_verified":
            return "run.verified", {
                "runId": event.run_id,
                "verdict": _string(payload, "verdict"),
                "reasons": list(payload.get("reasons") or ()),
                "missing": list(payload.get("missing") or ()),
                "model": _string(payload, "model"),
                "latencyMs": payload.get("latencyMs"),
                "inputTokens": payload.get("inputTokens"),
                "outputTokens": payload.get("outputTokens"),
            }
        if event_type == "run_awaiting_user":
            return "run.awaiting_user", {
                "runId": event.run_id,
                "reason": _string(payload, "reason"),
                "summary": _string(payload, "summary"),
                "options": list(payload.get("options") or ()),
                "progress": payload.get("progress") or {},
                "budget": payload.get("budget") or {},
                "repeatedFailures": list(payload.get("repeatedFailures") or ()),
                "failures": list(payload.get("failures") or ()),
                "guidance": _string(payload, "guidance"),
                "willStop": payload.get("willStop", False),
                "verdict": payload.get("verdict"),
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
