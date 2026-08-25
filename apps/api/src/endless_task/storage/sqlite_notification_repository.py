"""SQLite persistence for task run notifications (R4.7)."""

from __future__ import annotations

from typing import Callable, Sequence

from endless_task.domain.models import Notification, NotificationKind
from endless_task.domain.repositories import NotFoundError, ValidationError

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

LIST_CAP = 50


def notification_from_row(row) -> Notification:
    return Notification(
        id=row["id"],
        kind=NotificationKind(row["kind"]),
        task_id=row["task_id"],
        run_id=row["run_id"],
        conversation_id=row["conversation_id"],
        title=row["title"],
        body=row["body"],
        created_at=row["created_at"],
        read_at=row["read_at"],
    )


class SqliteNotificationRepository:
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

    def record(
        self,
        *,
        kind: NotificationKind,
        task_id: str,
        run_id: str,
        conversation_id: str,
        title: str,
        body: str,
    ) -> bool:
        if not title.strip() or not body.strip():
            raise ValidationError("Notification title and body are required.")
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    id, kind, task_id, run_id, conversation_id,
                    title, body, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._id_factory("note"),
                    kind.value,
                    task_id,
                    run_id,
                    conversation_id,
                    title.strip(),
                    body.strip(),
                    now,
                ),
            )
        return (cursor.rowcount or 0) > 0

    def list_notifications(
        self, *, unread_only: bool = False
    ) -> Sequence[Notification]:
        query = "SELECT * FROM notifications"
        if unread_only:
            query += " WHERE read_at IS NULL"
        query += f" ORDER BY created_at DESC, id ASC LIMIT {LIST_CAP}"
        with self._database.connect() as connection:
            rows = connection.execute(query).fetchall()
        return tuple(notification_from_row(row) for row in rows)

    def get_notification(self, notification_id: str) -> Notification:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Notification not found: {notification_id}")
        return notification_from_row(row)

    def mark_read(self, notification_id: str) -> Notification:
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM notifications WHERE id = ?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Notification not found: {notification_id}")
            if row["read_at"] is None:
                connection.execute(
                    "UPDATE notifications SET read_at = ? WHERE id = ?",
                    (now, notification_id),
                )
        return self.get_notification(notification_id)

    def mark_all_read(self) -> int:
        now = self._clock()
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE notifications SET read_at = ? WHERE read_at IS NULL",
                (now,),
            )
        return cursor.rowcount or 0
