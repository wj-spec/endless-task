"""Task run notifications (R4.7).

Maps finished task runs to inbox notifications. Cancelled **task** runs stay silent
(they are usually user-initiated, and a task can be re-run by hand); everything else
informs the user with a title naming the task and trigger plus a short body.

Cancelled **reminder** runs are *not* silent — see `REMINDER_CANCELLED_BODY`: a
reminder is one-shot (`mark_fired` on the way in, no manual re-run), so a silent
cancellation would leave the user with neither notification nor trace.
"""

from __future__ import annotations

import logging
from typing import Optional

from endless_task.domain.models import (
    NotificationKind,
    Reminder,
    TaskRecord,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.storage import (
    SqliteHubEventRepository,
    SqliteNotificationRepository,
)

logger = logging.getLogger(__name__)

_TRIGGER_LABEL = {
    TaskRunTrigger.MANUAL: "手动执行",
    TaskRunTrigger.SCHEDULED: "到点执行",
}

_BODY_CAP = 120

#: 提醒的一次性语义：到点即 `mark_fired`，之后不会再被调度器选中，也**没有**手动重跑
#: 入口（切片 10 T3 明确不新增能力）。重启清扫要给被中断的提醒补一条如实通知时用它，
#: 把"没重跑"这件事说清，而不是让用户以为它会自己再来一次。
#: 切片 10 终审修复 W-F：措辞**不许多断言**。被重启清扫的提醒 run 是**已经 spawn 过**
#: 的（`start_reminder` 先 `mark_fired` 再 spawn），可能已经产生工具副作用，所以
#: "本次未执行"是假的；如实的口径是"本次没有跑完"。
REMINDER_NO_RETRY_NOTE = "提醒到时即标记为已触发，本次没有跑完，不会自动重跑。"

#: 切片 10 残留 A1：提醒 run 落 `CANCELLED` 时**必须**有一条如实通知。
#:
#: 与任务 run 不同：提醒到点即 `mark_fired`、之后不会再被调度器选中，也**没有**手动
#: 重跑入口（见上面 `REMINDER_NO_RETRY_NOTE`）。所以"取消"吃掉的不只是这一次运行，
#: 而是整条提醒——用户此前**什么都不会收到**（`notify_reminder` 对非
#: COMPLETED/FAILED 的终态提前 return），提醒已 `fired` 而消息没送到。
#:
#: 文案口径：**第一句**必须把"未执行"说在前面，不许用"不会自动重跑"掩盖事实
#: （切片 10 残留）。"取消"是反事实的精确来源——打断这次运行的唯一入口是
#: `TaskWorker.cancel`（`POST /tasks/{task_id}/runs/{run_id}/cancel`），
#: 它把执行器的取消令牌打上标记。
REMINDER_CANCELLED_BODY = (
    "本次提醒未执行（运行已被取消）。提醒到点即标记为已触发，不会自动重跑。"
)


def _compact(text: Optional[str]) -> str:
    if not text:
        return ""
    normalized = " ".join(text.split())
    if len(normalized) > _BODY_CAP:
        return normalized[:_BODY_CAP] + "…"
    return normalized


class TaskNotificationService:
    def __init__(
        self,
        *,
        notification_repository: SqliteNotificationRepository,
        hub_events: Optional[SqliteHubEventRepository] = None,
    ) -> None:
        self._repository = notification_repository
        self._hub_events = hub_events

    def _record_and_emit(
        self,
        *,
        kind: NotificationKind,
        task_id: str,
        run_id: str,
        conversation_id: str,
        title: str,
        body: str,
        turn_id: Optional[str] = None,
    ) -> None:
        created = self._repository.record(
            kind=kind,
            task_id=task_id,
            run_id=run_id,
            conversation_id=conversation_id,
            title=title,
            body=body,
            turn_id=turn_id,
        )
        if created and self._hub_events is not None:
            try:
                self._hub_events.append(
                    "notification.created",
                    conversation_id=conversation_id,
                    data={
                        "kind": kind.value,
                        "taskId": task_id,
                        "runId": run_id,
                        "turnId": turn_id,
                        "conversationId": conversation_id,
                        "title": title,
                    },
                )
            except Exception:  # noqa: BLE001 事件失败不阻断通知落库
                logger.debug(
                    "Hub event emit failed for notification",
                    exc_info=True,
                )

    def notify_run(
        self,
        task: TaskRecord,
        run: TaskRun,
        excerpt: Optional[str] = None,
    ) -> None:
        if run.status is TaskRunStatus.COMPLETED:
            kind = (
                NotificationKind.RUN_AWAITING
                if run.awaiting_user
                else NotificationKind.RUN_COMPLETED
            )
        elif run.status is TaskRunStatus.FAILED:
            kind = NotificationKind.RUN_FAILED
        else:
            return
        title = f"《{task.title}》· {_TRIGGER_LABEL[run.trigger]}"
        if kind is NotificationKind.RUN_AWAITING:
            body = _compact(run.awaiting_note) or "Assistant 正在等你处理。"
        elif kind is NotificationKind.RUN_FAILED:
            body = _compact(run.error) or "执行失败。"
        else:
            body = _compact(excerpt) or "本次执行已完成。"
        if run.attempt > 1:
            body = f"{body}（第 {run.attempt} 次尝试）"
        self._record_and_emit(
            kind=kind,
            task_id=task.id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            title=title,
            body=body,
            turn_id=run.turn_id,
        )

    def notify_reminder(
        self,
        reminder: Reminder,
        run: TaskRun,
        excerpt: Optional[str] = None,
        *,
        note: Optional[str] = None,
    ) -> None:
        """提醒 run 的终态通知。

        切片 10 残留 A1：`CANCELLED` **不再**提前 return。任务 run 被取消可以保持
        静默（`notify_run`，任务仍 active、有手动重跑入口），但提醒是一次性的：
        用户取消/打断在飞提醒后，提醒已经是 `fired`、消息永远不来，如果这里也静默，
        用户就处在"既没有通知、也没有痕迹"的状态里——这正是本项要消灭的形状。

        取消复用既有的 `RUN_FAILED` 这个 kind：`notifications.kind` 有 CHECK 约束
        （迁移 021 只允许 `run_completed` / `run_failed` / `run_awaiting`），新增一个
        kind 需要迁移、超出本任务范围；语义精确度由**正文**承担（第一句就是"未执行"）。
        """
        if run.status is TaskRunStatus.COMPLETED:
            kind = (
                NotificationKind.RUN_AWAITING
                if run.awaiting_user
                else NotificationKind.RUN_COMPLETED
            )
        elif run.status is TaskRunStatus.FAILED:
            kind = NotificationKind.RUN_FAILED
        elif run.status is TaskRunStatus.CANCELLED:
            kind = NotificationKind.RUN_FAILED
        else:
            return
        title = f"《提醒·{reminder.title}》"
        if kind is NotificationKind.RUN_AWAITING:
            body = _compact(run.awaiting_note) or "Assistant 正在等你处理。"
        elif run.status is TaskRunStatus.CANCELLED:
            body = REMINDER_CANCELLED_BODY
        elif kind is NotificationKind.RUN_FAILED:
            body = _compact(run.error) or "提醒执行失败。"
        else:
            body = _compact(excerpt) or "提醒已执行完成。"
        if note:
            # 调用方比 run.error 更清楚"这条提醒之后会怎样"（例如重启清扫），如实补一句。
            body = f"{body}（{_compact(note)}）"
        self._record_and_emit(
            kind=kind,
            task_id=reminder.id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            title=title,
            body=body,
            turn_id=run.turn_id,
        )
