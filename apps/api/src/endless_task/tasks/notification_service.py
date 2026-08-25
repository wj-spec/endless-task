"""Task run notifications (R4.7).

Maps finished task runs to inbox notifications. Cancelled runs stay silent
(they are usually user-initiated); everything else informs the user with a
title naming the task and trigger plus a short body.
"""

from __future__ import annotations

import logging
from typing import Optional

from endless_task.domain.models import (
    NotificationKind,
    TaskRecord,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.storage import SqliteNotificationRepository

logger = logging.getLogger(__name__)

_TRIGGER_LABEL = {
    TaskRunTrigger.MANUAL: "手动执行",
    TaskRunTrigger.SCHEDULED: "到点执行",
}

_BODY_CAP = 120


def _compact(text: Optional[str]) -> str:
    if not text:
        return ""
    normalized = " ".join(text.split())
    if len(normalized) > _BODY_CAP:
        return normalized[:_BODY_CAP] + "…"
    return normalized


class TaskNotificationService:
    def __init__(
        self, *, notification_repository: SqliteNotificationRepository
    ) -> None:
        self._repository = notification_repository

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
        self._repository.record(
            kind=kind,
            task_id=task.id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            title=title,
            body=body,
        )
