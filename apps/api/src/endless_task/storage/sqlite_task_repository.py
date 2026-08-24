"""SQLite persistence for P4 tasks (R4.0: create/read/list only)."""

from __future__ import annotations

import json
from typing import Callable, Optional, Sequence, Union

from endless_task.domain.models import TaskRecord, TaskStatus
from endless_task.domain.repositories import (
    NotFoundError,
    ValidationError,
)
from endless_task.domain.task_schedule import (
    TaskSchedule,
    parse_task_schedule,
    serialize_task_schedule,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def task_record_from_row(row) -> TaskRecord:
    return TaskRecord(
        id=row["id"],
        title=row["title"],
        commitment=row["commitment"],
        schedule=parse_task_schedule(json.loads(row["schedule"])),
        status=TaskStatus(row["status"]),
        source_conversation_id=row["source_conversation_id"],
        source_turn_id=row["source_turn_id"],
        source_proposal_id=row["source_proposal_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancelled_at=row["cancelled_at"],
    )


class SqliteTaskRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 200,
        max_commitment_chars: int = 2000,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_title_chars <= 0 or max_commitment_chars <= 0:
            raise ValueError("Task limits must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_commitment_chars = max_commitment_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_task(
        self,
        *,
        title: str,
        commitment: str,
        schedule: Union[str, dict, TaskSchedule],
        source_conversation_id: str,
        source_turn_id: str,
        source_proposal_id: Optional[str] = None,
    ) -> TaskRecord:
        normalized_title = self._validate_text(
            title, self._max_title_chars, field="title"
        )
        normalized_commitment = self._validate_text(
            commitment, self._max_commitment_chars, field="commitment"
        )
        parsed_schedule = parse_task_schedule(schedule)
        if not source_conversation_id.strip() or not source_turn_id.strip():
            raise ValidationError("Task source conversation and turn are required.")
        if source_proposal_id is not None and not source_proposal_id.strip():
            raise ValidationError("Task source proposal id must not be blank.")
        schedule_json = json.dumps(
            serialize_task_schedule(parsed_schedule),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = self._clock()
        task_id = self._id_factory("task")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    id, title, commitment, schedule, status,
                    source_conversation_id, source_turn_id, source_proposal_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    normalized_title,
                    normalized_commitment,
                    schedule_json,
                    TaskStatus.ACTIVE.value,
                    source_conversation_id.strip(),
                    source_turn_id.strip(),
                    source_proposal_id.strip() if source_proposal_id else None,
                    now,
                    now,
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return task_record_from_row(row)

    def list_tasks(self, *, include_cancelled: bool = False) -> Sequence[TaskRecord]:
        query = "SELECT * FROM tasks"
        if not include_cancelled:
            query += " WHERE status IN ('active', 'paused')"
        query += " ORDER BY updated_at DESC, id ASC"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(task_record_from_row(row) for row in rows)

    def cancel_tasks_for_conversation(self, conversation_id: str) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET status = ?, cancelled_at = ?, updated_at = ?
                WHERE source_conversation_id = ?
                    AND status IN ('active', 'paused')
                """,
                (
                    TaskStatus.CANCELLED.value,
                    now,
                    now,
                    conversation_id.strip(),
                ),
            )
        return cursor.rowcount or 0

    def _validate_text(self, value: str, limit: int, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"Task {field} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValidationError(f"Task {field} must not be empty.")
        if len(normalized) > limit:
            raise ValidationError(f"Task {field} exceeds {limit} characters.")
        return normalized
