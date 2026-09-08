"""B2 记忆巩固的溯源与去重记录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from endless_task.domain.repositories import NotFoundError, ValidationError

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now

Clock = Callable[[], str]

PENDING = "pending"
ACCEPTED = "accepted"
REJECTED = "rejected"
_STATUSES = (PENDING, ACCEPTED, REJECTED)


@dataclass(frozen=True)
class MemoryConsolidationRecord:
    id: str
    proposal_id: str
    signature: str
    kind: str
    source_memory_ids: tuple[str, ...]
    status: str
    created_at: str
    updated_at: str
    insight_memory_id: Optional[str] = None
    resolved_at: Optional[str] = None


def _from_row(row) -> MemoryConsolidationRecord:
    try:
        ids = tuple(json.loads(row["source_memory_ids"]))
    except (TypeError, ValueError):
        ids = ()
    return MemoryConsolidationRecord(
        id=row["id"],
        proposal_id=row["proposal_id"],
        signature=row["signature"],
        kind=row["kind"],
        source_memory_ids=tuple(str(item) for item in ids),
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        insight_memory_id=row["insight_memory_id"],
        resolved_at=row["resolved_at"],
    )


class SqliteMemoryConsolidationRepository:
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
        proposal_id: str,
        signature: str,
        kind: str,
        source_memory_ids: Sequence[str],
    ) -> MemoryConsolidationRecord:
        if kind not in ("preference", "fact"):
            raise ValidationError(f"Unsupported memory kind: {kind!r}")
        if not source_memory_ids:
            raise ValidationError("Consolidation needs at least one source memory.")
        now = self._clock()
        record_id = self._id_factory("mcon")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_consolidations (
                    id, proposal_id, signature, kind, source_memory_ids,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    proposal_id,
                    signature,
                    kind,
                    json.dumps(list(source_memory_ids), ensure_ascii=False),
                    PENDING,
                    now,
                    now,
                ),
            )
        return self.get(record_id)

    def get(self, consolidation_id: str) -> MemoryConsolidationRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_consolidations WHERE id = ?",
                (consolidation_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Consolidation not found: {consolidation_id}")
        return _from_row(row)

    def find_by_signature(
        self, signature: str
    ) -> Optional[MemoryConsolidationRecord]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_consolidations WHERE signature = ?",
                (signature,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def find_by_proposal(
        self, proposal_id: str
    ) -> Optional[MemoryConsolidationRecord]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_consolidations
                WHERE proposal_id = ?
                ORDER BY created_at DESC, id
                LIMIT 1
                """,
                (proposal_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_records(
        self, *, include_resolved: bool = True
    ) -> Sequence[MemoryConsolidationRecord]:
        query = "SELECT * FROM memory_consolidations"
        params: tuple = ()
        if not include_resolved:
            query += " WHERE status = ?"
            params = (PENDING,)
        query += " ORDER BY created_at DESC, id"
        with self._database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(_from_row(row) for row in rows)

    def mark_resolved(
        self,
        consolidation_id: str,
        *,
        status: str,
        insight_memory_id: Optional[str] = None,
    ) -> MemoryConsolidationRecord:
        if status not in (ACCEPTED, REJECTED):
            raise ValidationError("Only accepted/rejected are terminal.")
        now = self._clock()
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM memory_consolidations WHERE id = ?",
                (consolidation_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Consolidation not found: {consolidation_id}")
            connection.execute(
                """
                UPDATE memory_consolidations
                SET status = ?, insight_memory_id = ?, resolved_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, insight_memory_id, now, now, consolidation_id),
            )
        return self.get(consolidation_id)


__all__ = [
    "ACCEPTED",
    "MemoryConsolidationRecord",
    "PENDING",
    "REJECTED",
    "SqliteMemoryConsolidationRepository",
]
