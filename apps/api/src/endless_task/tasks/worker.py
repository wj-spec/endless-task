"""Task execution worker (R4.3).

A run executes the task's commitment as an automatic turn in the source
conversation, reusing the regular execution path. Results therefore land in
the source conversation like any other turn.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional

from endless_task.domain.models import (
    Conversation,
    ConversationKind,
    Reminder,
    ReminderStatus,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
    TaskStatus,
    TurnSnapshot,
    TurnStatus,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime import TurnController
from endless_task.storage import (
    SqliteChatRepository,
    SqliteReminderRepository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)

from .notification_service import TaskNotificationService
from .run_review_service import RunReview, TaskRunReviewService

logger = logging.getLogger(__name__)

REMINDER_PREFIX = "【提醒】你设置的提醒到时了，请按承诺执行："

TRIGGER_PREFIX = {
    TaskRunTrigger.MANUAL: "【手动执行】请现在执行这项已确认的安排：",
    TaskRunTrigger.SCHEDULED: "【到点执行】安排已到期，请按承诺执行：",
}


class TaskWorker:
    """Executes tasks as automatic turns in their source conversation."""

    def __init__(
        self,
        *,
        task_repository: SqliteTaskRepository,
        run_repository: SqliteTaskRunRepository,
        controller: TurnController,
        chat_repository: Optional[SqliteChatRepository] = None,
        max_concurrent_task_runs: int = 1,
        review_service: Optional[TaskRunReviewService] = None,
        notification_service: Optional[TaskNotificationService] = None,
        reminder_repository: Optional[SqliteReminderRepository] = None,
    ) -> None:
        if max_concurrent_task_runs <= 0:
            raise ValueError("max_concurrent_task_runs must be positive")
        self._task_repository = task_repository
        self._run_repository = run_repository
        self._controller = controller
        self._chat_repository = chat_repository
        self._semaphore = asyncio.Semaphore(max_concurrent_task_runs)
        self._tasks: set[asyncio.Task[None]] = set()
        self._review_service = review_service
        self._notification_service = notification_service
        self._reminder_repository = reminder_repository

    async def start(
        self,
        task_id: str,
        *,
        trigger: TaskRunTrigger,
        attempt: int = 1,
    ) -> TaskRun:
        task = self._task_repository.get_task(task_id)
        if task.status is not TaskStatus.ACTIVE:
            raise InvalidStateError("Only active tasks can be executed.")
        if self._run_repository.has_running_task(task_id):
            raise InvalidStateError("This task is already running.")
        run = self._run_repository.create_run(
            task_id=task_id,
            trigger=trigger,
            conversation_id=task.source_conversation_id,
            attempt=attempt,
        )
        if self._chat_repository is not None:
            try:
                branch = self._ensure_branch(task, run)
            except NotFoundError:
                logger.warning(
                    "Task run %s failed: source conversation is gone", run.id
                )
                return self._finish(
                    run.id,
                    TaskRunStatus.FAILED,
                    error="来源会话已删除，无法执行该安排。",
                    task=task,
                    retryable=False,
                )
            if branch is not None:
                run = self._run_repository.link_conversation(run.id, branch.id)
        spawned = asyncio.create_task(self.execute(run.id))
        self._tasks.add(spawned)
        spawned.add_done_callback(self._tasks.discard)
        return run

    async def execute(self, run_id: str) -> TaskRun:
        async with self._semaphore:
            return await self._execute(run_id)

    async def _execute(self, run_id: str) -> TaskRun:
        run = self._run_repository.get_run(run_id)
        try:
            task = self._task_repository.get_task(run.task_id)
        except Exception:  # noqa: BLE001 - task vanished between start and run
            return self._finish(
                run_id, TaskRunStatus.CANCELLED, error="Task no longer exists."
            )
        if task.status is not TaskStatus.ACTIVE:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Task is not active anymore.",
                task=task,
            )

        message = TRIGGER_PREFIX[run.trigger] + task.commitment
        try:
            handle = await self._controller.submit(
                conversation_id=run.conversation_id,
                client_request_id=f"taskrun:{run.id}",
                content=message,
            )
        except NotFoundError:
            logger.warning(
                "Task run %s failed: source conversation is gone", run_id
            )
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="来源会话已删除，无法执行该安排。",
                task=task,
                retryable=False,
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed to submit turn: %s", run_id, error)
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="Turn submission failed.",
                task=task,
                retryable=True,
            )
        self._run_repository.link_turn(run_id, handle.turn_id)

        try:
            snapshot = await self._controller.wait(handle)
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed while waiting: %s", run_id, error)
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="Turn execution failed.",
                task=task,
                retryable=True,
            )

        status = snapshot.turn.status
        if status is TurnStatus.COMPLETED:
            review = await self._review_run(snapshot)
            return self._finish(
                run_id,
                TaskRunStatus.COMPLETED,
                awaiting_user=review.awaiting_user,
                awaiting_note=review.note,
                task=task,
                excerpt=self._active_text(snapshot),
            )
        if status is TurnStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                task=task,
            )
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error="Turn did not complete successfully.",
            task=task,
            retryable=True,
        )

    async def start_reminder(self, reminder_id: str) -> TaskRun:
        if self._reminder_repository is None:
            raise InvalidStateError("Reminders are not configured.")
        reminder = self._reminder_repository.get_reminder(reminder_id)
        if reminder.status is not ReminderStatus.PENDING:
            raise InvalidStateError("Only pending reminders can be executed.")
        if self._run_repository.has_running_task(reminder.id):
            raise InvalidStateError("This reminder is already running.")
        run = self._run_repository.create_run(
            task_id=reminder.id,
            trigger=TaskRunTrigger.SCHEDULED,
            conversation_id=reminder.source_conversation_id,
        )
        if self._chat_repository is not None:
            try:
                branch = self._chat_repository.create_branch(
                    parent_conversation_id=reminder.source_conversation_id,
                    kind=ConversationKind.EPHEMERAL,
                    title=self._execution_title("提醒", run.started_at),
                )
            except InvalidStateError:
                branch = None
            except NotFoundError:
                logger.warning(
                    "Reminder run %s failed: source conversation is gone", run.id
                )
                self._reminder_repository.mark_fired(reminder.id)
                return self._finish(
                    run.id,
                    TaskRunStatus.FAILED,
                    error="来源会话已删除，无法执行该提醒。",
                    reminder=reminder,
                )
            if branch is not None:
                run = self._run_repository.link_conversation(run.id, branch.id)
        self._reminder_repository.mark_fired(reminder.id)
        spawned = asyncio.create_task(
            self._execute_reminder(run.id, reminder)
        )
        self._tasks.add(spawned)
        spawned.add_done_callback(self._tasks.discard)
        return run

    async def _execute_reminder(
        self, run_id: str, reminder: Reminder
    ) -> TaskRun:
        message = REMINDER_PREFIX + reminder.commitment
        try:
            handle = await self._controller.submit(
                conversation_id=reminder.source_conversation_id,
                client_request_id=f"taskrun:{run_id}",
                content=message,
            )
        except NotFoundError:
            logger.warning(
                "Reminder run %s failed: source conversation is gone", run_id
            )
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="来源会话已删除，无法执行该提醒。",
                reminder=reminder,
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Reminder run %s failed to submit: %s", run_id, error)
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="Turn submission failed.",
                retryable=True,
                reminder=reminder,
            )
        self._run_repository.link_turn(run_id, handle.turn_id)
        try:
            snapshot = await self._controller.wait(handle)
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Reminder run %s failed while waiting: %s", run_id, error)
            return self._finish(
                run_id,
                TaskRunStatus.FAILED,
                error="Turn execution failed.",
                retryable=True,
                reminder=reminder,
            )
        status = snapshot.turn.status
        if status is TurnStatus.COMPLETED:
            review = await self._review_run(snapshot)
            return self._finish(
                run_id,
                TaskRunStatus.COMPLETED,
                awaiting_user=review.awaiting_user,
                awaiting_note=review.note,
                excerpt=self._active_text(snapshot),
                reminder=reminder,
            )
        if status is TurnStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                reminder=reminder,
            )
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error="Turn did not complete successfully.",
            retryable=True,
            reminder=reminder,
        )

    def _ensure_branch(self, task, run: TaskRun) -> Optional[Conversation]:
        assert self._chat_repository is not None
        previous = [
            item
            for item in self._run_repository.list_runs(task_id=task.id)
            if item.id != run.id
        ]
        if previous:
            last = previous[-1]
            if last.conversation_id != task.source_conversation_id:
                try:
                    candidate = self._chat_repository.get_conversation(
                        last.conversation_id
                    )
                except NotFoundError:
                    candidate = None
                if (
                    candidate is not None
                    and candidate.parent_conversation_id
                    == task.source_conversation_id
                ):
                    return candidate
        try:
            return self._chat_repository.create_branch(
                parent_conversation_id=task.source_conversation_id,
                kind=ConversationKind.EPHEMERAL,
                title=self._execution_title(task.title, run.started_at),
            )
        except InvalidStateError:
            return None

    @staticmethod
    def _execution_title(title: str, started_at: str) -> str:
        try:
            moment = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            stamp = moment.strftime("%m-%d %H:%M")
        except ValueError:
            stamp = started_at[:16]
        return f"《{title}》· 执行 @{stamp}"

    def _finish(
        self,
        run_id: str,
        status: TaskRunStatus,
        *,
        error: Optional[str] = None,
        awaiting_user: bool = False,
        awaiting_note: Optional[str] = None,
        retryable: bool = False,
        task: Optional[TaskRecord] = None,
        reminder: Optional[Reminder] = None,
        excerpt: Optional[str] = None,
    ) -> TaskRun:
        run = self._run_repository.finish_run(
            run_id,
            status,
            error=error,
            awaiting_user=awaiting_user,
            awaiting_note=awaiting_note,
            retryable=retryable,
        )
        if self._notification_service is not None:
            try:
                if task is not None:
                    self._notification_service.notify_run(task, run, excerpt)
                elif reminder is not None:
                    self._notification_service.notify_reminder(
                        reminder, run, excerpt
                    )
            except Exception:  # noqa: BLE001 - notifications must not fail runs
                logger.warning(
                    "Task run notification skipped for %s",
                    run_id,
                    exc_info=True,
                )
        return run

    @staticmethod
    def _active_text(snapshot: TurnSnapshot) -> Optional[str]:
        active = next(
            (
                item
                for item in snapshot.response_variants
                if item.variant.id == snapshot.turn.active_response_variant_id
            ),
            None,
        )
        if active is None or not active.assistant_message.content.strip():
            return None
        return active.assistant_message.content

    async def _review_run(self, snapshot: TurnSnapshot) -> RunReview:
        if self._review_service is None:
            return RunReview(False)
        text = self._active_text(snapshot)
        if text is None:
            return RunReview(False)
        return await self._review_service.review(
            user_message=snapshot.user_message.content,
            assistant_message=text,
        )

    async def drain(self) -> None:
        pending = tuple(self._tasks)
        for item in pending:
            try:
                await item
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.warning("Task run task raised during drain", exc_info=True)

    def running_count(self) -> int:
        return len(self._tasks)
