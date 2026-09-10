"""审计轨迹事实构造（从 app.py 搬出，供 v2 路由族共用）。

行为零改动：字段、顺序、兜底值与搬家前一致。
"""

from __future__ import annotations

from endless_task.runtime_v2.audit_trail import AuditFact
from endless_task.runtime_v2.gateway import derive_approval_risk
from typing import Optional

_AUDIT_TERMINAL_TOOL_EVENTS = (
    "tool_execution_completed",
    "tool_execution_failed",
    "tool_execution_rejected",
    "tool_execution_expired",
    "tool_execution_cancelled",
)


def audit_tool_name(container, payload) -> str:
    """工具名优先取事件载荷，其次回查工具执行记录（终态事件不带 toolName）。"""
    name = str(payload.get("toolName") or "")
    if name:
        return name
    execution_id = payload.get("toolExecutionId")
    if not execution_id:
        return ""
    try:
        return container.runtime_v2_repository.get_tool_execution(
            str(execution_id)
        ).tool_name
    except Exception:  # noqa: BLE001 记录缺失时不影响轨迹
        return ""


def audit_tool_title(event_type: str, tool_name: str) -> str:
    label = tool_name or "工具"
    if event_type == "tool_execution_failed":
        return f"{label} 执行失败"
    if event_type == "tool_execution_completed":
        return f"{label} 执行完成"
    return f"{label} 状态变化"


def audit_approval_title(decision: str) -> str:
    return {
        "approve": "你批准了这次操作",
        "deny": "你拒绝了这次操作",
        "modify": "你修改参数后批准",
    }.get(decision, "审批状态变化")


def audit_tool_effect(container, tool_name: str) -> str:
    if not tool_name:
        return ""
    try:
        definition = container.tool_registry.resolve(tool_name).definition
    except Exception:  # noqa: BLE001 动态工具可能不在注册表里
        return ""
    effect = getattr(definition.effect, "value", definition.effect)
    return str(effect or "")


def audit_fact_from_event(container, event) -> Optional[AuditFact]:
    """把一条运行事件归一化成审计事实（不认识的类型返回 None）。"""
    event_type = getattr(event, "event_type", "")
    payload = dict(getattr(event, "payload", {}) or {})
    occurred_at = getattr(event, "occurred_at", "") or ""
    fact_id = getattr(event, "event_id", "") or f"{event_type}:{occurred_at}"
    if event_type in ("run_started", "run_completed", "run_failed", "run_cancelled"):
        titles = {
            "run_started": "开始运行",
            "run_completed": "运行完成",
            "run_failed": "运行失败",
            "run_cancelled": "运行被取消",
        }
        return AuditFact(
            fact_id=fact_id,
            kind="run",
            occurred_at=occurred_at,
            title=titles[event_type],
            summary=str(payload.get("safeMessage") or ""),
            error_code=str(payload.get("errorCode") or ""),
        )
    if event_type in _AUDIT_TERMINAL_TOOL_EVENTS:
        # 只保留终态，避免同一次调用出现 4 条（created/status/started/completed）。
        tool_name = audit_tool_name(container, payload)
        return AuditFact(
            fact_id=fact_id,
            kind="tool",
            occurred_at=occurred_at,
            title=audit_tool_title(event_type, tool_name),
            summary=str(payload.get("safeMessage") or ""),
            tool_name=tool_name,
            effect=audit_tool_effect(container, tool_name),
            error_code=str(payload.get("errorCode") or ""),
            payload={
                "refs": {
                    "toolExecutionId": payload.get("toolExecutionId"),
                    "callId": payload.get("callId"),
                }
            },
        )
    if event_type == "approval_requested":
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        tool_name = str(metadata.get("toolName") or "")
        effect = str(metadata.get("effect") or "")
        return AuditFact(
            fact_id=fact_id,
            kind="approval",
            occurred_at=occurred_at,
            title="请求确认",
            summary=str(payload.get("summary") or ""),
            tool_name=tool_name,
            effect=effect,
            risk=str(metadata.get("risk") or derive_approval_risk(effect, tool_name)),
            payload={
                "refs": {
                    "approvalId": payload.get("approvalId"),
                    "toolExecutionId": payload.get("toolExecutionId"),
                }
            },
        )
    if event_type == "approval_resolved":
        decision = str(payload.get("decision") or "")
        tool_name = audit_tool_name(container, payload)
        effect = audit_tool_effect(container, tool_name)
        return AuditFact(
            fact_id=fact_id,
            kind="approval",
            occurred_at=occurred_at,
            title=audit_approval_title(decision),
            decision=decision,
            tool_name=tool_name,
            effect=effect,
            risk=derive_approval_risk(effect, tool_name) if tool_name else "",
            payload={
                "refs": {
                    "approvalId": payload.get("approvalId"),
                    "toolExecutionId": payload.get("toolExecutionId"),
                }
            },
        )
    if event_type == "plan_updated":
        steps = payload.get("steps")
        return AuditFact(
            fact_id=fact_id,
            kind="plan",
            occurred_at=occurred_at,
            title="更新执行计划",
            summary=str(payload.get("title") or ""),
            payload={"refs": {"planEntryId": payload.get("planEntryId")}, "steps": steps},
        )
    if event_type == "run_stuck":
        return AuditFact(
            fact_id=fact_id,
            kind="escalation",
            occurred_at=occurred_at,
            title="检测到卡住",
            summary=str(payload.get("guidance") or ""),
            payload={
                "reason": "no_progress",
                "summary": "；".join(
                    str(item) for item in (payload.get("reasons") or ())
                ),
            },
        )
    if event_type == "run_awaiting_user":
        return AuditFact(
            fact_id=fact_id,
            kind="escalation",
            occurred_at=occurred_at,
            title="升级：需要你决定下一步",
            summary=str(payload.get("summary") or ""),
            payload={
                "reason": payload.get("reason"),
                "summary": payload.get("summary"),
                "options": payload.get("options"),
            },
        )
    if event_type == "run_verified":
        return AuditFact(
            fact_id=fact_id,
            kind="verification",
            occurred_at=occurred_at,
            title=f"独立验证：{payload.get('verdict') or '未知'}",
            summary=str(payload.get("model") or ""),
            payload={
                "verdict": payload.get("verdict"),
                "reasons": payload.get("reasons"),
                "missing": payload.get("missing"),
            },
        )
    return None
