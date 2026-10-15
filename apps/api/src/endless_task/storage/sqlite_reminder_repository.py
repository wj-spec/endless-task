"""SQLite persistence for one-off reminders (R4.9)."""

from __future__ import annotations

import sqlite3
from typing import Callable, Optional, Sequence

from endless_task.domain.models import Reminder, ReminderStatus
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]


def insert_reminder_row(
    connection: sqlite3.Connection,
    *,
    reminder_id: str,
    title: str,
    commitment: str,
    due_at: str,
    source_conversation_id: str,
    source_turn_id: str,
    source_proposal_id: Optional[str],
    now: str,
) -> None:
    """`reminders` 表的**唯一**写入器（批次 D R-D4，与 `insert_task_row` 同形）。

    谁建提醒都必须落到这里，否则就会在库里出现第三种行形状：

    - `SqliteReminderRepository.create_reminder`（**显式创建的缝**：HTTP
      `POST /reminders` 走它）；
    - `SqliteTaskProposalRepository.accept_proposal_as_reminder`（**提案接受**的
      缝，此前自己内联了一份**逐字相同**的 INSERT）。

    本函数只负责"一行 `reminders` 怎么写"（列集合、列序、`status` 默认
    `pending`），**不做**校验与时区归一：`create_reminder` 在调用前完成
    title/commitment 归一化与非空校验；`accept_proposal_as_reminder` 复用提案在
    `create_proposal` 时已归一化的值、以及 `reminder_due_to_utc_iso` 算出的
    UTC 时刻。校验一律发生在**任何写入之前**，因此非法输入不会留下半行。

    调用方必须已经处于自己的事务里：`create_reminder` 用它包住单次 INSERT，
    `accept_proposal_as_reminder` 用它把"建提醒 + 标记提案 accepted"保持在同一
    事务中（原子性——不能拆成两个事务，否则崩溃窗口会留下"提醒已建、提案仍
    pending"，用户再点一次接受就会建出第二条提醒）。
    """
    connection.execute(
        """
        INSERT INTO reminders (
            id, title, commitment, due_at, status,
            source_conversation_id, source_turn_id, source_proposal_id,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reminder_id,
            title,
            commitment,
            due_at,
            ReminderStatus.PENDING.value,
            source_conversation_id,
            source_turn_id,
            source_proposal_id,
            now,
            now,
        ),
    )


def reminder_from_row(row) -> Reminder:
    return Reminder(
        id=row["id"],
        title=row["title"],
        commitment=row["commitment"],
        due_at=row["due_at"],
        status=ReminderStatus(row["status"]),
        source_conversation_id=row["source_conversation_id"],
        source_turn_id=row["source_turn_id"],
        source_proposal_id=row["source_proposal_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        fired_at=row["fired_at"],
        cancelled_at=row["cancelled_at"],
    )


class SqliteReminderRepository:
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
            raise ValueError("Reminder limits must be positive")
        self._database = database
        self._max_title_chars = max_title_chars
        self._max_commitment_chars = max_commitment_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_reminder(
        self,
        *,
        title: str,
        commitment: str,
        due_at: str,
        source_conversation_id: str,
        source_turn_id: str,
        source_proposal_id: Optional[str] = None,
    ) -> Reminder:
        normalized_title = self._validate_text(
            title, self._max_title_chars, field="title"
        )
        normalized_commitment = self._validate_text(
            commitment, self._max_commitment_chars, field="commitment"
        )
        if not due_at.strip():
            raise ValidationError("Reminder due time is required.")
        if not source_conversation_id.strip() or not source_turn_id.strip():
            raise ValidationError("Reminder source conversation and turn are required.")
        now = self._clock()
        reminder_id = self._id_factory("rem")
        with self._database.transaction() as connection:
            insert_reminder_row(
                connection,
                reminder_id=reminder_id,
                title=normalized_title,
                commitment=normalized_commitment,
                due_at=due_at.strip(),
                source_conversation_id=source_conversation_id.strip(),
                source_turn_id=source_turn_id.strip(),
                source_proposal_id=(
                    source_proposal_id.strip() if source_proposal_id else None
                ),
                now=now,
            )
        return self.get_reminder(reminder_id)

    def get_reminder(self, reminder_id: str) -> Reminder:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Reminder not found: {reminder_id}")
        return reminder_from_row(row)

    def list_reminders(
        self, *, include_cancelled: bool = False
    ) -> Sequence[Reminder]:
        query = "SELECT * FROM reminders"
        if not include_cancelled:
            query += " WHERE status IN ('pending', 'fired')"
        query += " ORDER BY due_at DESC, id ASC"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(reminder_from_row(row) for row in rows)

    def list_due_pending(self, now_iso: str) -> Sequence[Reminder]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ? "
                "ORDER BY due_at ASC",
                (now_iso,),
            ).fetchall()
        return tuple(reminder_from_row(row) for row in rows)

    def mark_fired(self, reminder_id: str) -> Reminder:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Reminder not found: {reminder_id}")
            if row["status"] != ReminderStatus.PENDING.value:
                raise InvalidStateError("Only pending reminders can fire.")
            connection.execute(
                "UPDATE reminders SET status = ?, fired_at = ?, updated_at = ? "
                "WHERE id = ?",
                (ReminderStatus.FIRED.value, now, now, reminder_id),
            )
        return self.get_reminder(reminder_id)

    def cancel_reminder(self, reminder_id: str) -> Reminder:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Reminder not found: {reminder_id}")
            if row["status"] != ReminderStatus.PENDING.value:
                raise InvalidStateError("Only pending reminders can be cancelled.")
            connection.execute(
                "UPDATE reminders SET status = ?, cancelled_at = ?, updated_at = ? "
                "WHERE id = ?",
                (ReminderStatus.CANCELLED.value, now, now, reminder_id),
            )
        return self.get_reminder(reminder_id)

    def cancel_reminders_for_conversation(self, conversation_id: str) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET status = ?, cancelled_at = ?, updated_at = ? "
                "WHERE source_conversation_id = ? AND status = 'pending'",
                (
                    ReminderStatus.CANCELLED.value,
                    now,
                    now,
                    conversation_id.strip(),
                ),
            )
        return cursor.rowcount or 0

    def _validate_text(self, value: str, limit: int, *, field: str) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"Reminder {field} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValidationError(f"Reminder {field} must not be empty.")
        if len(normalized) > limit:
            raise ValidationError(f"Reminder {field} exceeds {limit} characters.")
        return normalized
