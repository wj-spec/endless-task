"""SQLite persistence for P4 task runs (R4.3 execution journal)."""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from endless_task.domain.models import (
    TaskRun,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def task_run_from_row(row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        task_id=row["task_id"],
        trigger=TaskRunTrigger(row["trigger"]),
        status=TaskRunStatus(row["status"]),
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


class SqliteTaskRunRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def create_run(
        self,
        *,
        task_id: str,
        trigger: TaskRunTrigger,
        conversation_id: str,
    ) -> TaskRun:
        if not task_id.strip() or not conversation_id.strip():
            raise ValidationError("Task run task and conversation are required.")
        now = self._clock()
        run_id = self._id_factory("taskrun")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_runs (
                    id, task_id, trigger, status, conversation_id, started_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id.strip(),
                    trigger.value,
                    TaskRunStatus.RUNNING.value,
                    conversation_id.strip(),
                    now,
                ),
            )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> TaskRun:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Task run not found: {run_id}")
        return task_run_from_row(row)

    def list_runs(self, *, task_id: str) -> Sequence[TaskRun]:
        query = (
            "SELECT * FROM task_runs WHERE task_id = ? "
            "ORDER BY started_at, id"
        )
        with self._database.connect() as connection:
            rows = connection.execute(query, (task_id,)).fetchall()
        return tuple(task_run_from_row(row) for row in rows)

    def has_running_task(self, task_id: str) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM task_runs WHERE task_id = ? AND status = ? "
                "LIMIT 1",
                (task_id, TaskRunStatus.RUNNING.value),
            ).fetchone()
        return row is not None

    def link_turn(self, run_id: str, turn_id: str) -> TaskRun:
        if not turn_id.strip():
            raise ValidationError("Task run turn id must not be blank.")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task run not found: {run_id}")
            if row["status"] != TaskRunStatus.RUNNING.value:
                raise InvalidStateError("Only running task runs can link a turn.")
            connection.execute(
                "UPDATE task_runs SET turn_id = ? WHERE id = ?",
                (turn_id.strip(), run_id),
            )
        return self.get_run(run_id)

    def finish_run(
        self,
        run_id: str,
        status: TaskRunStatus,
        *,
        error: Optional[str] = None,
    ) -> TaskRun:
        if status is TaskRunStatus.RUNNING:
            raise ValidationError("Task runs cannot be finished as running.")
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task run not found: {run_id}")
            if row["status"] != TaskRunStatus.RUNNING.value:
                raise InvalidStateError("Only running task runs can be finished.")
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (status.value, error, now, run_id),
            )
        return self.get_run(run_id)
