"""Recurring task scheduler (R4.4).

A background loop that starts due tasks through the R4.3 worker. Due-ness is
derived statelessly from the task creation time and the latest scheduled run,
so restarts recover missed periods with at most one catch-up run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

from endless_task.domain.models import (
    TaskRecord,
    TaskRunStatus,
    TaskRunTrigger,
    TaskStatus,
)
from endless_task.domain.repositories import InvalidStateError, NotFoundError
from endless_task.domain.task_schedule import next_occurrence
from endless_task.storage import (
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)

from .worker import TaskWorker

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TaskScheduler:
    def __init__(
        self,
        *,
        task_repository: SqliteTaskRepository,
        run_repository: SqliteTaskRunRepository,
        worker: TaskWorker,
        tick_seconds: float = 30.0,
        max_attempts: int = 3,
        retry_backoff: Tuple[timedelta, ...] = (
            timedelta(seconds=60),
            timedelta(seconds=300),
        ),
        clock: Clock = lambda: datetime.now(timezone.utc),
    ) -> None:
        if tick_seconds <= 0:
            raise ValueError("tick_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not retry_backoff:
            raise ValueError("retry_backoff must not be empty")
        self._task_repository = task_repository
        self._run_repository = run_repository
        self._worker = worker
        self._tick_seconds = tick_seconds
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff
        self._clock = clock

    async def run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive errors
                logger.warning("Task scheduler scan failed", exc_info=True)
            await asyncio.sleep(self._tick_seconds)

    async def scan_once(self, now: Optional[datetime] = None) -> int:
        moment = now or self._clock()
        started = 0
        for task in self._task_repository.list_tasks():
            if task.status is not TaskStatus.ACTIVE:
                continue
            attempt = self._retry_attempt(task, moment)
            if not self.is_due(task, moment) and attempt is None:
                continue
            try:
                await self._worker.start(
                    task.id,
                    trigger=TaskRunTrigger.SCHEDULED,
                    attempt=attempt or 1,
                )
            except (InvalidStateError, NotFoundError):
                continue
            except Exception:  # noqa: BLE001 - one task must not stop the scan
                logger.warning(
                    "Scheduled start failed for task %s", task.id, exc_info=True
                )
                continue
            started += 1
        return started

    def _retry_attempt(
        self, task: TaskRecord, now: datetime
    ) -> Optional[int]:
        scheduled = [
            run
            for run in self._run_repository.list_runs(task_id=task.id)
            if run.trigger is TaskRunTrigger.SCHEDULED
        ]
        if not scheduled:
            return None
        last = max(scheduled, key=lambda run: run.started_at)
        if last.status is not TaskRunStatus.FAILED or not last.retryable:
            return None
        if last.attempt >= self._max_attempts:
            return None
        if last.finished_at is None:
            return None
        backoff = self._retry_backoff[
            min(last.attempt - 1, len(self._retry_backoff) - 1)
        ]
        if parse_timestamp(last.finished_at) + backoff > now:
            return None
        return last.attempt + 1

    def is_due(self, task: TaskRecord, now: datetime) -> bool:
        anchor = parse_timestamp(task.created_at)
        if task.resumed_at is not None:
            resumed = parse_timestamp(task.resumed_at)
            if resumed > anchor:
                anchor = resumed
        for run in self._run_repository.list_runs(task_id=task.id):
            if run.trigger is not TaskRunTrigger.SCHEDULED:
                continue
            candidate = parse_timestamp(run.started_at)
            if candidate > anchor:
                anchor = candidate
        return next_occurrence(task.schedule, anchor) <= now
