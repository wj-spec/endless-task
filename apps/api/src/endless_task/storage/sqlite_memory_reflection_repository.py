"""B4 反思的溯源与去重记录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from endless_task.domain.repositories import NotFoundError, ValidationError

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"


@dataclass(frozen=True)
class MemoryReflectionRecord:
    id: str
    conversation_id: str
    trigger: str
    signature: str
    insight_content: str
    proposal_id: str
    status: str
    created_at: str
    run_id: Optional[str] = None
    source_refs: tuple[dict[str, Any], ...] = ()
    insight_memory_id: Optional[str] = None
    resolved_at: Optional[str] = None


def _from_row(row) -> MemoryReflectionRecord:
    try:
        refs = tuple(json.loads(row["source_refs"]))
    except (TypeError, ValueError):
        refs = ()
    return MemoryReflectionRecord(
        id=row["id"],
        conversation_id=row["conversation_id"],
        trigger=row["trigger"],
        signature=row["signature"],
        insight_content=row["insight_content"],
        proposal_id=row["proposal_id"],
        status=row["status"],
        created_at=row["created_at"],
        run_id=row["run_id"],
        source_refs=tuple(
            dict(item) for item in refs if isinstance(item, Mapping)
        ),
        insight_memory_id=row["insight_memory_id"],
        resolved_at=row["resolved_at"],
    )


class SqliteMemoryReflectionRepository:
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

    def create(
        self,
        *,
        conversation_id: str,
        trigger: str,
        signature: str,
        insight_content: str,
        proposal_id: str,
        source_refs: Sequence[Mapping[str, Any]] = (),
        run_id: Optional[str] = None,
    ) -> MemoryReflectionRecord:
        if not insight_content.strip():
            raise ValidationError("Reflection insight must not be empty.")
        record_id = self._id_factory("mref")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_reflections (
                    id, conversation_id, run_id, trigger, signature,
                    insight_content, source_refs, proposal_id, status,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    conversation_id,
                    run_id,
                    trigger,
                    signature,
                    insight_content,
                    json.dumps([dict(item) for item in source_refs], ensure_ascii=False),
                    proposal_id,
                    PENDING,
                    now,
                    now,
                ),
            )
        return self.get(record_id)

    def get(self, reflection_id: str) -> MemoryReflectionRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_reflections WHERE id = ?", (reflection_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Reflection not found: {reflection_id}")
        return _from_row(row)

    def find_by_signature(
        self, signature: str
    ) -> Optional[MemoryReflectionRecord]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_reflections WHERE signature = ?",
                (signature,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def find_by_proposal(
        self, proposal_id: str
    ) -> Optional[MemoryReflectionRecord]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_reflections
                WHERE proposal_id = ?
                ORDER BY created_at DESC, id
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_records(
        self,
        *,
        conversation_id: Optional[str] = None,
        include_resolved: bool = True,
        limit: int = 50,
    ) -> Sequence[MemoryReflectionRecord]:
        if limit <= 0:
            return ()
        query = "SELECT * FROM memory_reflections"
        clauses: list[str] = []
        params: list[Any] = []
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if not include_resolved:
            clauses.append("status = ?")
            params.append(PENDING)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id LIMIT ?"
        params.append(int(limit))
        with self._database.connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(_from_row(row) for row in rows)

    def mark_resolved(
        self,
        reflection_id: str,
        *,
        status: str,
        insight_memory_id: Optional[str] = None,
    ) -> MemoryReflectionRecord:
        if status not in (ACCEPTED, REJECTED):
            raise ValidationError("Only accepted/rejected are terminal.")
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_reflections WHERE id = ?", (reflection_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Reflection not found: {reflection_id}")
            connection.execute(
                """
                UPDATE memory_reflections
                SET status = ?, insight_memory_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, insight_memory_id, now, now, reflection_id),
            )
        return self.get(reflection_id)


__all__ = [
    "ACCEPTED",
    "MemoryReflectionRecord",
    "PENDING",
    "REJECTED",
    "SqliteMemoryReflectionRepository",
]
