"""transcript 条目（用户/助手消息、工具调用与结果引用）。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）：方法体一字未改，
只是换了宿主类。共享的 `self._database` / `self._write` / 行映射由
`V2RepositoryBase` 提供。
"""

from __future__ import annotations

from .base import V2RepositoryBase


from .base import _dump
from collections.abc import Mapping
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime_v2.domain import Actor, LaneKind, LaneStatus, RuntimeV2MessageSubmission, TranscriptEntryRecord, TranscriptEntryStatus, TranscriptEntryType
import hashlib
import sqlite3
from typing import Any, Optional


class TranscriptRepositoryMixin(V2RepositoryBase):
    """见模块 docstring。"""

    def append_entry(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        type: TranscriptEntryType,
        actor: Actor,
        payload: Mapping[str, Any],
        context_policy: Mapping[str, Any],
        display: Optional[Mapping[str, Any]] = None,
        status: TranscriptEntryStatus = TranscriptEntryStatus.FINAL,
        type_version: int = 1,
        parent_id: Optional[str] = None,
        source_run_id: Optional[str] = None,
        allow_variant_sibling: bool = False,
        entry_id: Optional[str] = None,
    ) -> TranscriptEntryRecord:
        entry_id = entry_id or self._id_factory("entry")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> TranscriptEntryRecord:
            nonlocal parent_id
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == LaneKind.ARCHIVED.value or lane["status"] == LaneStatus.ARCHIVED.value:
                raise ConflictError("Archived lanes cannot receive new entries")

            expected_parent = lane["leaf_entry_id"]
            if parent_id is None:
                parent_id = expected_parent
            if parent_id != expected_parent:
                if not allow_variant_sibling or source_run_id is None or parent_id is None:
                    raise ConflictError("Entry parent must match the lane leaf")
                sibling_parent = self._get_entry_row(connection, parent_id)
                if sibling_parent["lane_id"] != lane_id:
                    raise InvalidStateError("Sibling parent does not belong to lane")
                if sibling_parent["source_run_id"] is not None:
                    raise InvalidStateError("Variant sibling parent must be a shared entry")
            if parent_id is not None:
                parent = self._get_entry_row(connection, parent_id)
                if parent["conversation_id"] != conversation_id:
                    raise InvalidStateError("Parent entry does not belong to conversation")
                if parent["status"] == TranscriptEntryStatus.STREAMING.value:
                    raise InvalidStateError("Cannot append after a streaming entry")
            if source_run_id is not None:
                source_run = self._get_run_row(connection, source_run_id)
                if source_run["conversation_id"] != conversation_id:
                    raise InvalidStateError("Source run does not belong to conversation")
                if source_run["lane_id"] != lane_id:
                    raise InvalidStateError("Source run does not belong to lane")

            seq = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
                (lane_id,),
            )
            connection.execute(
                """
                INSERT INTO v2_transcript_entries(
                    id, conversation_id, parent_id, lane_id, seq, type,
                    type_version, actor, status, created_at, updated_at,
                    payload_json, context_policy_json, display_json, source_run_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    conversation_id,
                    parent_id,
                    lane_id,
                    seq,
                    type.value,
                    type_version,
                    actor.value,
                    status.value,
                    now,
                    now,
                    _dump(payload),
                    _dump(context_policy),
                    _dump(display or {}),
                    source_run_id,
                ),
            )
            if parent_id == expected_parent:
                connection.execute(
                    "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                    (entry_id, lane_id),
                )
            return TranscriptEntryRecord(
                id=entry_id,
                conversation_id=conversation_id,
                parent_id=parent_id,
                lane_id=lane_id,
                seq=seq,
                type=type,
                type_version=type_version,
                actor=actor,
                status=status,
                created_at=now,
                updated_at=now,
                payload=dict(payload),
                context_policy=dict(context_policy),
                display=dict(display or {}),
                source_run_id=source_run_id,
            )

        return self._write(operation)

    def create_message_submission(
        self,
        *,
        conversation_id: str,
        lane_id: Optional[str],
        content: str,
        client_request_id: str,
    ) -> RuntimeV2MessageSubmission:
        """Atomically bind one client request to one user entry and one run."""

        request_id = client_request_id.strip()
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RuntimeV2MessageSubmission:
            self._ensure_conversation(connection, conversation_id)
            existing = connection.execute(
                """
                SELECT * FROM v2_message_requests
                WHERE conversation_id = ? AND client_request_id = ?
                """,
                (conversation_id, request_id),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != content_hash:
                    raise ConflictError(
                        "Idempotency key was already used for different message content"
                    )
                if lane_id is not None and existing["lane_id"] != lane_id:
                    raise ConflictError(
                        "Idempotency key was already used for a different lane"
                    )
                existing_lane = self._get_lane_row(connection, existing["lane_id"])
                existing_entry = self._get_entry_row(
                    connection,
                    existing["user_entry_id"],
                )
                existing_run = self._get_run_row(connection, existing["run_id"])
                return RuntimeV2MessageSubmission(
                    lane=self._lane_from_row(existing_lane),
                    user_entry=self._entry_from_row(existing_entry),
                    run=self._run_from_row(existing_run),
                    created=False,
                )

            pointer = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if lane_id is None:
                if pointer is None:
                    selected_lane_id = self._id_factory("lane")
                    connection.execute(
                        """
                        INSERT INTO v2_lanes(
                            id, conversation_id, kind, base_entry_id, leaf_entry_id,
                            created_at, metadata_json, status, archived_at,
                            display_name, summary, source_lane_id, created_from_entry_id
                        )
                        VALUES (?, ?, 'main', NULL, NULL, ?, '{}', 'active',
                                NULL, NULL, NULL, NULL, NULL)
                        """,
                        (selected_lane_id, conversation_id, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO v2_conversation_pointers(
                            conversation_id, active_lane_id, active_run_id,
                            active_run_variant_id, updated_at
                        ) VALUES (?, ?, NULL, NULL, ?)
                        """,
                        (conversation_id, selected_lane_id, now),
                    )
                else:
                    selected_lane_id = str(pointer["active_lane_id"])
            else:
                if pointer is None:
                    raise ConflictError("Conversation has no v2 lane pointer")
                selected_lane_id = lane_id

            lane = self._get_lane_row(connection, selected_lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == LaneKind.ARCHIVED.value or lane["status"] == LaneStatus.ARCHIVED.value:
                raise ConflictError("Messages cannot be sent to an archived lane")

            active_run = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ? AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                )
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active_run is not None:
                raise ConflictError("Conversation already has an active v2 run")

            user_entry_id = self._id_factory("entry")
            entry_seq = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
                (selected_lane_id,),
            )
            connection.execute(
                """
                INSERT INTO v2_transcript_entries(
                    id, conversation_id, parent_id, lane_id, seq, type,
                    type_version, actor, status, created_at, updated_at,
                    payload_json, context_policy_json, display_json, source_run_id
                )
                VALUES (?, ?, ?, ?, ?, 'user_message', 1, 'user', 'final',
                        ?, ?, ?, ?, '{}', NULL)
                """,
                (
                    user_entry_id,
                    conversation_id,
                    lane["leaf_entry_id"],
                    selected_lane_id,
                    entry_seq,
                    now,
                    now,
                    _dump({"content": content}),
                    _dump({"include_in_llm": True, "transform": "full"}),
                ),
            )
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (user_entry_id, selected_lane_id),
            )

            run_id = self._id_factory("run")
            sibling_group_id = self._id_factory("run_group")
            connection.execute(
                """
                INSERT INTO v2_runs(
                    id, conversation_id, lane_id, trigger_entry_id,
                    sibling_group_id, assistant_entry_id, is_active_variant,
                    status, created_at, correlation_id
                )
                VALUES (?, ?, ?, ?, ?, NULL, 1, 'created', ?, ?)
                """,
                (
                    run_id,
                    conversation_id,
                    selected_lane_id,
                    user_entry_id,
                    sibling_group_id,
                    now,
                    request_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO v2_message_requests(
                    conversation_id, client_request_id, content_hash, lane_id,
                    user_entry_id, run_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conversation_id,
                    request_id,
                    content_hash,
                    selected_lane_id,
                    user_entry_id,
                    run_id,
                    now,
                ),
            )
            return RuntimeV2MessageSubmission(
                lane=self._lane_from_row(self._get_lane_row(connection, selected_lane_id)),
                user_entry=self._entry_from_row(
                    self._get_entry_row(connection, user_entry_id)
                ),
                run=self._run_from_row(self._get_run_row(connection, run_id)),
                created=True,
            )

        return self._write(operation)

    def get_entry(self, entry_id: str) -> TranscriptEntryRecord:
        with self._database.connect() as connection:
            row = self._get_entry_row(connection, entry_id)
        return self._entry_from_row(row)

    def list_entries(
        self,
        lane_id: str,
        *,
        after_seq: int = 0,
        include_variants: bool = False,
    ) -> tuple[TranscriptEntryRecord, ...]:
        with self._database.connect() as connection:
            self._get_lane_row(connection, lane_id)
            rows = connection.execute(
                """
                SELECT e.* FROM v2_transcript_entries AS e
                WHERE e.lane_id = ? AND e.seq > ?
                  AND (
                    ? = 1
                    OR e.source_run_id IS NULL
                    OR e.source_run_id IN (
                        SELECT id FROM v2_runs WHERE is_active_variant = 1
                    )
                  )
                ORDER BY e.seq
                """,
                (lane_id, after_seq, 1 if include_variants else 0),
            ).fetchall()
        return tuple(self._entry_from_row(row) for row in rows)

    def has_user_message(self, conversation_id: str) -> bool:
        """该会话是否已经有用户消息（用于"首条消息命名会话"的判断）。"""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM v2_transcript_entries
                WHERE conversation_id = ? AND type = ?
                LIMIT 1
                """,
                (conversation_id, TranscriptEntryType.USER_MESSAGE.value),
            ).fetchone()
        return row is not None

    def list_lane_context_entries(
        self,
        lane_id: str,
        *,
        include_variants: bool = False,
    ) -> tuple[TranscriptEntryRecord, ...]:
        with self._database.connect() as connection:
            lane = self._get_lane_row(connection, lane_id)
            if lane["leaf_entry_id"] is None:
                return ()
            rows = self._entry_context_rows(
                connection,
                lane["leaf_entry_id"],
                include_variants=include_variants,
            )
        return tuple(self._entry_from_row(row) for row in rows)

    def list_entry_context_entries(
        self,
        entry_id: str,
        *,
        include_variants: bool = False,
    ) -> tuple[TranscriptEntryRecord, ...]:
        with self._database.connect() as connection:
            self._get_entry_row(connection, entry_id)
            rows = self._entry_context_rows(
                connection,
                entry_id,
                include_variants=include_variants,
            )
        return tuple(self._entry_from_row(row) for row in rows)
