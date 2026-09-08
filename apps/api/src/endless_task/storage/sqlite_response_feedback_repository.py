"""A3 反馈机制仓储：用户对某条回答（response variant）的满意度反馈。

本地落库；rating（up/down）+ 可选 reason/note 作为偏好/RLHF 信号，
供用户模型（B5）、记忆反思（B4）与评估（D1）复用。
"""

from __future__ import annotations

import sqlite3
from typing import Callable, List, Optional

from endless_task.domain.models import FeedbackRating, ResponseFeedback

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now


class SqliteResponseFeedbackRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], str] = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    @staticmethod
    def _find_row(connection, turn_id: str, variant_id: Optional[str]):
        if variant_id is None:
            return connection.execute(
                "SELECT * FROM response_feedback "
                "WHERE turn_id = ? AND variant_id IS NULL LIMIT 1",
                (turn_id,),
            ).fetchone()
        return connection.execute(
            "SELECT * FROM response_feedback "
            "WHERE turn_id = ? AND variant_id = ? LIMIT 1",
            (turn_id, variant_id),
        ).fetchone()

    def upsert(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        variant_id: Optional[str],
        rating: FeedbackRating,
        reason: Optional[str] = None,
        note: Optional[str] = None,
    ) -> ResponseFeedback:
        """幂等写入：同一 (turn_id, variant_id) 只保留一条，后写覆盖先写。"""
        now = self._clock()
        with self._database.transaction() as connection:
            row = self._find_row(connection, turn_id, variant_id)
            if row is not None:
                connection.execute(
                    "UPDATE response_feedback "
                    "SET rating = ?, reason = ?, note = ?, updated_at = ? "
                    "WHERE id = ?",
                    (rating.value, reason, note, now, row["id"]),
                )
                feedback_id = row["id"]
            else:
                feedback_id = self._id_factory("rf")
                connection.execute(
                    "INSERT INTO response_feedback ("
                    "id, conversation_id, turn_id, variant_id, "
                    "rating, reason, note, created_at, updated_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        feedback_id,
                        conversation_id,
                        turn_id,
                        variant_id,
                        rating.value,
                        reason,
                        note,
                        now,
                        now,
                    ),
                )
            updated = self._find_row(connection, turn_id, variant_id)
        return self._from_row(updated)

    def get_for_turn(self, turn_id: str) -> Optional[ResponseFeedback]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM response_feedback "
                "WHERE turn_id = ? ORDER BY updated_at DESC LIMIT 1",
                (turn_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_for_conversation(
        self,
        conversation_id: str,
    ) -> List[ResponseFeedback]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM response_feedback "
                "WHERE conversation_id = ? ORDER BY updated_at DESC",
                (conversation_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> ResponseFeedback:
        return ResponseFeedback(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            variant_id=row["variant_id"],
            rating=FeedbackRating(row["rating"]),
            reason=row["reason"],
            note=row["note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
