"""SQLite persistence for P4 task proposals (R4.1: create/read/list only)."""

from __future__ import annotations

import json
from typing import Callable, Optional, Sequence, Tuple, Union

from endless_task.domain.models import (
    TaskProposal,
    TaskProposalStatus,
    TaskRecord,
    TaskStatus,
)
from endless_task.domain.repositories import (
    InvalidStateError,
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
from .sqlite_task_repository import task_record_from_row

Clock = Callable[[], str]


def task_proposal_from_row(row) -> TaskProposal:
    return TaskProposal(
        id=row["id"],
        conversation_id=row["conversation_id"],
        turn_id=row["turn_id"],
        title=row["title"],
        commitment=row["commitment"],
        schedule=parse_task_schedule(json.loads(row["schedule"])),
        reason=row["reason"],
        status=TaskProposalStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_task_id=row["resolved_task_id"],
        resolved_at=row["resolved_at"],
    )


class SqliteTaskProposalRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_title_chars: int = 200,
        max_commitment_chars: int = 2000,
        max_reason_chars: int = 500,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if (
            max_title_chars <= 0
            or max_commitment_chars <= 0
            or max_reason_chars <= 0
        ):
            raise ValueError("Task proposal limits must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_commitment_chars = max_commitment_chars
        self._max_reason_chars = max_reason_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        title: str,
        commitment: str,
        schedule: Union[str, dict, TaskSchedule],
        reason: str,
    ) -> TaskProposal:
        normalized_title = self._validate_text(
            title, self._max_title_chars, field="title"
        )
        normalized_commitment = self._validate_text(
            commitment, self._max_commitment_chars, field="commitment"
        )
        normalized_reason = self._validate_text(
            reason, self._max_reason_chars, field="reason"
        )
        parsed_schedule = parse_task_schedule(schedule)
        if not conversation_id.strip() or not turn_id.strip():
            raise ValidationError(
                "Task proposal source conversation and turn are required."
            )
        schedule_json = json.dumps(
            serialize_task_schedule(parsed_schedule),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        now = self._clock()
        proposal_id = self._id_factory("taskp")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_proposals (
                    id, conversation_id, turn_id, title, commitment, schedule,
                    reason, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    conversation_id.strip(),
                    turn_id.strip(),
                    normalized_title,
                    normalized_commitment,
                    schedule_json,
                    normalized_reason,
                    TaskProposalStatus.PENDING.value,
                    now,
                    now,
                ),
            )
        return self.get_proposal(proposal_id)

    def get_proposal(self, proposal_id: str) -> TaskProposal:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Task proposal not found: {proposal_id}")
        return task_proposal_from_row(row)

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[TaskProposal]:
        query = "SELECT * FROM task_proposals WHERE conversation_id = ?"
        params: list = [conversation_id]
        if not include_resolved:
            query += " AND status = ?"
            params.append(TaskProposalStatus.PENDING.value)
        query += " ORDER BY created_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(task_proposal_from_row(row) for row in rows)

    def find_pending_by_commitment(
        self, commitment: str, schedule_json: str, *, conversation_id: str
    ) -> Optional[TaskProposal]:
        normalized = commitment.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_proposals
                WHERE status = ? AND commitment = ? AND schedule = ?
                    AND conversation_id = ?
                ORDER BY created_at, id
                """,
                (
                    TaskProposalStatus.PENDING.value,
                    normalized,
                    schedule_json,
                    conversation_id.strip(),
                ),
            ).fetchone()
        return task_proposal_from_row(row) if row is not None else None

    def list_pending(self, *, limit: int) -> Sequence[TaskProposal]:
        query = (
            "SELECT * FROM task_proposals WHERE status = ? "
            "ORDER BY created_at, id LIMIT ?"
        )
        with self._database.connect() as connection:
            rows = connection.execute(
                query, (TaskProposalStatus.PENDING.value, limit)
            ).fetchall()
        return tuple(task_proposal_from_row(row) for row in rows)

    def cancel_proposal(self, proposal_id: str) -> TaskProposal:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task proposal not found: {proposal_id}")
            proposal = task_proposal_from_row(row)
            if proposal.status is not TaskProposalStatus.PENDING:
                raise InvalidStateError(
                    "Only pending task proposals can be cancelled."
                )
            connection.execute(
                """
                UPDATE task_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskProposalStatus.CANCELLED.value, now, now, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def accept_proposal(
        self, proposal_id: str
    ) -> Tuple[TaskProposal, TaskRecord]:
        now = self._clock()
        task_id = self._id_factory("task")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task proposal not found: {proposal_id}")
            proposal = task_proposal_from_row(row)
            if proposal.status is not TaskProposalStatus.PENDING:
                raise InvalidStateError(
                    "Only pending task proposals can be resolved."
                )
            connection.execute(
                """
                INSERT INTO tasks (
                    id, title, commitment, schedule, status,
                    source_conversation_id, source_turn_id, source_proposal_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    proposal.title,
                    proposal.commitment,
                    row["schedule"],
                    TaskStatus.ACTIVE.value,
                    proposal.conversation_id,
                    proposal.turn_id,
                    proposal.id,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE task_proposals
                SET status = ?, resolved_task_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    TaskProposalStatus.ACCEPTED.value,
                    task_id,
                    now,
                    now,
                    proposal_id,
                ),
            )
        task = self._get_task(task_id)
        return self.get_proposal(proposal_id), task

    def reject_proposal(self, proposal_id: str) -> TaskProposal:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Task proposal not found: {proposal_id}")
            proposal = task_proposal_from_row(row)
            if proposal.status is not TaskProposalStatus.PENDING:
                raise InvalidStateError(
                    "Only pending task proposals can be resolved."
                )
            connection.execute(
                """
                UPDATE task_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (TaskProposalStatus.REJECTED.value, now, now, proposal_id),
            )
        return self.get_proposal(proposal_id)

    def cancel_proposals_for_conversation(self, conversation_id: str) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE task_proposals
                SET status = ?, resolved_at = ?, updated_at = ?
                WHERE conversation_id = ? AND status = ?
                """,
                (
                    TaskProposalStatus.CANCELLED.value,
                    now,
                    now,
                    conversation_id.strip(),
                    TaskProposalStatus.PENDING.value,
                ),
            )
        return cursor.rowcount or 0

    def _get_task(self, task_id: str) -> TaskRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Task not found: {task_id}")
        return task_record_from_row(row)

    def _validate_text(self, value: str, limit: int, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"Task proposal {field} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValidationError(f"Task proposal {field} must not be empty.")
        if len(normalized) > limit:
            raise ValidationError(
                f"Task proposal {field} exceeds {limit} characters."
            )
        return normalized
