from __future__ import annotations

from typing import Callable, Optional, Sequence

from endless_task.domain.models import MemoryKind, MemoryRecord, MemoryStatus
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from endless_task.runtime.memory_forgetting import clamp_importance

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now


Clock = Callable[[], str]

CONFIRMED_PROPOSAL_ORIGIN = "confirmed_proposal"
AUTO_FACT_ORIGIN = "auto_fact"


def _shift_seconds(timestamp: str, seconds: int) -> str:
    """在 ISO 时间戳上做整数秒位移（解析失败则原样返回）。"""
    from datetime import datetime, timedelta, timezone

    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(seconds=seconds)).isoformat()


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
    write_origin: str = CONFIRMED_PROPOSAL_ORIGIN,
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
            write_origin,
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
        importance=float(row["importance"]),
        access_count=int(row["access_count"]),
        last_accessed_at=row["last_accessed_at"],
        pinned=bool(row["pinned"]),
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
        self._embedding_hook = None

    # ---------- R5.8 索引钩子 ----------

    def set_embedding_hook(self, hook) -> None:
        """注入索引钩子（submit/remove），写侧增量建向量。"""
        self._embedding_hook = hook

    def _notify_hook(self, action: str, ref_id: str) -> None:
        hook = self._embedding_hook
        if hook is None:
            return
        try:
            if action == "submit":
                hook.submit('memory', ref_id)
            else:
                hook.remove('memory', ref_id)
        except Exception:  # noqa: BLE001 钩子失败不影响写操作
            pass

    def create_memory(
        self,
        *,
        kind: MemoryKind,
        content: str,
        source_conversation_id: str,
        source_turn_id: str,
        write_origin: str = CONFIRMED_PROPOSAL_ORIGIN,
    ) -> MemoryRecord:
        normalized_kind = self._validate_kind(kind)
        normalized_content = self._validate_content(content)
        if not source_conversation_id.strip() or not source_turn_id.strip():
            raise ValidationError("Memory source conversation and turn are required.")
        if write_origin not in (CONFIRMED_PROPOSAL_ORIGIN, AUTO_FACT_ORIGIN):
            raise ValidationError("Memory write origin is not supported.")
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
                write_origin=write_origin,
            )
        self._notify_hook("submit", memory_id)
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

    def list_memories_for_context(self, limit: int) -> Sequence[MemoryRecord]:
        """按"该留"的顺序取记忆：钉住 > 重要 > 常用 > 新近。

        B3：上下文预算有限时，先注入最该记住的那些，而不是最早写入的那些。
        """
        if limit <= 0:
            return ()
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE status = ?
                ORDER BY pinned DESC, importance DESC, access_count DESC,
                         updated_at DESC, id
                LIMIT ?
                """,
                (MemoryStatus.ACTIVE.value, int(limit)),
            ).fetchall()
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
        self._notify_hook("submit", memory_id)
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
        self._notify_hook("remove", memory_id)
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
        self._notify_hook("remove", memory_id)
        return self.get_memory(memory_id)

    # ---------- B3 重要性 / 访问 / 遗忘 ----------

    def record_access(
        self,
        memory_ids: Sequence[str],
        *,
        min_interval_seconds: int = 3600,
    ) -> int:
        """记录记忆被注入/使用：access_count +1、last_accessed_at 更新。

        间隔重复的关键是"被反复用到"而非"每次读取都算"，因此同一记忆在
        ``min_interval_seconds`` 内只计一次（默认 1 小时）。
        """
        unique_ids = tuple(dict.fromkeys(memory_id for memory_id in memory_ids if memory_id))
        if not unique_ids:
            return 0
        now = self._clock()
        cutoff = _shift_seconds(now, -int(min_interval_seconds))
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._database.transaction() as connection:
            cursor = connection.execute(
                f"""
                UPDATE memories
                SET access_count = access_count + 1, last_accessed_at = ?
                WHERE id IN ({placeholders})
                  AND status = ?
                  AND (last_accessed_at IS NULL OR last_accessed_at <= ?)
                """,
                (now, *unique_ids, MemoryStatus.ACTIVE.value, cutoff),
            )
        return int(cursor.rowcount or 0)

    def set_memory_importance(
        self, memory_id: str, importance: float
    ) -> MemoryRecord:
        normalized = clamp_importance(importance)
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            connection.execute(
                "UPDATE memories SET importance = ?, updated_at = ? WHERE id = ?",
                (normalized, self._clock(), memory_id),
            )
        return self.get_memory(memory_id)

    def set_memory_pinned(self, memory_id: str, pinned: bool) -> MemoryRecord:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Memory not found: {memory_id}")
            connection.execute(
                "UPDATE memories SET pinned = ?, updated_at = ? WHERE id = ?",
                (1 if pinned else 0, self._clock(), memory_id),
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
