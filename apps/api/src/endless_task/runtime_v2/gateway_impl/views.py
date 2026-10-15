from __future__ import annotations

from __future__ import annotations
from endless_task.runtime.provider import ProviderToolDefinition
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
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
from ..replay import RunReplayResult, CrashRecoveryReport, ConversationRuntimeSnapshot, RuntimeV2ReplayService, UNFINISHED_TOOL_STATUSES
from .models import _SNAPSHOT_UNTRUSTED_MAX_CHARS, derive_approval_risk


def _parse_iso_moment(value: object) -> Optional[datetime]:
    """把运行行上的 ISO 时间戳解析成带时区的 datetime（解析不了返回 None）。

    **解析失败必须是 None，不能退化成"现在"**：读不到起点就不给耗时，比给一个
    看起来合理、其实是猜的值要诚实。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _elapsed_seconds(record: RunRecord, *, now: Optional[datetime] = None) -> Optional[float]:
    """一次运行已经走了多久（秒）。

    起点是 run 行上的 ``started_at``（由 ``start_run`` 写入，本任务不新增来源）；
    终点是 ``finished_at``（终态后冻结），运行中则用**当前时刻**——快照因此给的是
    一个活值，前端拿 `startedAt` 也能自己算。没有 ``started_at`` 时返回 None
    （从未开始的 run 没有耗时，不编一个 0 冒充）。
    """
    started = _parse_iso_moment(record.started_at)
    if started is None:
        return None
    finished = _parse_iso_moment(record.finished_at)
    end = finished or now or datetime.now(timezone.utc)
    return round(max(0.0, (end - started).total_seconds()), 3)


def _current_step_json(replay: RunReplayResult) -> Optional[dict[str, object]]:
    """当前步骤：第几个模型轮 + 正在跑（或最后跑过）的那个工具。

    数据来源是既有的 run 行与既有计数器（``v2_model_turns`` / ``v2_tool_executions``
    已经由执行器逐个写入），不新造来源：进行中的模型轮在库里就是一行 streaming，
    在飞的工具就是一行 running。没有任何模型轮时返回 None（还没开始干活）。

    工具按"最后一轮起往回找**未终态**的那一个"取——长活卡在一条慢工具上时，
    这正好就是当前步骤；都终态了就取最后跑过的那个（`toolTurnIndex` 说明它属于
    哪一轮，避免把上一轮的工具误读成当前轮）。
    """
    turns = replay.model_turns
    if not turns:
        return None
    pairs = [
        (turn.record.turn_index, tool.record)
        for turn in turns
        for tool in turn.tool_executions
    ]
    active = next(
        (
            pair
            for pair in reversed(pairs)
            if pair[1].status in UNFINISHED_TOOL_STATUSES
        ),
        None,
    )
    chosen = active or (pairs[-1] if pairs else None)
    return {
        "modelTurnIndex": turns[-1].record.turn_index,
        "modelTurnCount": len(turns),
        "toolName": chosen[1].tool_name if chosen is not None else None,
        "toolStatus": chosen[1].status.value if chosen is not None else None,
        "toolTurnIndex": chosen[0] if chosen is not None else None,
        "toolInFlight": active is not None,
    }


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
        # 切片 10 T2 Step 1：进度可见。起点/耗时直接来自 run 行（`started_at`
        # 由 `start_run` 写入）——耗时不落库、按快照时刻现算，所以是一个**活值**
        # 而不是每次刷新都不动的常量；`currentStep` 由既有 model_turns/tool_executions
        # 计数器投影。`budget` 由调用方（`gateway_impl/base.py`）从执行器自报的
        # `run.budget_declared` 事件补上（墙只有执行器知道）。
        "startedAt": replay.record.started_at,
        "elapsedSeconds": _elapsed_seconds(replay.record),
        "currentStep": _current_step_json(replay),
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


def _run_budget_json(
    product_events: tuple[ProductRuntimeEventRecord, ...],
    *,
    run_id: Optional[str],
    elapsed_seconds: Optional[float],
) -> Optional[dict[str, object]]:
    """把执行器声明的**运行预算墙**投影成快照里的 `runState.budget`。

    墙（`softSeconds`/`hardSeconds`/单轮上限/成本上限）的唯一真相源是构造执行器时
    给入的 `RunBudget`，因此由执行器在运行开始时发一条
    ``run_budget_declared``；网关按 run 行猜档位就是伪造（交互 run、任务 run、
    子运行各是一档）。没有这条事件时返回 None——**不退回默认档**，因为"这个 run
    的上限是多少"答不上来时，答"大概 15 分钟吧"比答"不知道"更糟。

    `usedSeconds`/`remainingSeconds` 用快照侧的活耗时（与 `runState.elapsedSeconds`
    同源）现算，这样预算条与耗时时钟是同一个数、不会各走各的。
    """
    if run_id is None:
        return None
    declared: Optional[ProductRuntimeEventRecord] = None
    for event in product_events:
        if event.event_type == "run.budget_declared" and event.run_id == run_id:
            declared = event
    if declared is None:
        return None
    data = declared.data
    hard_seconds = data.get("hardSeconds")
    remaining: Optional[float] = None
    if isinstance(hard_seconds, (int, float)) and elapsed_seconds is not None:
        remaining = round(max(0.0, float(hard_seconds) - elapsed_seconds), 3)
    return {
        "softSeconds": data.get("softSeconds"),
        "hardSeconds": hard_seconds,
        "perTurnSeconds": data.get("perTurnSeconds"),
        "costCapUsd": data.get("costCapUsd"),
        "usedSeconds": elapsed_seconds,
        "remainingSeconds": remaining,
        # 墙是执行器在运行开始时声明的，这里给出声明时刻与来源，方便前端/排障
        # 区分"真实墙"与"没有墙"。
        "source": "run_budget_declared",
        "declaredAt": declared.occurred_at,
    }


def _run_state_with_budget_json(
    snapshot: ConversationRuntimeSnapshot,
    product_events: tuple[ProductRuntimeEventRecord, ...],
) -> Optional[dict[str, object]]:
    """`runState` 加上执行器声明的预算块（`runState.budget`）。

    为什么在 `_run_state_json` **外面**合并：它的签名（`(snapshot)`）在模块面
    快照里在册，是冻结面；而预算的真相源（产品事件）本来就不在快照对象里。
    """
    run_state = _run_state_json(snapshot)
    if run_state is None:
        return None
    run_state["budget"] = _run_budget_json(
        product_events,
        run_id=run_state["runId"],
        elapsed_seconds=run_state["elapsedSeconds"],
    )
    return run_state


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


#: run 级终态事件（产品事件口径）——用来判定"这个 run 已经结束了"。
_TERMINAL_RUN_PRODUCT_EVENTS = frozenset(
    {"run.finished", "run.failed", "run.cancelled"}
)


def _awaiting_user_scope(
    product_events: tuple[ProductRuntimeEventRecord, ...],
) -> Optional[str]:
    """最近一条 ``run.awaiting_user`` 所属的 run，且该 run 已经有终态事件。

    只在"没有任何在飞运行"时用得上：硬停收尾的顺序是
    ``run_awaiting_user(willStop=true)`` **紧跟** ``run.finished``，所以运行一结束
    「已安全停止，等你决定」就不再有任何在飞的 run 可挂靠——不补这条作用域，
    快照侧永远返回 None，用户默认看不到那句提示（T2 Step 2 要修的就是这个）。

    终态判定**不看两条事件的先后**（产品事件按 `(occurred_at, event_id)` 落库，
    同一毫秒内的两条会因 id 排序而互换先后），只看"该 run 有没有终态事件"。

    切片 10 终审修复 W-B：还要看**这个 run 之后有没有别的 run 开始**。硬停之后用户
    若又发一条消息、新 run（同会话）正常跑完且从未升级，最后一条
    `run.awaiting_user` 仍属于旧 run——不看后续 run，快照就会把旧 run 的
    `willStop=true` 横幅一直挂到下一次升级（对别的消费者说谎）。事件序
    （`event_seq` 严格递增）里出现**别的 run 的 `run.started`** 就说明这一作用域已经
    过期，返回 None。这是**收窄**，不是恒定返回 None：新 run 自己升级时，它的
    `run.awaiting_user` 就是最后一条，作用域仍然给得出。
    """
    latest: Optional[ProductRuntimeEventRecord] = None
    latest_seq = -1
    for event in product_events:
        if event.event_type == "run.awaiting_user":
            latest = event
            latest_seq = max(latest_seq, int(event.event_seq))
    if latest is None or latest.run_id is None:
        return None
    superseded = any(
        event.event_type == "run.started"
        and event.run_id != latest.run_id
        and int(event.event_seq) > latest_seq
        for event in product_events
    )
    if superseded:
        return None
    ended = any(
        event.run_id == latest.run_id
        and event.event_type in _TERMINAL_RUN_PRODUCT_EVENTS
        for event in product_events
    )
    return latest.run_id if ended else None


def _escalation_json(
    product_events: tuple[ProductRuntimeEventRecord, ...],
    *,
    running_run_id: Optional[str],
) -> Optional[dict[str, object]]:
    """C4：把最近一条 ``run.awaiting_user`` 投影成快照里的升级报告。

    作用域两条：
    - 有在飞运行 → 只看这个 run（今天的行为，逐字节不变）；
    - 没有在飞运行（= run 已终态）→ 只看**最近结束**的那个 run，而且**只投影
      ``willStop=True`` 的"已安全停止/等你决定"**。软阈值提醒（willStop=False）
      在运行结束后仍不该继续挂在快照上，否则横幅永远不消失。
    """
    terminal_run = running_run_id is None
    scope = running_run_id or _awaiting_user_scope(product_events)
    if scope is None:
        return None
    escalation: Optional[dict[str, object]] = None
    for event in product_events:
        if event.run_id != scope:
            continue
        if event.event_type == "run.awaiting_user":
            data = event.data
            escalation = {
                "runId": scope,
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
    if escalation is None:
        return None
    if terminal_run and not escalation["willStop"]:
        return None
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
