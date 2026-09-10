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
from .models import _SNAPSHOT_UNTRUSTED_MAX_CHARS, derive_approval_risk


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


def _context_budget_json(
    used: int,
    limit: Optional[int],
    *,
    cumulative: Optional[int] = None,
) -> dict[str, object]:
    """上下文预算。

    ``usedTokens`` 是**当前占用**（最近一轮请求的 input+output），也就是"离窗口
    上限还差多少"；``cumulativeTokens`` 是本 run 所有轮次累计消耗，用于成本参考。
    早先 usedTokens 用的是累计值，导致几轮工具调用就把进度条推到 60%（误导）。
    """
    limit = limit or 32_768
    used = max(0, int(used))
    ratio = min(1.0, used / limit) if limit else 0.0
    return {
        "limitTokens": limit,
        "usedTokens": used,
        "usedRatio": round(ratio, 4),
        "remainingTokens": max(0, limit - used),
        "cumulativeTokens": max(0, int(cumulative)) if cumulative is not None else used,
    }


def _last_turn_tokens(snapshot) -> int:
    """最近一轮的上下文占用（input+output）；没有轮次时为 0。"""
    active_run = getattr(snapshot, "active_run", None)
    turns = getattr(active_run, "model_turns", None) or ()
    if not turns:
        return 0
    last = turns[-1].record
    return int(last.input_tokens or 0) + int(last.output_tokens or 0)


def _stuck_json(
    product_events: tuple[ProductRuntimeEventRecord, ...],
    *,
    running_run_id: Optional[str],
) -> Optional[dict[str, object]]:
    """C2：把最近一条 ``run.stuck`` 投影成快照里的卡住态。

    卡住态是运行期的外部状态：``run.stuck`` 出现即置位，
    ``run.progress_resumed``（同一运行）出现即清空。刷新页面后前端依然能
    从快照恢复"卡在哪、试过什么、为什么失败"。
    """
    if running_run_id is None:
        return None
    stuck: Optional[dict[str, object]] = None
    for event in product_events:
        if event.run_id != running_run_id:
            continue
        if event.event_type == "run.stuck":
            stuck = {
                "runId": running_run_id,
                "level": event.data.get("level") or "remind",
                "detector": event.data.get("detector") or "",
                "reasons": list(event.data.get("reasons") or ()),
                "consecutive": event.data.get("consecutive"),
                "repeatedFailures": list(
                    event.data.get("repeatedFailures") or ()
                ),
                "attempts": list(event.data.get("attempts") or ()),
                "guidance": event.data.get("guidance") or "",
            }
        elif event.event_type == "run.progress_resumed":
            stuck = None
    return stuck


def _escalation_json(
    product_events: tuple[ProductRuntimeEventRecord, ...],
    *,
    running_run_id: Optional[str],
) -> Optional[dict[str, object]]:
    """C4：把最近一条 ``run.awaiting_user`` 投影成快照里的升级报告。"""
    if running_run_id is None:
        return None
    escalation: Optional[dict[str, object]] = None
    for event in product_events:
        if event.run_id != running_run_id:
            continue
        if event.event_type == "run.awaiting_user":
            data = event.data
            escalation = {
                "runId": running_run_id,
                "reason": data.get("reason") or "no_progress",
                "summary": data.get("summary") or "",
                "options": list(data.get("options") or ()),
                "progress": data.get("progress") or {},
                "budget": data.get("budget") or {},
                "repeatedFailures": list(data.get("repeatedFailures") or ()),
                "failures": list(data.get("failures") or ()),
                "guidance": data.get("guidance") or "",
                "willStop": bool(data.get("willStop")),
            }
        elif event.event_type == "run.progress_resumed":
            escalation = None
    return escalation


def _verification_json(
    product_events: tuple[ProductRuntimeEventRecord, ...],
    *,
    run_id: Optional[str],
) -> Optional[dict[str, object]]:
    """C1：最近一次独立验证的结论（``run.verifying`` 进行中 → 状态为 verifying）。"""
    if run_id is None:
        return None
    verification: Optional[dict[str, object]] = None
    for event in product_events:
        if event.run_id != run_id:
            continue
        if event.event_type == "run.verifying":
            verification = {
                "runId": run_id,
                "status": "verifying",
                "verdict": None,
                "reasons": [],
                "missing": [],
                "model": event.data.get("model") or "",
                "latencyMs": None,
            }
        elif event.event_type == "run.verified":
            data = event.data
            verification = {
                "runId": run_id,
                "status": "verified",
                "verdict": data.get("verdict") or "uncertain",
                "reasons": list(data.get("reasons") or ()),
                "missing": list(data.get("missing") or ()),
                "model": data.get("model") or "",
                "latencyMs": data.get("latencyMs"),
                "inputTokens": data.get("inputTokens"),
                "outputTokens": data.get("outputTokens"),
            }
    return verification


def _usage_json(
    active_run: Optional[RunReplayResult],
    *,
    catalog: PricingCatalog,
    first_token_latency_ms: Optional[int],
) -> Optional[dict[str, object]]:
    """C5：当前 run 的用量/成本/耗时（成本按定价表估算，未定价明确标注）。"""
    if active_run is None:
        return None
    summary = build_run_usage_summary(
        run=active_run.record,
        model_turns=tuple(turn.record for turn in active_run.model_turns),
        catalog=catalog,
        first_token_latency_ms=first_token_latency_ms,
    )
    return summary.as_json()


def _approval_json(approval: PendingApproval) -> dict[str, object]:
    effect = str(approval.metadata.get("effect") or "")
    return {
        "id": approval.approval_id,
        "runId": approval.run_id,
        "modelTurnId": approval.model_turn_id,
        "toolExecutionId": approval.tool_execution_id,
        "toolName": approval.tool_name,
        "summary": approval.summary,
        "reason": approval.reason,
        "effect": effect,
        "risk": str(
            approval.metadata.get("risk")
            or derive_approval_risk(effect, approval.tool_name)
        ),
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
