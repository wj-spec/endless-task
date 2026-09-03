"""update_plan 工具:模型把执行计划固化为可追溯的会话记录(可演进的计划视图)。

协议(路径 A:计划由 Agent 判断后主动声明,执行在 Run 内完成):

- 首次调用:``plan``(标题)+ ``steps``(全量步骤列表)声明计划;
- 执行中:``plan`` + ``update={index, status}`` 更新单步状态
  (status 由模型声明,系统不强制流转:pending/in_progress/completed/skipped/failed);
- 计划变化:重新提供 ``steps`` 全量重写(增/删/改/重排);
- 每次调用追加新 plan entry(append-only),**最新 entry 的 payload 即权威计划**;
  系统不推导步骤、不设模板、不校验状态顺序。

Plan entry 进入 lane 的 entry tree,上下文投影把结构化 plan 渲染为
system 消息供模型对照;``plan_updated`` 事件携带结构化 payload 供前端展示。

effect 声明为 READ_ONLY:plan 是 runtime 内部执行记录,不产生文件系统或
外部副作用,因此不需要审批。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, TYPE_CHECKING

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    RegisteredTool,
    ToolActivityCopy,
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
)

from .domain import Actor, TranscriptEntryType

if TYPE_CHECKING:
    from endless_task.storage.sqlite_runtime_v2_repository import (
        SqliteRuntimeV2Repository,
    )


_STATUS_ORDER = ("pending", "in_progress", "completed", "skipped", "failed")


class UpdatePlanTool(RegisteredTool):
    definition = ToolDefinition(
        name="update_plan",
        description=(
            "把当前任务的执行计划固化为会话记录(仅多步骤任务需要)。首次调用传入 "
            "plan(计划标题)与 steps(步骤列表)声明计划;每完成一个关键步骤或计划有 "
            "变化时,再次调用并传入 update={index, status} 更新该步状态,或重新传入 "
            "steps 重写整个计划。计划会进入上下文供后续步骤对照,并在界面展示进度。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "minLength": 1},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "skipped",
                                    "failed",
                                ],
                            },
                        },
                        "required": ["title"],
                        "additionalProperties": False,
                    },
                    "minItems": 1,
                    "maxItems": 20,
                },
                "update": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "minimum": 0},
                        "status": {
                            "type": "string",
                            "enum": [
                                "pending",
                                "in_progress",
                                "completed",
                                "skipped",
                                "failed",
                            ],
                        },
                    },
                    "required": ["index", "status"],
                    "additionalProperties": False,
                },
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,  # 内部执行记录，无文件系统/外部副作用
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=4_000,
    )

    def __init__(self, repository: "SqliteRuntimeV2Repository") -> None:
        self._repository = repository

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        del call
        return ToolActivityCopy(
            running="正在更新执行计划",
            completed="执行计划已更新",
            failed="计划更新失败",
            cancelled="计划更新已停止",
        )

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        cancellation_token.raise_if_cancelled()
        title = call.require_argument("plan", str).strip()
        if not title:
            raise ToolError(
                "empty_plan",
                "计划标题不能为空。",
                retryable=False,
            )
        run = self._repository.get_run(call.response_variant_id)
        if run.conversation_id != call.conversation_id:
            raise ToolError(
                "plan_run_mismatch",
                "计划与当前执行上下文不匹配。",
                retryable=False,
            )

        steps = self._resolve_steps(
            lane_id=run.lane_id,
            declared=(
                call.require_argument("steps", list)
                if "steps" in call.arguments
                else None
            ),
            update=(
                call.require_argument("update", dict)
                if "update" in call.arguments
                else None
            ),
        )
        current_step_index = _current_step_index(steps)
        payload: dict[str, Any] = {
            "title": title,
            "steps": steps,
            "currentStepIndex": current_step_index,
        }
        entry = self._repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=run.lane_id,
            type=TranscriptEntryType.PLAN,
            actor=Actor.ASSISTANT,
            payload=payload,
            context_policy={"include_in_llm": True, "transform": "full"},
            display={"source": "update_plan"},
            source_run_id=run.id,
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="plan_updated",
            payload={
                "planEntryId": entry.id,
                "title": title,
                "steps": steps,
                "currentStepIndex": current_step_index,
            },
        )
        return ToolResult(
            tool_call_id=call.id,
            content=render_plan(payload),
        )

    def _resolve_steps(
        self,
        *,
        lane_id: str,
        declared: object,
        update: object,
    ) -> list[dict[str, str]]:
        latest = self._latest_plan_payload(lane_id)
        latest_steps = latest.get("steps") if latest is not None else None

        if declared is not None:
            steps = _normalize_steps(declared)
            if not steps:
                raise ToolError(
                    "empty_plan_steps",
                    "计划步骤不能为空。",
                    retryable=False,
                )
        elif isinstance(latest_steps, list) and latest_steps:
            steps = _normalize_steps(latest_steps)
        else:
            raise ToolError(
                "plan_steps_required",
                "首次记录计划时必须提供步骤列表(steps)。",
                retryable=False,
            )

        if update is not None:
            steps = _apply_step_update(steps, update)
        return steps

    def _latest_plan_payload(self, lane_id: str) -> Optional[Mapping[str, Any]]:
        entries = self._repository.list_lane_context_entries(lane_id)
        for entry in reversed(entries):
            if entry.type is TranscriptEntryType.PLAN:
                return entry.payload
        return None


def _normalize_steps(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        status = str(item.get("status") or "").strip()
        if status not in _STATUS_ORDER:
            status = "pending"
        steps.append({"title": title, "status": status})
    return steps


def _apply_step_update(
    steps: list[dict[str, str]],
    update: object,
) -> list[dict[str, str]]:
    if not isinstance(update, dict):
        return steps
    index = update.get("index")
    status = str(update.get("status") or "").strip()
    if not isinstance(index, int) or not 0 <= index < len(steps) or not status:
        raise ToolError(
            "invalid_plan_update",
            "步骤更新必须提供有效 index 与 status。",
            retryable=False,
        )
    updated = [dict(step) for step in steps]
    updated[index]["status"] = status if status in _STATUS_ORDER else "pending"
    return updated


def _current_step_index(steps: list[dict[str, str]]) -> Optional[int]:
    for index, step in enumerate(steps):
        if step.get("status") == "in_progress":
            return index
    return None


_STATUS_MARKER = {
    "pending": "⬜",
    "in_progress": "（进行中）",
    "completed": "✅",
    "skipped": "（跳过）",
    "failed": "✗",
}


def render_plan(payload: Mapping[str, Any]) -> str:
    """把结构化 plan 渲染为可读文本(工具结果与上下文投影共用)。"""
    title = str(payload.get("title") or payload.get("content") or "")
    steps = payload.get("steps")
    lines = [f"执行计划：{title}"]
    if isinstance(steps, list):
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            step_title = str(step.get("title") or "")
            status = str(step.get("status") or "pending")
            marker = _STATUS_MARKER.get(status, "⬜")
            lines.append(f"[{index + 1}] {step_title} {marker}")
    if not lines[1:]:
        lines.append("（计划尚未包含步骤）")
    return "\n".join(lines)
