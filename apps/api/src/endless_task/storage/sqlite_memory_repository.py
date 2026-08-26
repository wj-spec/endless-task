from __future__ import annotations

from typing import Callable, Optional, Sequence

from endless_task.domain.models import MemoryKind, MemoryRecord, MemoryStatus
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now


Clock = Callable[[], str]

CONFIRMED_PROPOSAL_ORIGIN = "confirmed_proposal"


def insert_memory_row(
    connection,
    *,
    memory_id: str,
    kind: MemoryKind,
    content: str,
    source_conversation_id: str,
    source_turn_id: str,
    timestamp: str,
    source_proposal_id: Optional[str] = None,
) -> None:
    connection.execute(
        """
        INSERT INTO memories (
            id, kind, content, status,
            source_conversation_id, source_turn_id, write_origin,
            created_at, updated_at, source_proposal_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            memory_id,
            kind.value,
            content,
            MemoryStatus.ACTIVE.value,
            source_conversation_id,
            source_turn_id,
            CONFIRMED_PROPOSAL_ORIGIN,
            timestamp,
            timestamp,
            source_proposal_id,
        ),
    )


def memory_record_from_row(row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        kind=MemoryKind(row["kind"]),
        content=row["content"],
        status=MemoryStatus(row["status"]),
        source_conversation_id=row["source_conversation_id"],
        source_turn_id=row["source_turn_id"],
        write_origin=row["write_origin"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expired_at=row["expired_at"],
        deleted_at=row["deleted_at"],
        source_proposal_id=row["source_proposal_id"],
        expired_reason=row["expired_reason"],
        superseded_by=row["superseded_by"],
    )


class SqliteMemoryRepository:
    def __init__(
        self,
        database: Database,
        *,
        max_content_chars: int = 1000,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_content_chars <= 0:
            raise ValueError("max_content_chars must be positive")
        self._database = database
        self._max_content_chars = max_content_chars
        self._clock = clock
        self._id_factory = id_factory

    def create_memory(
        self,
        *,
        kind: MemoryKind,
        content: str,
        source_conversation_id: str,
        source_turn_id: str,
    ) -> MemoryRecord:
        normalized_kind = self._validate_kind(kind)
        normalized_content = self._validate_content(content)
        if not source_conversation_id.strip() or not source_turn_id.strip():
            raise ValidationError("Memory source conversation and turn are required.")
        now = self._clock()
        memory_id = self._id_factory("mem")
        with self._database.transaction() as connection:
            insert_memory_row(
                connection,
                memory_id=memory_id,
                kind=normalized_kind,
                content=normalized_content,
                source_conversation_id=source_conversation_id.strip(),
                source_turn_id=source_turn_id.strip(),
                timestamp=now,
            )
        return self.get_memory(memory_id)

    def get_memory(self, memory_id: str) -> MemoryRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Memory not found: {memory_id}")
        return memory_record_from_row(row)

    def list_memories(
        self, *, include_deleted: bool = False
    ) -> Sequence[MemoryRecord]:
        query = "SELECT * FROM memories"
        params: tuple = ()
        if not include_deleted:
            query += " WHERE status = ?"
            params = (MemoryStatus.ACTIVE.value,)
        query += " ORDER BY updated_at, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(memory_record_from_row(row) for row in rows)

    def update_memory_content(self, memory_id: str, content: str) -> MemoryRecord:
        normalized_content = self._validate_content(content)
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            record = memory_record_from_row(row)
            if record.status == MemoryStatus.DELETED:
                raise InvalidStateError("Deleted memories cannot be updated.")
            connection.execute(
                "UPDATE memories SET content = ?, updated_at = ? WHERE id = ?",
                (normalized_content, self._clock(), memory_id),
            )
        return self.get_memory(memory_id)

    def delete_memory(self, memory_id: str) -> MemoryRecord:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            record = memory_record_from_row(row)
            if record.status == MemoryStatus.DELETED:
                raise InvalidStateError("Memory is already deleted.")
            now = self._clock()
            connection.execute(
                """
                UPDATE memories
                SET status = ?, deleted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (MemoryStatus.DELETED.value, now, now, memory_id),
            )
        return self.get_memory(memory_id)

    def find_active_by_content(self, content: str) -> Optional[MemoryRecord]:
        normalized = content.strip()
        if not normalized:
            return None
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memories
                WHERE status = ? AND content = ?
                ORDER BY updated_at, id
                """,
                (MemoryStatus.ACTIVE.value, normalized),
            ).fetchone()
        return memory_record_from_row(row) if row is not None else None

    def expire_memory(
        self,
        memory_id: str,
        *,
        reason: str,
        superseded_by: Optional[str] = None,
    ) -> MemoryRecord:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValidationError("Expire reason must not be empty.")
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            record = memory_record_from_row(row)
            if record.status is not MemoryStatus.ACTIVE:
                raise InvalidStateError("Only active memories can expire.")
            now = self._clock()
            connection.execute(
                """
                UPDATE memories
                SET status = ?, expired_at = ?, expired_reason = ?,
                    superseded_by = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    MemoryStatus.EXPIRED.value,
                    now,
                    normalized_reason,
                    superseded_by,
                    now,
                    memory_id,
                ),
            )
        return self.get_memory(memory_id)

    def _validate_kind(self, kind: MemoryKind) -> MemoryKind:
        if isinstance(kind, MemoryKind):
            return kind
        try:
            return MemoryKind(kind)
        except ValueError as error:
            raise ValidationError(f"Unsupported memory kind: {kind!r}") from error

    def _validate_content(self, content: str) -> str:
        if not isinstance(content, str):
            raise ValidationError("Memory content must be a string.")
        normalized = content.strip()
        if not normalized:
            raise ValidationError("Memory content must not be empty.")
        if len(normalized) > self._max_content_chars:
            raise ValidationError(
                f"Memory content exceeds {self._max_content_chars} characters."
            )
        return normalized
