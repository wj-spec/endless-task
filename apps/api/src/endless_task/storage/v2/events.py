"""运行事件、lane 事件、产品事件与上下文压缩记录。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）：方法体一字未改，
只是换了宿主类。共享的 `self._database` / `self._write` / 行映射由
`V2RepositoryBase` 提供。
"""

from __future__ import annotations

from .base import V2RepositoryBase


from .base import _dump
from collections.abc import Mapping
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime_v2.domain import ContextCompactionRecord, LaneEventRecord, ProductRuntimeEventRecord, RuntimeEventRecord
import sqlite3
from typing import Any, Optional, Sequence


class EventRepositoryMixin(V2RepositoryBase):
    """见模块 docstring。"""

    def append_runtime_event(
        self,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        model_turn_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> RuntimeEventRecord:
        def operation(connection: sqlite3.Connection) -> RuntimeEventRecord:
            return self._insert_runtime_event(
                connection,
                run_id=run_id,
                model_turn_id=model_turn_id,
                event_type=event_type,
                payload=payload,
                correlation_id=correlation_id,
                event_id=event_id,
            )

        return self._write(operation)

    def list_runtime_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[RuntimeEventRecord, ...]:
        with self._database.connect() as connection:
            self._get_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_runtime_events
                WHERE run_id = ? AND event_seq > ?
                ORDER BY event_seq
                """,
                (run_id, after_sequence),
            ).fetchall()
        return tuple(self._runtime_event_from_row(row) for row in rows)

    def list_conversation_runtime_events(
        self,
        conversation_id: str,
    ) -> tuple[RuntimeEventRecord, ...]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT e.*
                FROM v2_runtime_events AS e
                JOIN v2_runs AS r ON r.id = e.run_id
                WHERE r.conversation_id = ?
                ORDER BY r.created_at, r.id, e.event_seq
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(self._runtime_event_from_row(row) for row in rows)

    def append_lane_event(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        event_type: str,
        data: Mapping[str, Any],
        event_id: Optional[str] = None,
    ) -> LaneEventRecord:
        event_id = event_id or self._id_factory("lane_event")
        occurred_at = self._clock()

        def operation(connection: sqlite3.Connection) -> LaneEventRecord:
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            return self._insert_lane_event(
                connection,
                event_id=event_id,
                conversation_id=conversation_id,
                lane_id=lane_id,
                event_type=event_type,
                occurred_at=occurred_at,
                data=data,
            )

        return self._write(operation)

    def list_conversation_lane_events(
        self,
        conversation_id: str,
    ) -> tuple[LaneEventRecord, ...]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_lane_events
                WHERE conversation_id = ?
                ORDER BY event_seq
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(self._lane_event_from_row(row) for row in rows)

    def list_product_event_source_ids(
        self,
        conversation_id: str,
    ) -> set[str]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT source_event_id FROM v2_product_events
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchall()
        return {str(row["source_event_id"]) for row in rows}

    def append_product_event(
        self,
        *,
        conversation_id: str,
        event_type: str,
        run_id: Optional[str],
        lane_id: Optional[str],
        source_event_id: str,
        occurred_at: str,
        data: Mapping[str, Any],
        event_id: Optional[str] = None,
    ) -> ProductRuntimeEventRecord:
        event_id = event_id or f"product_{source_event_id}"

        def operation(
            connection: sqlite3.Connection,
        ) -> ProductRuntimeEventRecord:
            self._ensure_conversation(connection, conversation_id)
            existing = connection.execute(
                """
                SELECT * FROM v2_product_events WHERE source_event_id = ?
                """,
                (source_event_id,),
            ).fetchone()
            if existing is not None:
                return self._product_event_from_row(existing)
            event_seq = self._next_seq(
                connection,
                """
                SELECT COALESCE(MAX(event_seq), 0) + 1
                FROM v2_product_events WHERE conversation_id = ?
                """,
                (conversation_id,),
            )
            connection.execute(
                """
                INSERT INTO v2_product_events(
                    id, conversation_id, event_seq, event_type, run_id,
                    lane_id, source_event_id, occurred_at, data_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    conversation_id,
                    event_seq,
                    event_type,
                    run_id,
                    lane_id,
                    source_event_id,
                    occurred_at,
                    _dump(data),
                ),
            )
            return self._product_event_from_row(
                connection.execute(
                    "SELECT * FROM v2_product_events WHERE id = ?",
                    (event_id,),
                ).fetchone()
            )

        return self._write(operation)

    def list_product_events(
        self,
        conversation_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[ProductRuntimeEventRecord, ...]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_product_events
                WHERE conversation_id = ? AND event_seq > ?
                ORDER BY event_seq
                """,
                (conversation_id, after_sequence),
            ).fetchall()
        return tuple(self._product_event_from_row(row) for row in rows)

    def record_context_compaction(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        base_entry_id: str,
        summary_entry_id: str,
        covered_entry_ids: Sequence[str],
        tokens_before: int,
        tokens_after: int,
        compaction_id: Optional[str] = None,
    ) -> ContextCompactionRecord:
        compaction_id = compaction_id or self._id_factory("compaction")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ContextCompactionRecord:
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            for entry_id in (base_entry_id, summary_entry_id, *covered_entry_ids):
                entry = self._get_entry_row(connection, entry_id)
                if entry["conversation_id"] != conversation_id:
                    raise InvalidStateError("Compaction entry does not belong to conversation")

            connection.execute(
                """
                INSERT INTO v2_context_compactions(
                    id, conversation_id, lane_id, base_entry_id,
                    summary_entry_id, covered_entry_ids_json,
                    tokens_before, tokens_after, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compaction_id,
                    conversation_id,
                    lane_id,
                    base_entry_id,
                    summary_entry_id,
                    _dump({"entryIds": list(covered_entry_ids)}),
                    tokens_before,
                    tokens_after,
                    now,
                ),
            )
            return ContextCompactionRecord(
                id=compaction_id,
                conversation_id=conversation_id,
                lane_id=lane_id,
                base_entry_id=base_entry_id,
                summary_entry_id=summary_entry_id,
                covered_entry_ids=tuple(covered_entry_ids),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                created_at=now,
            )

        return self._write(operation)

    def list_context_compactions(
        self,
        lane_id: str,
    ) -> tuple[ContextCompactionRecord, ...]:
        with self._database.connect() as connection:
            self._get_lane_row(connection, lane_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_context_compactions
                WHERE lane_id = ?
                ORDER BY created_at, id
                """,
                (lane_id,),
            ).fetchall()
            return tuple(self._context_compaction_from_row(row) for row in rows)
