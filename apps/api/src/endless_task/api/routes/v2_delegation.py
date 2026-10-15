"""v2 委派子运行路由（前端页签：列表 + 取消）。

为什么单独一个模块：这两条路由的族是**委派**，不是会话——`v2_conversations.py`
装的是发言/快照/lane/SSE 那条会话主链，委派的读控面自成一套（同一个页签后面
还要接"应用产出"，也需要 HTTP 面）。路径挂在会话集合下，是因为子运行本来就按
会话限定；`api/app.py` 里显式注册。

边界与状态派生**都不在这里**：列表/取消交给
``CoordinatorDelegationHandler.list_children_for_conversation`` /
``cancel_child_for_conversation``（会话限定的唯一实现），状态词表与已用时派生
复用 ``delegation.runtime_handler`` 的同一份函数。这里只做两件事：把库行拼成
JSON（`delegation/*.py` 不允许导入 fastapi，所以序列化留在路由层）、把 handler
的错误翻译成 `ApiRequestError`。

范围裁剪：页签的"应用产出"动作不在本批——`apply_child_patches` 目前只有模型工具
面，没有 HTTP 路径；为它新造写路由属于另一个任务。本模块只做"看状态 + 取消"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI

from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation import (
    ChildRunRow,
    child_run_elapsed_seconds,
    child_run_status,
    truncate_child_task,
)

from ..container import AppContainer
from ..errors import ApiRequestError
from ..v2_conversation_support import resolve_runtime_v2_conversation

#: 摘要截断上限。任务摘要与工具面/注入块同源（200 字，复用
#: ``truncate_child_task``）；结果摘要只在这里限长，避免一屏文把页签撑爆。
SUMMARY_MAX_CHARACTERS = 2_000

#: HTTP 取消是用户点击，理由与工具面的默认值同款。
CANCEL_REASON = "用户取消"


def register_v2_delegation_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/api/v2/conversations/{conversation_id}/delegated-children")
    async def list_delegated_children(conversation_id: str) -> dict[str, object]:
        """本会话派过的子运行（最近创建的在前，最多 50 条）。

        空集合是正常 200：还没派过活与"这个会话不存在"都由空列表回答，不额外
        泄漏会话的存在性。
        """
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        handler = container.delegation_handler
        if handler is None:
            # 装配没开委派：不可能有子运行，回空集合而不是错误（页签据此不渲染）。
            return {"items": (), "total": 0}
        rows = handler.list_children_for_conversation(target_conversation_id)
        now = datetime.now(timezone.utc)
        items = tuple(_delegated_child_json(row, now=now) for row in rows)
        return {"items": items, "total": len(items)}


    @app.post(
        "/api/v2/conversations/{conversation_id}"
        "/delegated-children/{child_run_id}/cancel"
    )
    async def cancel_delegated_child(
        conversation_id: str,
        child_run_id: str,
    ) -> dict[str, object]:
        """取消本会话的一个子运行；不属于本会话的子运行一律 404（不泄漏存在性）。"""
        target_conversation_id = resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        handler = container.delegation_handler
        if handler is None:
            # 没开委派就没有子运行：与"跨会话"一样按查无此子运行回答。
            raise _unknown_child_error()
        try:
            status = await handler.cancel_child_for_conversation(
                target_conversation_id,
                child_run_id,
                CANCEL_REASON,
            )
        except AgentPlatformError as error:
            raise _cancel_error(error) from error
        return {"childRunId": child_run_id, "status": status}


def _delegated_child_json(
    row: ChildRunRow, *, now: datetime
) -> dict[str, object]:
    """一行子运行的页签载荷（字段固定，多一个少一个都算接口变了）。

    ``status`` / ``elapsedSeconds`` 复用工具面的派生：线上词表固定为
    `queued | running | completed | failed | cancelled | interrupted`，终态行的
    已用时按 `terminal_at` 收口（不随当前时间增长）。``summary`` 是子代理自己的
    文本（保留换行），只做限长。
    """
    return {
        "childRunId": row.child_run_id,
        "status": child_run_status(row),
        "task": truncate_child_task(row.task),
        "createdAt": row.created_at,
        "startedAt": row.started_at,
        "terminalAt": row.terminal_at,
        "elapsedSeconds": child_run_elapsed_seconds(row, now),
        "summary": _truncate_summary(row.summary),
        "errorCode": row.error_code,
        "safeMessage": row.safe_message,
        "workspaceMode": row.workspace_mode,
    }


def _truncate_summary(summary: Optional[str]) -> Optional[str]:
    """结果摘要限长（截断标记与任务摘要同款；没有摘要就保持 None）。"""
    if summary is None or len(summary) <= SUMMARY_MAX_CHARACTERS:
        return summary
    return summary[:SUMMARY_MAX_CHARACTERS] + "…"


def _unknown_child_error() -> ApiRequestError:
    return ApiRequestError(
        "delegation_unknown_child",
        "该会话下没有这个子运行。",
        status_code=404,
    )


def _cancel_error(error: AgentPlatformError) -> ApiRequestError:
    """把 handler 的错误翻译成 HTTP（未列出的按冲突处理，不吞成 500）。

    只有"会话边界"这一种是 404：不属于本会话的子运行等于不存在（不泄漏存在
    性），消息换成中文固定文案（handler 里那句是给模型看的英文）；"拥有者不在
    本进程"是 409（可重试），别的内部错误也照 409 报出来，页签至少能看到原因。
    """
    if error.code == "delegation_unknown_child":
        return _unknown_child_error()
    return ApiRequestError(
        error.code,
        error.safe_message,
        status_code=409,
        details=error.details,
    )
