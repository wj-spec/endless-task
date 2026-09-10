"""Task execution worker (R4.3).

A run executes the task's commitment as an automatic turn in the source
conversation, reusing the regular execution path. Results therefore land in
the source conversation like any other turn.

The worker drives turns on the v2 runtime: it creates a message submission on
the shared ``SqliteRuntimeV2Repository`` (binding the run to a fresh v2 run)
and executes that run through an ``AgentRunExecutor``.
"""

from __future__ import annotations

from endless_task.domain.models import TaskRecord

import asyncio
import logging
import uuid
from datetime import datetime
from typing import NamedTuple, Optional

from endless_task.domain.models import (
    Conversation,
    ConversationKind,
    Reminder,
    ReminderStatus,
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
    TaskStatus,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    RunExecutionResult,
    RunStatus,
)
from endless_task.storage import (
    SqliteChatRepository,
    SqliteReminderRepository,
    SqliteRuntimeV2Repository,
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

# 兜底墙钟上限：正常超时由执行器的 agent_timeout_seconds 处理，这里是防止
# 任务执行永不返回的极端情况（对应 D11 的紧急 watchdog）。
DEFAULT_TURN_TIMEOUT_SECONDS = 24 * 60 * 60


class TaskTurnOutcome(NamedTuple):
    """Result of driving one task/reminder turn on the v2 runtime."""

    status: TaskRunStatus
    content: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False
    source_gone: bool = False


class TaskWorker:
    """Executes tasks as automatic turns in their source conversation."""

    def __init__(
        self,
        *,
        task_repository: SqliteTaskRepository,
        run_repository: SqliteTaskRunRepository,
        runtime_v2_repository: SqliteRuntimeV2Repository,
        task_executor: AgentRunExecutor,
        chat_repository: Optional[SqliteChatRepository] = None,
        max_concurrent_task_runs: int = 1,
        review_service: Optional[TaskRunReviewService] = None,
        notification_service: Optional[TaskNotificationService] = None,
        reminder_repository: Optional[SqliteReminderRepository] = None,
        turn_timeout_seconds: Optional[float] = None,
    ) -> None:
        if max_concurrent_task_runs <= 0:
            raise ValueError("max_concurrent_task_runs must be positive")
        self._task_repository = task_repository
        self._run_repository = run_repository
        self._runtime_v2_repository = runtime_v2_repository
        self._task_executor = task_executor
        self._chat_repository = chat_repository
        self._semaphore = asyncio.Semaphore(max_concurrent_task_runs)
        self._task_supervisor = BackgroundTaskSupervisor(
            name="task-worker",
            logger=logger,
        )
        self._review_service = review_service
        self._notification_service = notification_service
        self._reminder_repository = reminder_repository
        self._turn_timeout_seconds = turn_timeout_seconds

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
        run = self._run_repository.claim_run(
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
        self._task_supervisor.spawn(
            self.execute(run.id),
            name=f"task-run:{run.id}",
        )
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
        outcome = await self._execute_task_turn(
            run_id=run_id,
            conversation_id=run.conversation_id,
            message=message,
            source_gone_message="来源会话已删除，无法执行该安排。",
        )
        if outcome.status is TaskRunStatus.COMPLETED:
            return await self._finish_completed(
                run_id, task=task, message=message, content=outcome.content
            )
        if outcome.status is TaskRunStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                task=task,
            )
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error=outcome.error or "Turn did not complete successfully.",
            task=task,
            retryable=outcome.retryable,
        )

    async def start_reminder(self, reminder_id: str) -> TaskRun:
        if self._reminder_repository is None:
            raise InvalidStateError("Reminders are not configured.")
        reminder = self._reminder_repository.get_reminder(reminder_id)
        if reminder.status is not ReminderStatus.PENDING:
            raise InvalidStateError("Only pending reminders can be executed.")
        run = self._run_repository.claim_run(
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
        self._task_supervisor.spawn(
            self._execute_reminder(run.id, reminder),
            name=f"reminder-run:{run.id}",
        )
        return run

    async def _execute_reminder(
        self, run_id: str, reminder: Reminder
    ) -> TaskRun:
        message = REMINDER_PREFIX + reminder.commitment
        source_gone_message = "来源会话已删除，无法执行该提醒。"
        outcome = await self._execute_task_turn(
            run_id=run_id,
            conversation_id=reminder.source_conversation_id,
            message=message,
            source_gone_message=source_gone_message,
        )
        if outcome.source_gone:
            # A disappeared source conversation still fires the reminder.
            self._reminder_repository.mark_fired(reminder.id)
        if outcome.status is TaskRunStatus.COMPLETED:
            return await self._finish_completed(
                run_id,
                reminder=reminder,
                message=message,
                content=outcome.content,
            )
        if outcome.status is TaskRunStatus.CANCELLED:
            return self._finish(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
                reminder=reminder,
            )
        return self._finish(
            run_id,
            TaskRunStatus.FAILED,
            error=outcome.error or "Turn did not complete successfully.",
            retryable=outcome.retryable,
            reminder=reminder,
        )

    async def _execute_task_turn(
        self,
        *,
        run_id: str,
        conversation_id: str,
        message: str,
        source_gone_message: str,
    ) -> TaskTurnOutcome:
        """Submit one automatic turn on the v2 runtime and run it.

        Returns a :class:`TaskTurnOutcome`; never raises for expected failure
        modes so the journal always records the run.
        """
        try:
            submission = self._runtime_v2_repository.create_message_submission(
                conversation_id=conversation_id,
                lane_id=None,
                content=message,
                client_request_id=f"taskrun:{run_id}",
            )
        except NotFoundError:
            logger.warning(
                "Task run %s failed: source conversation is gone", run_id
            )
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error=source_gone_message,
                retryable=False,
                source_gone=True,
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed to submit turn: %s", run_id, error)
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error="Turn submission failed.",
                retryable=True,
            )

        # Map the task run to the v2 run id (surfaced as `turnId`).
        self._run_repository.link_turn(run_id, submission.run.id)

        if not submission.created:
            # Idempotent re-submission: the v2 run already exists and was
            # (or is being) executed; only derive its status, never re-run it.
            existing = self._runtime_v2_repository.get_run(submission.run.id)
            return self._map_status(existing.status, None)

        try:
            result = await self._execute_with_guard(submission.run.id)
        except RuntimeCancelled:
            return TaskTurnOutcome(
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed while waiting: %s", run_id, error)
            return TaskTurnOutcome(
                TaskRunStatus.FAILED,
                error="Turn execution failed.",
                retryable=True,
            )
        return self._map_status(result.status, result.content or None)

    async def _execute_with_guard(self, run_id: str) -> RunExecutionResult:
        """Execute a v2 run under a wall-clock guard so tasks never hang."""
        timeout = self._turn_timeout_seconds
        if timeout is None:
            timeout = DEFAULT_TURN_TIMEOUT_SECONDS
        return await asyncio.wait_for(
            self._task_executor.execute(
                run_id,
                cancellation_token=CancellationToken(),
            ),
            timeout=timeout,
        )

    @staticmethod
    def _map_status(status: RunStatus, content: Optional[str]) -> TaskTurnOutcome:
        if status is RunStatus.COMPLETED:
            return TaskTurnOutcome(TaskRunStatus.COMPLETED, content=content)
        if status is RunStatus.CANCELLED:
            return TaskTurnOutcome(
                TaskRunStatus.CANCELLED, error="Turn was cancelled."
            )
        return TaskTurnOutcome(
            TaskRunStatus.FAILED,
            error="Turn did not complete successfully.",
            retryable=True,
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
    def _excerpt(content: Optional[str]) -> Optional[str]:
        if not content or not content.strip():
            return None
        return content

    async def _finish_completed(
        self,
        run_id: str,
        *,
        message: str,
        content: Optional[str],
        task: Optional[TaskRecord] = None,
        reminder: Optional[Reminder] = None,
    ) -> TaskRun:
        review = await self._review_run(message, content)
        return self._finish(
            run_id,
            TaskRunStatus.COMPLETED,
            awaiting_user=review.awaiting_user,
            awaiting_note=review.note,
            task=task,
            reminder=reminder,
            excerpt=self._excerpt(content),
        )

    async def _review_run(
        self, message: str, content: Optional[str]
    ) -> RunReview:
        if self._review_service is None:
            return RunReview(False)
        if content is None:
            return RunReview(False)
        return await self._review_service.review(
            user_message=message,
            assistant_message=content,
        )

    async def drain(self) -> None:
        await self._task_supervisor.drain()

    def running_count(self) -> int:
        return self._task_supervisor.running_count()

    @property
    def _tasks(self) -> tuple[asyncio.Task[object], ...]:
        return self._task_supervisor.tasks
