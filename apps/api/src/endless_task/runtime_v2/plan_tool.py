"""update_plan 工具:模型把执行计划固化为可追溯的会话记录。

Plan entry(type=plan)进入 lane 的 entry tree,后续上下文投影把 plan 作为
system 消息注入,供模型在执行中对照;plan 更新 = 追加新 plan entry
(append-only,历史保留,不覆盖旧计划)。

effect 声明为 READ_ONLY:plan 是 runtime 内部执行记录,不产生文件系统或
外部副作用,因此不需要审批。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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


class UpdatePlanTool(RegisteredTool):
    definition = ToolDefinition(
        name="update_plan",
        description=(
            "把当前任务的执行计划固化为会话记录。在执行多步骤任务前调用一次，"
            "计划会进入上下文供后续步骤对照；计划有变化时再次调用即可（保留历史）。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8000,
                },
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,  # 内部执行记录，无文件系统/外部副作用
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=10.0,
        max_output_characters=2_000,
    )

    def __init__(self, repository: "SqliteRuntimeV2Repository") -> None:
        self._repository = repository

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        del call
        return ToolActivityCopy(
            running="正在记录执行计划",
            completed="执行计划已记录",
            failed="计划记录失败",
            cancelled="计划记录已停止",
        )

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        cancellation_token.raise_if_cancelled()
        plan = str(call.arguments.get("plan") or "").strip()
        if not plan:
            raise ToolError(
                "empty_plan",
                "计划内容不能为空。",
                retryable=False,
            )
        run = self._repository.get_run(call.response_variant_id)
        if run.conversation_id != call.conversation_id:
            raise ToolError(
                "plan_run_mismatch",
                "计划与当前执行上下文不匹配。",
                retryable=False,
            )
        entry = self._repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=run.lane_id,
            type=TranscriptEntryType.PLAN,
            actor=Actor.ASSISTANT,
            payload={"content": plan},
            context_policy={"include_in_llm": True, "transform": "full"},
            display={"source": "update_plan"},
            source_run_id=run.id,
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="plan_updated",
            payload={"planEntryId": entry.id, "content": plan},
        )
        return ToolResult(
            tool_call_id=call.id,
            content=(
                "执行计划已记录，后续步骤请对照该计划执行：\n"
                + plan
            ),
        )
