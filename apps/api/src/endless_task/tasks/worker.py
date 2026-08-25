"""Task execution worker (R4.3).

A run executes the task's commitment as an automatic turn in the source
conversation, reusing the regular execution path. Results therefore land in
the source conversation like any other turn.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from endless_task.domain.models import (
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
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)

from .run_review_service import RunReview, TaskRunReviewService

logger = logging.getLogger(__name__)

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
        max_concurrent_task_runs: int = 1,
        review_service: Optional[TaskRunReviewService] = None,
    ) -> None:
        if max_concurrent_task_runs <= 0:
            raise ValueError("max_concurrent_task_runs must be positive")
        self._task_repository = task_repository
        self._run_repository = run_repository
        self._controller = controller
        self._semaphore = asyncio.Semaphore(max_concurrent_task_runs)
        self._tasks: set[asyncio.Task[None]] = set()
        self._review_service = review_service

    async def start(
        self, task_id: str, *, trigger: TaskRunTrigger
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
        )
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
            return self._run_repository.finish_run(
                run_id, TaskRunStatus.CANCELLED, error="Task no longer exists."
            )
        if task.status is not TaskStatus.ACTIVE:
            return self._run_repository.finish_run(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Task is not active anymore.",
            )

        message = TRIGGER_PREFIX[run.trigger] + task.commitment
        try:
            handle = await self._controller.submit(
                conversation_id=run.conversation_id,
                client_request_id=f"taskrun:{uuid.uuid4().hex}",
                content=message,
            )
        except NotFoundError:
            logger.warning(
                "Task run %s failed: source conversation is gone", run_id
            )
            return self._run_repository.finish_run(
                run_id,
                TaskRunStatus.FAILED,
                error="来源会话已删除，无法执行该安排。",
            )
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed to submit turn: %s", run_id, error)
            return self._run_repository.finish_run(
                run_id, TaskRunStatus.FAILED, error="Turn submission failed."
            )
        self._run_repository.link_turn(run_id, handle.turn_id)

        try:
            snapshot = await self._controller.wait(handle)
        except Exception as error:  # noqa: BLE001 - journal must survive failures
            logger.warning("Task run %s failed while waiting: %s", run_id, error)
            return self._run_repository.finish_run(
                run_id, TaskRunStatus.FAILED, error="Turn execution failed."
            )

        status = snapshot.turn.status
        if status is TurnStatus.COMPLETED:
            review = await self._review_run(snapshot)
            return self._run_repository.finish_run(
                run_id,
                TaskRunStatus.COMPLETED,
                awaiting_user=review.awaiting_user,
                awaiting_note=review.note,
            )
        if status is TurnStatus.CANCELLED:
            return self._run_repository.finish_run(
                run_id,
                TaskRunStatus.CANCELLED,
                error="Turn was cancelled.",
            )
        return self._run_repository.finish_run(
            run_id,
            TaskRunStatus.FAILED,
            error="Turn did not complete successfully.",
        )

    async def _review_run(self, snapshot: TurnSnapshot) -> RunReview:
        if self._review_service is None:
            return RunReview(False)
        active = next(
            (
                item
                for item in snapshot.response_variants
                if item.variant.id == snapshot.turn.active_response_variant_id
            ),
            None,
        )
        if active is None or not active.assistant_message.content.strip():
            return RunReview(False)
        return await self._review_service.review(
            user_message=snapshot.user_message.content,
            assistant_message=active.assistant_message.content,
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
