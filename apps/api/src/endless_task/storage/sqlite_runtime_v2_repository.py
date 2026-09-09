from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from endless_task.domain.models import ConversationKind
from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError
from endless_task.tooling import JsonValue, ToolCallError

from endless_task.runtime_v2.domain import (
    Actor,
    ContextCompactionRecord,
    ConversationPointer,
    LaneKind,
    LaneStatus,
    LaneEventRecord,
    LanePromotionRecord,
    LaneRecord,
    ModelTurnRecord,
    ModelTurnStatus,
    RunRecord,
    RunStatus,
    RuntimeEventRecord,
    ProductRuntimeEventRecord,
    RuntimeV2MessageSubmission,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
    TranscriptEntryStatus,
    TranscriptEntryType,
    TemporaryConversationRecord,
)

from .database import Database


ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.CREATED,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPACTING,
        RunStatus.CANCELLING,
    }
)

_ACTIVE_MODEL_TURN_STATUSES = frozenset(
    {
        ModelTurnStatus.CREATED,
        ModelTurnStatus.PROJECTING_CONTEXT,
        ModelTurnStatus.WAITING_PROVIDER_SLOT,
        ModelTurnStatus.STREAMING,
        ModelTurnStatus.EXECUTING_TOOLS,
    }
)

_ACTIVE_TOOL_EXECUTION_STATUSES = frozenset(
    {
        ToolExecutionStatus.CREATED,
        ToolExecutionStatus.VALIDATING,
        ToolExecutionStatus.WAITING_APPROVAL,
        ToolExecutionStatus.RUNNING,
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _dump(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    """把 mappingproxy / tuple / 嵌套映射递归转成可 JSON 序列化的普通结构。"""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _load(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ConflictError("Persisted JSON payload must be an object")
    return parsed


class SqliteRuntimeV2Repository:

    #: per-thread write-reentrancy guard (06 §30.5).
    _write_depth = threading.local()
    """SQLite persistence for the v2 Agent Runtime state model.

    The repository intentionally does not migrate v1 rows. M1 only provides the
    active v2 write path; the v1 tables remain untouched and readable.
    """

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], str] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def create_lane(
        self,
        *,
        conversation_id: str,
        kind: LaneKind = LaneKind.MAIN,
        base_entry_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
        status: LaneStatus = LaneStatus.ACTIVE,
        archived_at: Optional[str] = None,
        display_name: Optional[str] = None,
        summary: Optional[str] = None,
        source_lane_id: Optional[str] = None,
        created_from_entry_id: Optional[str] = None,
    ) -> LaneRecord:
        lane_id = lane_id or self._id_factory("lane")
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None
        persisted_kind = kind
        if kind is LaneKind.ARCHIVED:
            persisted_kind = LaneKind.PERSISTENT_BRANCH
            status = LaneStatus.ARCHIVED
            archived_at = archived_at or now
        metadata_dict = dict(metadata or {})
        if source_lane_id is None:
            raw_source_lane_id = metadata_dict.get("sourceLaneId")
            source_lane_id = raw_source_lane_id if isinstance(raw_source_lane_id, str) else None
        created_from_entry_id = created_from_entry_id or base_entry_id
        if persisted_kind is LaneKind.MAIN and status is LaneStatus.ARCHIVED:
            raise InvalidStateError("The main lane cannot be archived")

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            self._ensure_conversation(connection, conversation_id)
            if base_entry_id is not None:
                base = self._get_entry_row(connection, base_entry_id)
                if base["conversation_id"] != conversation_id:
                    raise InvalidStateError("Base entry does not belong to conversation")
            if source_lane_id is not None:
                self._get_lane_row(connection, source_lane_id)
            if created_from_entry_id is not None:
                self._get_entry_row(connection, created_from_entry_id)
            existing = connection.execute(
                "SELECT 1 FROM v2_lanes WHERE conversation_id = ? AND kind = 'main'",
                (conversation_id,),
            ).fetchone()
            if persisted_kind is LaneKind.MAIN and existing is not None:
                raise ConflictError("Conversation already has a main lane")

            connection.execute(
                """
                INSERT INTO v2_lanes(
                    id, conversation_id, kind, base_entry_id, leaf_entry_id,
                    created_at, metadata_json, status, archived_at,
                    display_name, summary, source_lane_id, created_from_entry_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lane_id,
                    conversation_id,
                    persisted_kind.value,
                    base_entry_id,
                    base_entry_id,
                    now,
                    _dump(metadata_dict),
                    status.value,
                    archived_at,
                    display_name,
                    summary,
                    source_lane_id,
                    created_from_entry_id,
                ),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=conversation_id,
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def get_lane(self, lane_id: str) -> LaneRecord:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM v2_lanes WHERE id = ?", (lane_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Lane not found: {lane_id}")
        return self._lane_from_row(row)

    def list_lanes(
        self,
        conversation_id: str,
        *,
        include_archived: bool = False,
    ) -> tuple[LaneRecord, ...]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            archived_filter = "" if include_archived else "AND kind != 'archived' AND status != 'archived'"
            rows = connection.execute(
                f"""
                SELECT * FROM v2_lanes
                WHERE conversation_id = ? {archived_filter}
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(self._lane_from_row(row) for row in rows)

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

    def get_model_turn(self, turn_id: str) -> ModelTurnRecord:
        with self._database.connect() as connection:
            row = self._get_model_turn_row(connection, turn_id)
        return self._model_turn_from_row(row)

    def list_model_turns(self, run_id: str) -> tuple[ModelTurnRecord, ...]:
        with self._database.connect() as connection:
            self._get_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_model_turns
                WHERE run_id = ?
                ORDER BY turn_index, id
                """,
                (run_id,),
            ).fetchall()
        return tuple(self._model_turn_from_row(row) for row in rows)

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

    def _entry_context_rows(
        self,
        connection: sqlite3.Connection,
        entry_id: str,
        *,
        include_variants: bool,
    ) -> tuple[sqlite3.Row, ...]:
        rows = connection.execute(
            """
            WITH RECURSIVE chain(entry_id, parent_id, depth) AS (
                SELECT id, parent_id, 0
                FROM v2_transcript_entries
                WHERE id = ?
                UNION ALL
                SELECT e.id, e.parent_id, c.depth + 1
                FROM v2_transcript_entries AS e
                JOIN chain AS c ON e.id = c.parent_id
            )
            SELECT e.*
            FROM v2_transcript_entries AS e
            JOIN chain AS c ON c.entry_id = e.id
            WHERE (
                ? = 1
                OR e.source_run_id IS NULL
                OR e.source_run_id IN (
                    SELECT id FROM v2_runs WHERE is_active_variant = 1
                )
            )
            ORDER BY c.depth DESC
            """,
            (entry_id, 1 if include_variants else 0),
        ).fetchall()
        return tuple(rows)

    def set_conversation_pointer(
        self,
        *,
        conversation_id: str,
        active_lane_id: str,
        active_run_id: Optional[str] = None,
        active_run_variant_id: Optional[str] = None,
    ) -> ConversationPointer:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ConversationPointer:
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, active_lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == LaneKind.ARCHIVED.value or lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot become active")
            if active_run_id is not None:
                run = self._get_run_row(connection, active_run_id)
                if run["conversation_id"] != conversation_id:
                    raise InvalidStateError("Run does not belong to conversation")
                if run["lane_id"] != active_lane_id:
                    raise InvalidStateError("Run does not belong to active lane")
            if active_run_variant_id is not None:
                run = self._get_run_row(connection, active_run_variant_id)
                if run["conversation_id"] != conversation_id:
                    raise InvalidStateError("Run variant does not belong to conversation")
                if run["lane_id"] != active_lane_id:
                    raise InvalidStateError("Run variant does not belong to active lane")

            connection.execute(
                """
                INSERT INTO v2_conversation_pointers(
                    conversation_id, active_lane_id, active_run_id,
                    active_run_variant_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    active_lane_id = excluded.active_lane_id,
                    active_run_id = excluded.active_run_id,
                    active_run_variant_id = excluded.active_run_variant_id,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    active_lane_id,
                    active_run_id,
                    active_run_variant_id,
                    now,
                ),
            )
            return ConversationPointer(
                conversation_id=conversation_id,
                active_lane_id=active_lane_id,
                active_run_id=active_run_id,
                active_run_variant_id=active_run_variant_id,
                updated_at=now,
            )

        return self._write(operation)

    def update_lane_kind(self, lane_id: str, kind: LaneKind) -> LaneRecord:
        if kind is LaneKind.ARCHIVED:
            return self.archive_lane_subtree(lane_id)[0]

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            lane = self._get_lane_row(connection, lane_id)
            current_kind = LaneKind(lane["kind"])
            if current_kind is LaneKind.ARCHIVED:
                current_kind = LaneKind.PERSISTENT_BRANCH
            if current_kind is kind:
                return self._lane_from_row(lane)
            if current_kind is LaneKind.MAIN:
                raise InvalidStateError("Use lane promotion to replace the main lane")
            if kind is LaneKind.MAIN:
                raise InvalidStateError("Use lane promotion to make a lane main")
            if lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot change kind")
            connection.execute(
                "UPDATE v2_lanes SET kind = ? WHERE id = ?",
                (kind.value, lane_id),
            )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def rename_lane(
        self,
        lane_id: str,
        display_name: Optional[str],
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> LaneRecord:
        normalized = " ".join((display_name or "").split()) or None
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> LaneRecord:
            lane = self._get_lane_row(connection, lane_id)
            previous_display_name = lane["display_name"]
            if previous_display_name == normalized:
                return self._lane_from_row(lane)
            connection.execute(
                "UPDATE v2_lanes SET display_name = ? WHERE id = ?",
                (normalized, lane_id),
            )
            if lane_event_type is not None and lane_event_id is not None:
                event_data = dict(lane_event_data or {})
                event_data.update(
                    {
                        "laneId": lane_id,
                        "displayName": normalized,
                        "previousDisplayName": previous_display_name,
                    }
                )
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=event_data,
                )
            return self._lane_from_row(self._get_lane_row(connection, lane_id))

        return self._write(operation)

    def archive_lane_subtree(
        self,
        lane_id: str,
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> tuple[LaneRecord, ...]:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> tuple[LaneRecord, ...]:
            lane = self._get_lane_row(connection, lane_id)
            if lane["kind"] == LaneKind.TEMPORARY.value:
                raise InvalidStateError("Temporary conversation lanes cannot be archived")
            pointer = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (lane["conversation_id"],),
            ).fetchone()
            is_main = (
                pointer["active_lane_id"] == lane_id
                if pointer is not None
                else lane["kind"] == LaneKind.MAIN.value
            )
            if is_main:
                raise InvalidStateError("The main lane cannot be archived")
            subtree = self._lane_subtree_rows(
                connection,
                conversation_id=lane["conversation_id"],
                lane_id=lane_id,
                excluded_lane_id=pointer["active_lane_id"] if pointer is not None else None,
            )
            lane_ids = tuple(row["id"] for row in subtree)
            active = connection.execute(
                f"""
                SELECT 1 FROM v2_runs
                WHERE lane_id IN ({','.join('?' for _ in lane_ids)})
                  AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                  )
                LIMIT 1
                """,
                lane_ids,
            ).fetchone()
            if active is not None:
                raise ConflictError("A lane with an active run cannot be archived")
            connection.execute(
                f"""
                UPDATE v2_lanes
                SET status = 'archived', archived_at = COALESCE(archived_at, ?),
                    kind = CASE WHEN kind = 'archived' THEN 'persistent_branch' ELSE kind END
                WHERE id IN ({','.join('?' for _ in lane_ids)})
                """,
                (now, *lane_ids),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return tuple(self._lane_from_row(self._get_lane_row(connection, item)) for item in lane_ids)

        return self._write(operation)

    def restore_lane_subtree(
        self,
        lane_id: str,
        *,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> tuple[LaneRecord, ...]:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> tuple[LaneRecord, ...]:
            lane = self._get_lane_row(connection, lane_id)
            pointer = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (lane["conversation_id"],),
            ).fetchone()
            subtree = self._lane_subtree_rows(
                connection,
                conversation_id=lane["conversation_id"],
                lane_id=lane_id,
                excluded_lane_id=pointer["active_lane_id"] if pointer is not None else None,
            )
            lane_ids = tuple(row["id"] for row in subtree)
            connection.execute(
                f"""
                UPDATE v2_lanes
                SET status = 'active', archived_at = NULL,
                    kind = CASE WHEN kind = 'archived' THEN 'persistent_branch' ELSE kind END
                WHERE id IN ({','.join('?' for _ in lane_ids)})
                """,
                lane_ids,
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=lane["conversation_id"],
                    lane_id=lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            return tuple(self._lane_from_row(self._get_lane_row(connection, item)) for item in lane_ids)

        return self._write(operation)

    def promote_lane(
        self,
        *,
        conversation_id: str,
        target_lane_id: str,
        lane_event_type: Optional[str] = None,
        lane_event_data: Optional[Mapping[str, Any]] = None,
    ) -> LanePromotionRecord:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event") if lane_event_type else None

        def operation(connection: sqlite3.Connection) -> LanePromotionRecord:
            self._ensure_conversation(connection, conversation_id)
            pointer_row = connection.execute(
                """
                SELECT * FROM v2_conversation_pointers
                WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if pointer_row is None:
                raise ConflictError("Conversation has no v2 lane pointer")
            previous_main = self._get_lane_row(
                connection,
                pointer_row["active_lane_id"],
            )
            target = self._get_lane_row(connection, target_lane_id)
            if previous_main["conversation_id"] != conversation_id:
                raise InvalidStateError("Active lane does not belong to conversation")
            if target["conversation_id"] != conversation_id:
                raise InvalidStateError("Target lane does not belong to conversation")
            if target_lane_id == pointer_row["active_lane_id"]:
                raise ConflictError("Target lane is already active")
            if target["kind"] == LaneKind.ARCHIVED.value or target["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot be promoted")

            connection.execute(
                """
                UPDATE v2_lanes
                SET kind = CASE
                    WHEN id = ? THEN 'main'
                    WHEN kind = 'main' THEN 'persistent_branch'
                    ELSE kind
                END
                WHERE conversation_id = ?
                """,
                (target_lane_id, conversation_id),
            )
            connection.execute(
                """
                UPDATE v2_conversation_pointers
                SET active_lane_id = ?, active_run_id = NULL,
                    active_run_variant_id = NULL, updated_at = ?
                WHERE conversation_id = ?
                """,
                (target_lane_id, now, conversation_id),
            )
            if lane_event_type is not None and lane_event_id is not None:
                self._insert_lane_event(
                    connection,
                    event_id=lane_event_id,
                    conversation_id=conversation_id,
                    lane_id=target_lane_id,
                    event_type=lane_event_type,
                    occurred_at=now,
                    data=lane_event_data or {},
                )
            promoted = self._lane_from_row(self._get_lane_row(connection, target_lane_id))
            previous = self._lane_from_row(self._get_lane_row(connection, previous_main["id"]))
            pointer = ConversationPointer(
                conversation_id=conversation_id,
                active_lane_id=target_lane_id,
                active_run_id=None,
                active_run_variant_id=None,
                updated_at=now,
            )
            return LanePromotionRecord(
                promoted_lane=promoted,
                previous_main_lane=previous,
                pointer=pointer,
            )

        return self._write(operation)

    def create_temporary_conversation_from_lane(
        self,
        *,
        source_conversation_id: str,
        source_lane_id: str,
        source_leaf_entry_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        lane_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> tuple[str, LaneRecord, TemporaryConversationRecord]:
        temporary_conversation_id = conversation_id or self._id_factory("conv")
        temporary_lane_id = lane_id or self._id_factory("lane")
        now = self._clock()

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[str, LaneRecord, TemporaryConversationRecord]:
            source_conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (source_conversation_id,),
            ).fetchone()
            if source_conversation is None:
                raise NotFoundError(f"Conversation not found: {source_conversation_id}")
            if source_conversation["kind"] == ConversationKind.EPHEMERAL.value:
                raise InvalidStateError("Temporary conversations cannot be forked")
            active = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ? AND status IN (
                    'created', 'queued', 'running', 'waiting_approval',
                    'compacting', 'cancelling'
                )
                LIMIT 1
                """,
                (source_conversation_id,),
            ).fetchone()
            if active is not None:
                raise ConflictError("Conversation has an active run")

            source_lane = self._get_lane_row(connection, source_lane_id)
            if source_lane["conversation_id"] != source_conversation_id:
                raise InvalidStateError("Source lane does not belong to conversation")
            if source_lane["kind"] == LaneKind.ARCHIVED.value or source_lane["status"] == LaneStatus.ARCHIVED.value:
                raise InvalidStateError("Archived lanes cannot create temporary conversations")
            leaf_entry_id = source_leaf_entry_id or source_lane["leaf_entry_id"]
            if leaf_entry_id is None or source_lane["leaf_entry_id"] is None:
                raise ConflictError("Source lane has no visible entries")
            visible_source_entries = self._entry_context_rows(
                connection,
                source_lane["leaf_entry_id"],
                include_variants=False,
            )
            visible_source_entry_ids = {row["id"] for row in visible_source_entries}
            if leaf_entry_id not in visible_source_entry_ids:
                raise InvalidStateError("Source entry is not on the source lane path")
            source_entries = self._entry_context_rows(
                connection,
                leaf_entry_id,
                include_variants=False,
            )
            source_entry_ids = tuple(row["id"] for row in source_entries)
            source_title = " ".join((title or "").split()) or "临时对话"
            connection.execute(
                """
                INSERT INTO conversations(
                    id, title, status, next_turn_ordinal, title_is_manual,
                    created_at, updated_at, archived_at,
                    parent_conversation_id, fork_turn_id, kind, promoted_at,
                    workspace_id, provider_profile_id, model_override
                ) VALUES (?, ?, 'active', 1, ?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?)
                """,
                (
                    temporary_conversation_id,
                    source_title,
                    1 if title and title.strip() else 0,
                    now,
                    now,
                    ConversationKind.EPHEMERAL.value,
                    source_conversation["workspace_id"],
                    source_conversation["provider_profile_id"],
                    source_conversation["model_override"],
                ),
            )
            connection.execute(
                """
                INSERT INTO v2_lanes(
                    id, conversation_id, kind, base_entry_id, leaf_entry_id,
                    created_at, metadata_json, status, archived_at,
                    display_name, summary, source_lane_id, created_from_entry_id
                )
                VALUES (?, ?, 'temporary', NULL, NULL, ?, ?, 'active', NULL, NULL, ?, ?, ?)
                """,
                (
                    temporary_lane_id,
                    temporary_conversation_id,
                    now,
                    _dump(
                        {
                            "sourceConversationId": source_conversation_id,
                            "sourceLaneId": source_lane_id,
                            "sourceLeafEntryId": leaf_entry_id,
                        }
                    ),
                    self._entry_excerpt(source_entries[-1]) if source_entries else None,
                    source_lane_id,
                    leaf_entry_id,
                ),
            )

            copied_ids: dict[str, str] = {}
            copied_entry_ids: list[str] = []
            for seq, source_entry in enumerate(source_entries, start=1):
                copied_entry_id = self._id_factory("entry")
                copied_ids[source_entry["id"]] = copied_entry_id
                copied_entry_ids.append(copied_entry_id)
                source_parent_id = source_entry["parent_id"]
                parent_id = copied_ids.get(source_parent_id) if source_parent_id else None
                display = _load(source_entry["display_json"])
                display.setdefault("sourceEntryId", source_entry["id"])
                connection.execute(
                    """
                    INSERT INTO v2_transcript_entries(
                        id, conversation_id, parent_id, lane_id, seq, type,
                        type_version, actor, status, created_at, updated_at,
                        payload_json, context_policy_json, display_json, source_run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        copied_entry_id,
                        temporary_conversation_id,
                        parent_id,
                        temporary_lane_id,
                        seq,
                        source_entry["type"],
                        source_entry["type_version"],
                        source_entry["actor"],
                        source_entry["status"],
                        source_entry["created_at"],
                        source_entry["updated_at"],
                        source_entry["payload_json"],
                        source_entry["context_policy_json"],
                        _dump(display),
                    ),
                )

            copied_leaf_entry_id = copied_ids[leaf_entry_id]
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (copied_leaf_entry_id, temporary_lane_id),
            )
            connection.execute(
                """
                INSERT INTO v2_conversation_pointers(
                    conversation_id, active_lane_id, active_run_id,
                    active_run_variant_id, updated_at
                ) VALUES (?, ?, NULL, NULL, ?)
                """,
                (temporary_conversation_id, temporary_lane_id, now),
            )
            connection.execute(
                """
                INSERT INTO v2_temporary_conversations(
                    conversation_id, source_conversation_id, source_lane_id,
                    source_base_entry_id, source_leaf_entry_id,
                    snapshot_entry_ids_json, created_at, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    temporary_conversation_id,
                    source_conversation_id,
                    source_lane_id,
                    source_lane["base_entry_id"],
                    leaf_entry_id,
                    _dump(
                        {
                            "entryIds": list(source_entry_ids),
                            "copiedEntryIds": copied_entry_ids,
                        }
                    ),
                    now,
                ),
            )
            self._insert_lane_event(
                connection,
                event_id=self._id_factory("lane_event"),
                conversation_id=temporary_conversation_id,
                lane_id=temporary_lane_id,
                event_type="temporary_conversation.created",
                occurred_at=now,
                data={
                    "sourceConversationId": source_conversation_id,
                    "sourceLaneId": source_lane_id,
                    "sourceLeafEntryId": leaf_entry_id,
                },
            )
            return (
                temporary_conversation_id,
                self._lane_from_row(self._get_lane_row(connection, temporary_lane_id)),
                self._temporary_conversation_from_row(
                    self._get_temporary_conversation_row(
                        connection,
                        temporary_conversation_id,
                    )
                ),
            )

        return self._write(operation)

    def get_temporary_conversation(
        self,
        conversation_id: str,
    ) -> TemporaryConversationRecord:
        with self._database.connect() as connection:
            row = self._get_temporary_conversation_row(connection, conversation_id)
        return self._temporary_conversation_from_row(row)

    def promote_temporary_conversation(
        self,
        conversation_id: str,
    ) -> TemporaryConversationRecord:
        now = self._clock()
        lane_event_id = self._id_factory("lane_event")

        def operation(connection: sqlite3.Connection) -> TemporaryConversationRecord:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError(f"Conversation not found: {conversation_id}")
            if conversation["kind"] != ConversationKind.EPHEMERAL.value:
                raise ConflictError("Only temporary conversations can be promoted")
            temporary_record = self._get_temporary_conversation_row(
                connection,
                conversation_id,
            )
            active = connection.execute(
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
            if active is not None:
                raise ConflictError("Conversation has an active run")
            root = connection.execute(
                """
                SELECT * FROM v2_lanes
                WHERE conversation_id = ? AND kind = 'temporary'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if root is None:
                raise InvalidStateError("Temporary conversation root lane is missing")
            connection.execute(
                """
                UPDATE conversations
                SET kind = 'normal', promoted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, conversation_id),
            )
            connection.execute(
                "UPDATE v2_lanes SET kind = 'main' WHERE id = ?",
                (root["id"],),
            )
            connection.execute(
                """
                UPDATE v2_temporary_conversations
                SET promoted_at = COALESCE(promoted_at, ?)
                WHERE conversation_id = ?
                """,
                (now, conversation_id),
            )
            self._insert_lane_event(
                connection,
                event_id=lane_event_id,
                conversation_id=conversation_id,
                lane_id=root["id"],
                event_type="temporary_conversation.promoted",
                occurred_at=now,
                data={
                    "temporaryConversationId": conversation_id,
                    "sourceConversationId": temporary_record["source_conversation_id"],
                    "sourceLaneId": temporary_record["source_lane_id"],
                    "sourceLeafEntryId": temporary_record["source_leaf_entry_id"],
                },
            )
            return self._temporary_conversation_from_row(
                self._get_temporary_conversation_row(connection, conversation_id)
            )

        return self._write(operation)

    def delete_temporary_conversation(self, conversation_id: str) -> None:
        lane_event_id = self._id_factory("lane_event")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> None:
            conversation = connection.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError(f"Conversation not found: {conversation_id}")
            temporary_record = self._get_temporary_conversation_row(
                connection,
                conversation_id,
            )
            if conversation["kind"] != ConversationKind.EPHEMERAL.value:
                raise ConflictError("Only unpromoted temporary conversations can be deleted")
            active = connection.execute(
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
            if active is not None:
                raise ConflictError("Conversation has an active run")
            root = connection.execute(
                """
                SELECT id FROM v2_lanes
                WHERE conversation_id = ? AND kind = 'temporary'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            source_conversation_id = temporary_record["source_conversation_id"]
            source_lane_id = temporary_record["source_lane_id"]
            if source_conversation_id is not None and source_lane_id is not None:
                source_lane = connection.execute(
                    """
                    SELECT id FROM v2_lanes
                    WHERE id = ? AND conversation_id = ?
                    """,
                    (source_lane_id, source_conversation_id),
                ).fetchone()
                if source_lane is not None:
                    self._insert_lane_event(
                        connection,
                        event_id=lane_event_id,
                        conversation_id=source_conversation_id,
                        lane_id=source_lane_id,
                        event_type="temporary_conversation.deleted",
                        occurred_at=now,
                        data={
                            "temporaryConversationId": conversation_id,
                            "temporaryLaneId": root["id"] if root is not None else None,
                            "sourceConversationId": source_conversation_id,
                            "sourceLaneId": source_lane_id,
                            "sourceLeafEntryId": temporary_record["source_leaf_entry_id"],
                        },
                    )
            connection.execute(
                "DELETE FROM conversations WHERE id = ?",
                (conversation_id,),
            )

        self._write(operation)

    def set_run_assistant_entry(
        self,
        *,
        run_id: str,
        assistant_entry_id: str,
        is_active_variant: bool = False,
    ) -> RunRecord:
        def operation(connection: sqlite3.Connection) -> RunRecord:
            current = self._get_run_row(connection, run_id)
            entry = self._get_entry_row(connection, assistant_entry_id)
            if entry["conversation_id"] != current["conversation_id"]:
                raise InvalidStateError("Assistant entry does not belong to run conversation")
            if entry["lane_id"] != current["lane_id"]:
                raise InvalidStateError("Assistant entry does not belong to run lane")
            if is_active_variant:
                connection.execute(
                    """
                    UPDATE v2_runs
                    SET is_active_variant = 0
                    WHERE sibling_group_id = ? AND id != ?
                    """,
                    (current["sibling_group_id"], run_id),
                )
                leaf_row = connection.execute(
                    """
                    SELECT id FROM v2_transcript_entries
                    WHERE lane_id = ? AND source_run_id = ?
                    ORDER BY seq DESC
                    LIMIT 1
                    """,
                    (current["lane_id"], run_id),
                ).fetchone()
                if leaf_row is not None:
                    connection.execute(
                        "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                        (leaf_row["id"], current["lane_id"]),
                    )
            connection.execute(
                """
                UPDATE v2_runs
                SET assistant_entry_id = ?, is_active_variant = ?
                WHERE id = ?
                """,
                (assistant_entry_id, 1 if is_active_variant else 0, run_id),
            )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def set_active_run_variant(self, run_id: str) -> RunRecord:
        def operation(connection: sqlite3.Connection) -> RunRecord:
            current = self._get_run_row(connection, run_id)
            connection.execute(
                """
                UPDATE v2_runs
                SET is_active_variant = 0
                WHERE sibling_group_id = ? AND id != ?
                """,
                (current["sibling_group_id"], run_id),
            )
            connection.execute(
                "UPDATE v2_runs SET is_active_variant = 1 WHERE id = ?",
                (run_id,),
            )
            leaf_row = connection.execute(
                """
                SELECT id FROM v2_transcript_entries
                WHERE lane_id = ? AND source_run_id = ?
                ORDER BY seq DESC
                LIMIT 1
                """,
                (current["lane_id"], run_id),
            ).fetchone()
            if leaf_row is not None:
                connection.execute(
                    "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                    (leaf_row["id"], current["lane_id"]),
                )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def get_conversation_pointer(self, conversation_id: str) -> Optional[ConversationPointer]:
        with self._database.connect() as connection:
            self._ensure_conversation(connection, conversation_id)
            row = connection.execute(
                "SELECT * FROM v2_conversation_pointers WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        return ConversationPointer(
            conversation_id=row["conversation_id"],
            active_lane_id=row["active_lane_id"],
            active_run_id=row["active_run_id"],
            active_run_variant_id=row["active_run_variant_id"],
            updated_at=row["updated_at"],
        )

    def create_run(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        trigger_entry_id: str,
        sibling_group_id: Optional[str] = None,
        assistant_entry_id: Optional[str] = None,
        is_active_variant: bool = False,
        run_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        user_content_override: Optional[str] = None,
    ) -> RunRecord:
        run_id = run_id or self._id_factory("run")
        sibling_group_id = sibling_group_id or self._id_factory("run_group")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            self._ensure_conversation(connection, conversation_id)
            lane = self._get_lane_row(connection, lane_id)
            if lane["conversation_id"] != conversation_id:
                raise InvalidStateError("Lane does not belong to conversation")
            if lane["kind"] == LaneKind.ARCHIVED.value or lane["status"] == LaneStatus.ARCHIVED.value:
                raise ConflictError("Archived lanes cannot start runs")
            trigger = self._get_entry_row(connection, trigger_entry_id)
            if trigger["conversation_id"] != conversation_id:
                raise InvalidStateError("Trigger entry does not belong to conversation")
            if trigger["lane_id"] != lane_id:
                raise InvalidStateError("Trigger entry does not belong to run lane")
            if is_active_variant:
                connection.execute(
                    """
                    UPDATE v2_runs
                    SET is_active_variant = 0
                    WHERE sibling_group_id = ?
                    """,
                    (sibling_group_id,),
                )

            connection.execute(
                """
                INSERT INTO v2_runs(
                    id, conversation_id, lane_id, trigger_entry_id,
                    sibling_group_id, assistant_entry_id, is_active_variant,
                    status, created_at, correlation_id, trigger_content_override
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    conversation_id,
                    lane_id,
                    trigger_entry_id,
                    sibling_group_id,
                    assistant_entry_id,
                    1 if is_active_variant else 0,
                    RunStatus.CREATED.value,
                    now,
                    correlation_id,
                    user_content_override,
                ),
            )
            return RunRecord(
                id=run_id,
                conversation_id=conversation_id,
                lane_id=lane_id,
                trigger_entry_id=trigger_entry_id,
                sibling_group_id=sibling_group_id,
                assistant_entry_id=assistant_entry_id,
                is_active_variant=is_active_variant,
                status=RunStatus.CREATED,
                created_at=now,
                correlation_id=correlation_id,
                trigger_content_override=user_content_override,
            )

        return self._write(operation)

    def get_run(self, run_id: str) -> RunRecord:
        with self._database.connect() as connection:
            row = self._get_run_row(connection, run_id)
        return self._run_from_row(row)

    def list_run_variants(self, run_id: str) -> tuple[RunRecord, ...]:
        with self._database.connect() as connection:
            current = self._get_run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_runs
                WHERE sibling_group_id = ?
                ORDER BY created_at, id
                """,
                (current["sibling_group_id"],),
            ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def list_runs(
        self,
        *,
        conversation_id: Optional[str] = None,
        statuses: Optional[Sequence[RunStatus]] = None,
    ) -> tuple[RunRecord, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if conversation_id is not None:
            clauses.append("conversation_id = ?")
            parameters.append(conversation_id)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            parameters.extend(status.value for status in statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._database.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM v2_runs {where} ORDER BY created_at, id",
                parameters,
            ).fetchall()
        return tuple(self._run_from_row(row) for row in rows)

    def update_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        cancelled_by: Optional[str] = None,
    ) -> RunRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            current = self._get_run_row(connection, run_id)
            if current["status"] == status.value:
                return self._run_from_row(current)
            if current["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal run status cannot change")

            started_at = current["started_at"] or (
                now if status in ACTIVE_RUN_STATUSES else None
            )
            finished_at = (
                now
                if status
                in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_runs
                SET status = ?, started_at = ?, finished_at = ?,
                    error_code = ?, safe_message = ?, cancelled_by = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    error_code,
                    safe_message,
                    cancelled_by,
                    run_id,
                ),
            )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def start_run(
        self,
        run_id: str,
        *,
        correlation_id: Optional[str] = None,
    ) -> RunRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            current = self._get_run_row(connection, run_id)
            if current["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal run status cannot change")
            connection.execute(
                """
                UPDATE v2_runs
                SET is_active_variant = 0
                WHERE sibling_group_id = ? AND id != ?
                """,
                (current["sibling_group_id"], run_id),
            )
            connection.execute(
                """
                UPDATE v2_runs
                SET status = ?, started_at = COALESCE(started_at, ?),
                    is_active_variant = 1
                WHERE id = ?
                """,
                (RunStatus.RUNNING.value, now, run_id),
            )
            self._insert_runtime_event(
                connection,
                run_id=run_id,
                event_type="run_started",
                payload={},
                correlation_id=correlation_id or current["correlation_id"],
                occurred_at=now,
            )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def finalize_interrupted_run(
        self,
        run_id: str,
        *,
        error_code: str = "v2_interrupted",
        safe_message: str = "运行被中断，已按用户决策标记为失败。",
        deactivate_variant: bool = False,
    ) -> RunRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            run = self._get_run_row(connection, run_id)
            if run["status"] not in tuple(
                status.value for status in ACTIVE_RUN_STATUSES
            ):
                raise InvalidStateError("Run is not an interrupted active run")

            model_turns = connection.execute(
                """
                SELECT * FROM v2_model_turns
                WHERE run_id = ?
                ORDER BY turn_index, id
                """,
                (run_id,),
            ).fetchall()
            for turn in model_turns:
                if turn["status"] not in tuple(
                    status.value for status in _ACTIVE_MODEL_TURN_STATUSES
                ):
                    continue
                connection.execute(
                    """
                    UPDATE v2_model_turns
                    SET status = ?, started_at = COALESCE(started_at, ?),
                        finished_at = ?, error_code = ?, safe_message = ?
                    WHERE id = ?
                    """,
                    (
                        ModelTurnStatus.FAILED.value,
                        now,
                        now,
                        error_code,
                        safe_message,
                        turn["id"],
                    ),
                )
                self._insert_runtime_event(
                    connection,
                    run_id=run_id,
                    model_turn_id=str(turn["id"]),
                    event_type="model_turn_failed",
                    payload={
                        "errorCode": error_code,
                        "safeMessage": safe_message,
                    },
                    correlation_id=run["correlation_id"],
                    occurred_at=now,
                )

            tool_executions = connection.execute(
                """
                SELECT t.* FROM v2_tool_executions AS t
                JOIN v2_model_turns AS m ON m.id = t.model_turn_id
                WHERE m.run_id = ?
                ORDER BY m.turn_index, m.id, t.created_at, t.id
                """,
                (run_id,),
            ).fetchall()
            for tool in tool_executions:
                if tool["status"] not in tuple(
                    status.value for status in _ACTIVE_TOOL_EXECUTION_STATUSES
                ):
                    continue
                connection.execute(
                    """
                    UPDATE v2_tool_executions
                    SET status = ?, started_at = COALESCE(started_at, ?),
                        finished_at = ?, error_code = ?, safe_message = ?,
                        retryable = 0, error_correlation_id = ?,
                        error_details_json = ?
                    WHERE id = ?
                    """,
                    (
                        ToolExecutionStatus.FAILED.value,
                        now,
                        now,
                        error_code,
                        "中断后无法确认工具结果；请检查实际副作用后重试。",
                        run["correlation_id"],
                        _dump({"reason": "interrupted"}),
                        tool["id"],
                    ),
                )
                self._insert_runtime_event(
                    connection,
                    run_id=run_id,
                    model_turn_id=str(tool["model_turn_id"]),
                    event_type="tool_execution_failed",
                    payload={
                        "toolExecutionId": str(tool["id"]),
                        "status": ToolExecutionStatus.FAILED.value,
                        "errorCode": error_code,
                        "safeMessage": "中断后无法确认工具结果；请检查实际副作用后重试。",
                        "retryable": False,
                        "correlationId": run["correlation_id"],
                        "errorDetails": {"reason": "interrupted"},
                    },
                    correlation_id=run["correlation_id"],
                    occurred_at=now,
                )

            connection.execute(
                """
                UPDATE v2_runs
                SET status = ?, started_at = COALESCE(started_at, ?),
                    finished_at = ?, error_code = ?, safe_message = ?,
                    is_active_variant = ?
                WHERE id = ?
                """,
                (
                    RunStatus.FAILED.value,
                    now,
                    now,
                    error_code,
                    safe_message,
                    0 if deactivate_variant else run["is_active_variant"],
                    run_id,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=run_id,
                event_type="run_failed",
                payload={"errorCode": error_code, "safeMessage": safe_message},
                correlation_id=run["correlation_id"],
                occurred_at=now,
            )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def transition_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        cancelled_by: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> RunRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> RunRecord:
            current = self._get_run_row(connection, run_id)
            if current["status"] == status.value:
                return self._run_from_row(current)
            if current["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal run status cannot change")
            started_at = current["started_at"] or (
                now if status in ACTIVE_RUN_STATUSES else None
            )
            finished_at = (
                now
                if status
                in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_runs
                SET status = ?, started_at = ?, finished_at = ?,
                    error_code = ?, safe_message = ?, cancelled_by = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    error_code,
                    safe_message,
                    cancelled_by,
                    run_id,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=payload or {},
                correlation_id=correlation_id or current["correlation_id"],
                occurred_at=now,
            )
            return self._run_from_row(self._get_run_row(connection, run_id))

        return self._write(operation)

    def finalize_run(
        self,
        run_id: str,
        *,
        content: str,
        finish_reason: str,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        context_policy: Optional[Mapping[str, Any]] = None,
        display: Optional[Mapping[str, Any]] = None,
        correlation_id: Optional[str] = None,
    ) -> tuple[RunRecord, TranscriptEntryRecord]:
        now = self._clock()
        assistant_entry_id = self._id_factory("entry")

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[RunRecord, TranscriptEntryRecord]:
            run = self._get_run_row(connection, run_id)
            if run["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal run status cannot change")
            parent_entry = self._latest_run_entry_row(connection, run["id"])
            parent_id = (
                parent_entry["id"]
                if parent_entry is not None
                else run["trigger_entry_id"]
            )
            seq = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
                (run["lane_id"],),
            )
            payload = {
                "content": content,
                "finishReason": finish_reason,
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
            }
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
                    assistant_entry_id,
                    run["conversation_id"],
                    parent_id,
                    run["lane_id"],
                    seq,
                    TranscriptEntryType.ASSISTANT_MESSAGE.value,
                    1,
                    Actor.ASSISTANT.value,
                    TranscriptEntryStatus.FINAL.value,
                    now,
                    now,
                    _dump(payload),
                    _dump(
                        context_policy
                        or {"include_in_llm": True, "transform": "full"}
                    ),
                    _dump(display or {}),
                    run_id,
                ),
            )
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (assistant_entry_id, run["lane_id"]),
            )
            connection.execute(
                """
                UPDATE v2_runs
                SET is_active_variant = 0
                WHERE sibling_group_id = ? AND id != ?
                """,
                (run["sibling_group_id"], run_id),
            )
            connection.execute(
                """
                UPDATE v2_runs
                SET assistant_entry_id = ?, is_active_variant = 1, status = ?,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    assistant_entry_id,
                    RunStatus.COMPLETED.value,
                    now,
                    run_id,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=run_id,
                event_type="run_completed",
                payload={"assistantEntryId": assistant_entry_id},
                correlation_id=correlation_id or run["correlation_id"],
                occurred_at=now,
            )
            return (
                self._run_from_row(self._get_run_row(connection, run_id)),
                self._entry_from_row(self._get_entry_row(connection, assistant_entry_id)),
            )

        return self._write(operation)

    def create_model_turn(
        self,
        *,
        run_id: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> ModelTurnRecord:
        turn_id = turn_id or self._id_factory("turn")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ModelTurnRecord:
            run = self._get_run_row(connection, run_id)
            if run["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Cannot add a model turn to a terminal run")
            turn_index = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM v2_model_turns WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                """
                INSERT INTO v2_model_turns(
                    id, run_id, turn_index, status, provider, model,
                    request_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    run_id,
                    turn_index,
                    ModelTurnStatus.CREATED.value,
                    provider,
                    model,
                    request_id,
                    now,
                ),
            )
            return ModelTurnRecord(
                id=turn_id,
                run_id=run_id,
                turn_index=turn_index,
                status=ModelTurnStatus.CREATED,
                created_at=now,
                provider=provider,
                model=model,
                request_id=request_id,
            )

        return self._write(operation)

    def start_model_turn(
        self,
        run_id: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ModelTurnRecord:
        turn_id = turn_id or self._id_factory("turn")
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ModelTurnRecord:
            run = self._get_run_row(connection, run_id)
            if run["status"] in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Cannot add a model turn to a terminal run")
            turn_index = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM v2_model_turns WHERE run_id = ?",
                (run_id,),
            )
            connection.execute(
                """
                INSERT INTO v2_model_turns(
                    id, run_id, turn_index, status, provider, model,
                    request_id, created_at, started_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    run_id,
                    turn_index,
                    ModelTurnStatus.PROJECTING_CONTEXT.value,
                    provider,
                    model,
                    request_id,
                    now,
                    now,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=run_id,
                model_turn_id=turn_id,
                event_type="model_turn_status_changed",
                payload={"status": ModelTurnStatus.PROJECTING_CONTEXT.value},
                correlation_id=correlation_id or run["correlation_id"],
                occurred_at=now,
            )
            return self._model_turn_from_row(
                self._get_model_turn_row(connection, turn_id)
            )

        return self._write(operation)

    def transition_model_turn_status(
        self,
        turn_id: str,
        status: ModelTurnStatus,
        *,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ModelTurnRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ModelTurnRecord:
            current = self._get_model_turn_row(connection, turn_id)
            if current["status"] == status.value:
                return self._model_turn_from_row(current)
            if current["status"] in (
                ModelTurnStatus.COMPLETED.value,
                ModelTurnStatus.FAILED.value,
                ModelTurnStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal model turn status cannot change")
            started_at = current["started_at"] or (
                now if status is not ModelTurnStatus.CREATED else None
            )
            finished_at = (
                now
                if status
                in (
                    ModelTurnStatus.COMPLETED,
                    ModelTurnStatus.FAILED,
                    ModelTurnStatus.CANCELLED,
                )
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_model_turns
                SET status = ?, started_at = ?, finished_at = ?,
                    input_tokens = ?, output_tokens = ?,
                    error_code = ?, safe_message = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    input_tokens,
                    output_tokens,
                    error_code,
                    safe_message,
                    turn_id,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=current["run_id"],
                model_turn_id=turn_id,
                event_type=event_type,
                payload=payload or {},
                correlation_id=correlation_id,
                occurred_at=now,
            )
            return self._model_turn_from_row(
                self._get_model_turn_row(connection, turn_id)
            )

        return self._write(operation)

    def update_model_turn_status(
        self,
        turn_id: str,
        status: ModelTurnStatus,
        *,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
    ) -> ModelTurnRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ModelTurnRecord:
            current = self._get_model_turn_row(connection, turn_id)
            if current["status"] == status.value:
                return self._model_turn_from_row(current)
            if current["status"] in (
                ModelTurnStatus.COMPLETED.value,
                ModelTurnStatus.FAILED.value,
                ModelTurnStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Terminal model turn status cannot change")

            started_at = current["started_at"] or (
                now if status is not ModelTurnStatus.CREATED else None
            )
            finished_at = (
                now
                if status
                in (
                    ModelTurnStatus.COMPLETED,
                    ModelTurnStatus.FAILED,
                    ModelTurnStatus.CANCELLED,
                )
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_model_turns
                SET status = ?, started_at = ?, finished_at = ?,
                    input_tokens = ?, output_tokens = ?,
                    error_code = ?, safe_message = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    input_tokens,
                    output_tokens,
                    error_code,
                    safe_message,
                    turn_id,
                ),
            )
            return self._model_turn_from_row(self._get_model_turn_row(connection, turn_id))

        return self._write(operation)

    def create_tool_execution(
        self,
        *,
        model_turn_id: str,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        approval_id: Optional[str] = None,
        execution_id: Optional[str] = None,
    ) -> ToolExecutionRecord:
        execution_id = execution_id or self._id_factory("tool")
        now = self._clock()
        arguments_json = _dump(arguments)
        arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()

        def operation(connection: sqlite3.Connection) -> ToolExecutionRecord:
            turn = self._get_model_turn_row(connection, model_turn_id)
            if turn["status"] in (
                ModelTurnStatus.COMPLETED.value,
                ModelTurnStatus.FAILED.value,
                ModelTurnStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Cannot add a tool execution to a terminal model turn")
            connection.execute(
                """
                INSERT INTO v2_tool_executions(
                    id, model_turn_id, call_id, tool_name, arguments_hash,
                    arguments_json, status, approval_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    model_turn_id,
                    call_id,
                    tool_name,
                    arguments_hash,
                    arguments_json,
                    ToolExecutionStatus.CREATED.value,
                    approval_id,
                    now,
                ),
            )
            return ToolExecutionRecord(
                id=execution_id,
                model_turn_id=model_turn_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments_hash=arguments_hash,
                arguments=dict(arguments),
                status=ToolExecutionStatus.CREATED,
                created_at=now,
                approval_id=approval_id,
            )

        return self._write(operation)

    def record_tool_call(
        self,
        *,
        model_turn_id: str,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        execution_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> tuple[ToolExecutionRecord, TranscriptEntryRecord]:
        execution_id = execution_id or self._id_factory("tool")
        entry_id = self._id_factory("entry")
        now = self._clock()
        arguments_json = _dump(arguments)
        arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[ToolExecutionRecord, TranscriptEntryRecord]:
            turn = self._get_model_turn_row(connection, model_turn_id)
            if turn["status"] in (
                ModelTurnStatus.COMPLETED.value,
                ModelTurnStatus.FAILED.value,
                ModelTurnStatus.CANCELLED.value,
            ):
                raise InvalidStateError("Cannot add a tool execution to a terminal model turn")
            run = self._get_run_row(connection, turn["run_id"])
            parent_entry = self._latest_run_entry_row(connection, run["id"])
            parent_id = (
                parent_entry["id"]
                if parent_entry is not None
                else run["trigger_entry_id"]
            )
            connection.execute(
                """
                INSERT INTO v2_tool_executions(
                    id, model_turn_id, call_id, tool_name, arguments_hash,
                    arguments_json, status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    model_turn_id,
                    call_id,
                    tool_name,
                    arguments_hash,
                    arguments_json,
                    ToolExecutionStatus.CREATED.value,
                    now,
                ),
            )
            seq = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
                (run["lane_id"],),
            )
            payload = {
                "callId": call_id,
                "toolName": tool_name,
                "arguments": dict(arguments),
            }
            context_policy = {
                "include_in_llm": False,
                "transform": "none",
                "trust_level": "untrusted",
            }
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
                    run["conversation_id"],
                    parent_id,
                    run["lane_id"],
                    seq,
                    TranscriptEntryType.TOOL_CALL.value,
                    1,
                    Actor.TOOL.value,
                    TranscriptEntryStatus.FINAL.value,
                    now,
                    now,
                    _dump(payload),
                    _dump(context_policy),
                    _dump({"toolExecutionId": execution_id}),
                    run["id"],
                ),
            )
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (entry_id, run["lane_id"]),
            )
            self._insert_runtime_event(
                connection,
                run_id=run["id"],
                model_turn_id=model_turn_id,
                event_type="tool_execution_created",
                payload={"toolExecutionId": execution_id, "callId": call_id},
                correlation_id=correlation_id or run["correlation_id"],
                occurred_at=now,
            )
            return (
                self._tool_execution_from_row(
                    self._get_tool_execution_row(connection, execution_id)
                ),
                self._entry_from_row(self._get_entry_row(connection, entry_id)),
            )

        return self._write(operation)

    def transition_tool_execution_status(
        self,
        execution_id: str,
        status: ToolExecutionStatus,
        *,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> ToolExecutionRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ToolExecutionRecord:
            current = self._get_tool_execution_row(connection, execution_id)
            if current["status"] == status.value:
                return self._tool_execution_from_row(current)
            if current["status"] in (
                ToolExecutionStatus.COMPLETED.value,
                ToolExecutionStatus.FAILED.value,
                ToolExecutionStatus.CANCELLED.value,
                ToolExecutionStatus.REJECTED.value,
                ToolExecutionStatus.EXPIRED.value,
            ):
                raise InvalidStateError("Terminal tool execution status cannot change")
            started_at = current["started_at"] or (
                now if status is ToolExecutionStatus.RUNNING else None
            )
            finished_at = (
                now
                if status
                in (
                    ToolExecutionStatus.COMPLETED,
                    ToolExecutionStatus.FAILED,
                    ToolExecutionStatus.CANCELLED,
                    ToolExecutionStatus.REJECTED,
                    ToolExecutionStatus.EXPIRED,
                )
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_tool_executions
                SET status = ?, started_at = ?, finished_at = ?,
                    error_code = ?, safe_message = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    error_code,
                    safe_message,
                    execution_id,
                ),
            )
            turn = self._get_model_turn_row(connection, current["model_turn_id"])
            self._insert_runtime_event(
                connection,
                run_id=turn["run_id"],
                model_turn_id=current["model_turn_id"],
                event_type=event_type,
                payload=payload or {"toolExecutionId": execution_id},
                correlation_id=correlation_id,
                occurred_at=now,
            )
            return self._tool_execution_from_row(
                self._get_tool_execution_row(connection, execution_id)
            )

        return self._write(operation)

    def mark_tool_execution_approved(
        self,
        execution_id: str,
        approval_id: str,
        *,
        event_type: str = "tool_execution_approved",
    ) -> ToolExecutionRecord:
        """记录"该次执行获得了审批证据"（S5：让 approval_gate 指标可判定）。

        审批通过/修改后写入 ``approval_id``；未获批而执行（或拒绝）不写，
        eval 的 ``approval_gate`` 因此能区分"审批后执行"与"绕过审批"。
        """
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ToolExecutionRecord:
            current = self._get_tool_execution_row(connection, execution_id)
            if current["approval_id"] == approval_id:
                return self._tool_execution_from_row(current)
            connection.execute(
                "UPDATE v2_tool_executions SET approval_id = ? WHERE id = ?",
                (approval_id, execution_id),
            )
            turn = self._get_model_turn_row(connection, current["model_turn_id"])
            self._insert_runtime_event(
                connection,
                run_id=turn["run_id"],
                model_turn_id=current["model_turn_id"],
                event_type=event_type,
                payload={
                    "toolExecutionId": execution_id,
                    "approvalId": approval_id,
                },
                occurred_at=now,
            )
            return self._tool_execution_from_row(
                self._get_tool_execution_row(connection, execution_id)
            )

        return self._write(operation)

    def record_tool_result(
        self,
        execution_id: str,
        *,
        status: ToolExecutionStatus,
        content: str,
        event_type: str,
        error: Optional[ToolCallError] = None,
        correlation_id: Optional[str] = None,
        structured_content: JsonValue = None,
    ) -> tuple[ToolExecutionRecord, TranscriptEntryRecord]:
        entry_id = self._id_factory("entry")
        now = self._clock()
        error_code = error.code if error is not None else None
        safe_message = error.safe_message if error is not None else None
        retryable = error.retryable if error is not None else None
        error_correlation_id = (
            error.correlation_id if error is not None else None
        )
        error_details = (
            _jsonable(dict(error.details)) if error is not None else None
        )
        error_details_json = _dump(error_details) if error_details is not None else None

        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[ToolExecutionRecord, TranscriptEntryRecord]:
            current = self._get_tool_execution_row(connection, execution_id)
            if current["status"] in (
                ToolExecutionStatus.COMPLETED.value,
                ToolExecutionStatus.FAILED.value,
                ToolExecutionStatus.CANCELLED.value,
                ToolExecutionStatus.REJECTED.value,
                ToolExecutionStatus.EXPIRED.value,
            ):
                raise InvalidStateError("Terminal tool execution status cannot change")
            turn = self._get_model_turn_row(connection, current["model_turn_id"])
            run = self._get_run_row(connection, turn["run_id"])
            persisted_error_correlation_id = (
                error_correlation_id or run["correlation_id"]
                if error is not None
                else None
            )
            parent_entry = self._latest_run_entry_row(connection, run["id"])
            parent_id = (
                parent_entry["id"]
                if parent_entry is not None
                else run["trigger_entry_id"]
            )
            seq = self._next_seq(
                connection,
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM v2_transcript_entries WHERE lane_id = ?",
                (run["lane_id"],),
            )
            payload = {
                "toolExecutionId": execution_id,
                "callId": current["call_id"],
                "toolName": current["tool_name"],
                "content": content,
                "errorCode": error_code,
                "safeMessage": safe_message,
                "retryable": retryable,
                "correlationId": persisted_error_correlation_id,
                "errorDetails": error_details,
                "structuredContent": _jsonable(structured_content),
            }
            context_policy = {
                "include_in_llm": True,
                "transform": "tool_result",
                "trust_level": "untrusted",
            }
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
                    run["conversation_id"],
                    parent_id,
                    run["lane_id"],
                    seq,
                    TranscriptEntryType.TOOL_RESULT.value,
                    1,
                    Actor.TOOL.value,
                    TranscriptEntryStatus.FINAL.value,
                    now,
                    now,
                    _dump(payload),
                    _dump(context_policy),
                    _dump({"toolExecutionId": execution_id}),
                    run["id"],
                ),
            )
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (entry_id, run["lane_id"]),
            )
            finished_at = (
                now
                if status
                in (
                    ToolExecutionStatus.COMPLETED,
                    ToolExecutionStatus.FAILED,
                    ToolExecutionStatus.CANCELLED,
                    ToolExecutionStatus.REJECTED,
                    ToolExecutionStatus.EXPIRED,
                )
                else None
            )
            connection.execute(
                """
                UPDATE v2_tool_executions
                SET status = ?, finished_at = ?, error_code = ?,
                    safe_message = ?, retryable = ?, error_correlation_id = ?,
                    error_details_json = ?, result_entry_id = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    finished_at,
                    error_code,
                    safe_message,
                    None if retryable is None else int(retryable),
                    persisted_error_correlation_id,
                    error_details_json,
                    entry_id,
                    execution_id,
                ),
            )
            self._insert_runtime_event(
                connection,
                run_id=run["id"],
                model_turn_id=current["model_turn_id"],
                event_type=event_type,
                payload={
                    "toolExecutionId": execution_id,
                    "resultEntryId": entry_id,
                    "status": status.value,
                    "content": content,
                    "errorCode": error_code,
                    "safeMessage": safe_message,
                    "retryable": retryable,
                    "correlationId": persisted_error_correlation_id,
                    "errorDetails": error_details,
                },
                correlation_id=correlation_id or run["correlation_id"],
                occurred_at=now,
            )
            return (
                self._tool_execution_from_row(
                    self._get_tool_execution_row(connection, execution_id)
                ),
                self._entry_from_row(self._get_entry_row(connection, entry_id)),
            )

        return self._write(operation)

    def get_tool_execution(self, execution_id: str) -> ToolExecutionRecord:
        with self._database.connect() as connection:
            row = self._get_tool_execution_row(connection, execution_id)
        return self._tool_execution_from_row(row)

    def list_tool_executions(self, model_turn_id: str) -> tuple[ToolExecutionRecord, ...]:
        with self._database.connect() as connection:
            self._get_model_turn_row(connection, model_turn_id)
            rows = connection.execute(
                """
                SELECT * FROM v2_tool_executions
                WHERE model_turn_id = ?
                ORDER BY created_at, id
                """,
                (model_turn_id,),
            ).fetchall()
        return tuple(self._tool_execution_from_row(row) for row in rows)

    def update_tool_execution_status(
        self,
        execution_id: str,
        status: ToolExecutionStatus,
        *,
        error_code: Optional[str] = None,
        safe_message: Optional[str] = None,
        result_entry_id: Optional[str] = None,
    ) -> ToolExecutionRecord:
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ToolExecutionRecord:
            current = self._get_tool_execution_row(connection, execution_id)
            if current["status"] == status.value:
                return self._tool_execution_from_row(current)
            if current["status"] in (
                ToolExecutionStatus.COMPLETED.value,
                ToolExecutionStatus.FAILED.value,
                ToolExecutionStatus.CANCELLED.value,
                ToolExecutionStatus.REJECTED.value,
                ToolExecutionStatus.EXPIRED.value,
            ):
                raise InvalidStateError("Terminal tool execution status cannot change")
            if result_entry_id is not None:
                self._get_entry_row(connection, result_entry_id)

            started_at = current["started_at"] or (
                now if status is ToolExecutionStatus.RUNNING else None
            )
            finished_at = (
                now
                if status
                in (
                    ToolExecutionStatus.COMPLETED,
                    ToolExecutionStatus.FAILED,
                    ToolExecutionStatus.CANCELLED,
                    ToolExecutionStatus.REJECTED,
                    ToolExecutionStatus.EXPIRED,
                )
                else current["finished_at"]
            )
            connection.execute(
                """
                UPDATE v2_tool_executions
                SET status = ?, started_at = ?, finished_at = ?,
                    error_code = ?, safe_message = ?, result_entry_id = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    started_at,
                    finished_at,
                    error_code,
                    safe_message,
                    result_entry_id,
                    execution_id,
                ),
            )
            return self._tool_execution_from_row(
                self._get_tool_execution_row(connection, execution_id)
            )

        return self._write(operation)

    def update_tool_execution_arguments(
        self,
        execution_id: str,
        arguments: Mapping[str, Any],
    ) -> ToolExecutionRecord:
        """A1-modify：审批"修改参数"后，把用户修正的参数写回执行记录。

        保持单次执行在重放/恢复时参数一致（执行记录是执行时参数的真源）。
        """
        now = self._clock()

        def operation(connection: sqlite3.Connection) -> ToolExecutionRecord:
            current = self._get_tool_execution_row(connection, execution_id)
            if current["status"] in (
                ToolExecutionStatus.COMPLETED.value,
                ToolExecutionStatus.FAILED.value,
                ToolExecutionStatus.CANCELLED.value,
                ToolExecutionStatus.REJECTED.value,
                ToolExecutionStatus.EXPIRED.value,
            ):
                raise InvalidStateError(
                    "Terminal tool execution arguments cannot change"
                )
            arguments_json = _dump(arguments)
            arguments_hash = hashlib.sha256(
                arguments_json.encode("utf-8")
            ).hexdigest()
            connection.execute(
                "UPDATE v2_tool_executions SET "
                "arguments_json = ?, arguments_hash = ?, updated_at = ? "
                "WHERE id = ?",
                (arguments_json, arguments_hash, now, execution_id),
            )
            return self._tool_execution_from_row(self._get_tool_execution_row(
                connection, execution_id,
            ))

        return self._write(operation)

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

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        # 死锁加固（06 §30.5）：同线程嵌套 _write 会让内层 BEGIN IMMEDIATE 等待外层
        # 写锁并静默挂起（busy_timeout 在本死锁下不收敛）。重入检测把该场景变成显式
        # 报错，暴露真实嵌套调用点，而不是无限等待。
        depth = getattr(self._write_depth, "value", 0)
        if depth:
            raise InvalidStateError(
                "nested_database_write",
                "RuntimeV2 仓库写操作在同线程内嵌套调用（可能的死锁路径）；"
                "请将嵌套写移到事务外。",
            )
        self._write_depth.value = depth + 1
        try:
            with self._database.transaction() as connection:
                return operation(connection)
        except sqlite3.IntegrityError as error:
            raise ConflictError(str(error)) from error
        finally:
            self._write_depth.value = depth

    def _insert_runtime_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        model_turn_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> RuntimeEventRecord:
        self._get_run_row(connection, run_id)
        if model_turn_id is not None:
            turn = self._get_model_turn_row(connection, model_turn_id)
            if turn["run_id"] != run_id:
                raise InvalidStateError("Model turn does not belong to run")
        event_id = event_id or self._id_factory("event")
        occurred_at = occurred_at or self._clock()
        event_seq = self._next_seq(
            connection,
            "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM v2_runtime_events WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            """
            INSERT INTO v2_runtime_events(
                event_id, run_id, model_turn_id, event_seq, event_type,
                occurred_at, correlation_id, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                model_turn_id,
                event_seq,
                event_type,
                occurred_at,
                correlation_id,
                _dump(payload),
            ),
        )
        return RuntimeEventRecord(
            event_id=event_id,
            run_id=run_id,
            model_turn_id=model_turn_id,
            event_seq=event_seq,
            event_type=event_type,
            occurred_at=occurred_at,
            correlation_id=correlation_id,
            payload=dict(payload),
        )

    @staticmethod
    def _ensure_conversation(
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Conversation not found: {conversation_id}")

    @staticmethod
    def _get_lane_row(connection: sqlite3.Connection, lane_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_lanes WHERE id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Lane not found: {lane_id}")
        return row

    @staticmethod
    def _lane_subtree_rows(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        lane_id: str,
        excluded_lane_id: Optional[str] = None,
    ) -> tuple[sqlite3.Row, ...]:
        rows = connection.execute(
            """
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM v2_lanes
                WHERE id = ? AND conversation_id = ?
                UNION ALL
                SELECT child.id
                FROM v2_lanes AS child
                JOIN subtree AS parent ON child.source_lane_id = parent.id
                WHERE child.conversation_id = ?
                  AND (? IS NULL OR child.id <> ?)
            )
            SELECT lanes.*
            FROM v2_lanes AS lanes
            JOIN subtree ON subtree.id = lanes.id
            ORDER BY lanes.created_at, lanes.id
            """,
            (
                lane_id,
                conversation_id,
                conversation_id,
                excluded_lane_id,
                excluded_lane_id,
            ),
        ).fetchall()
        if not rows:
            raise NotFoundError(f"Lane not found: {lane_id}")
        return tuple(rows)

    @staticmethod
    def _get_temporary_conversation_row(
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_temporary_conversations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Temporary conversation not found: {conversation_id}")
        return row

    @staticmethod
    def _get_entry_row(connection: sqlite3.Connection, entry_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_transcript_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Transcript entry not found: {entry_id}")
        return row

    @staticmethod
    def _get_run_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Run not found: {run_id}")
        return row

    @staticmethod
    def _get_model_turn_row(
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_model_turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Model turn not found: {turn_id}")
        return row

    @staticmethod
    def _get_tool_execution_row(
        connection: sqlite3.Connection,
        execution_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_tool_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Tool execution not found: {execution_id}")
        return row

    @staticmethod
    def _latest_run_entry_row(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM v2_transcript_entries
            WHERE source_run_id = ?
            ORDER BY seq DESC, id DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    @staticmethod
    def _insert_lane_event(
        connection: sqlite3.Connection,
        *,
        event_id: str,
        conversation_id: str,
        lane_id: str,
        event_type: str,
        occurred_at: str,
        data: Mapping[str, Any],
    ) -> LaneEventRecord:
        event_seq = SqliteRuntimeV2Repository._next_seq(
            connection,
            """
            SELECT COALESCE(MAX(event_seq), 0) + 1
            FROM v2_lane_events WHERE conversation_id = ?
            """,
            (conversation_id,),
        )
        connection.execute(
            """
            INSERT INTO v2_lane_events(
                id, conversation_id, lane_id, event_seq, event_type,
                occurred_at, data_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                conversation_id,
                lane_id,
                event_seq,
                event_type,
                occurred_at,
                _dump(data),
            ),
        )
        return SqliteRuntimeV2Repository._lane_event_from_row(
            connection.execute(
                "SELECT * FROM v2_lane_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        )

    @staticmethod
    def _next_seq(
        connection: sqlite3.Connection,
        query: str,
        parameters: Sequence[Any],
    ) -> int:
        row = connection.execute(query, parameters).fetchone()
        return int(row[0])

    @staticmethod
    def _entry_excerpt(row: sqlite3.Row, *, max_chars: int = 80) -> str:
        payload = _load(row["payload_json"])
        content = payload.get("content")
        if not isinstance(content, str):
            return ""
        normalized = " ".join(content.split())
        if len(normalized) <= max_chars:
            return normalized
        return normalized[: max_chars - 1] + "…"

    @staticmethod
    def _temporary_conversation_from_row(
        row: sqlite3.Row,
    ) -> TemporaryConversationRecord:
        payload = _load(row["snapshot_entry_ids_json"])
        entry_ids = payload.get("entryIds", [])
        return TemporaryConversationRecord(
            conversation_id=row["conversation_id"],
            source_conversation_id=row["source_conversation_id"],
            source_lane_id=row["source_lane_id"],
            source_base_entry_id=row["source_base_entry_id"],
            source_leaf_entry_id=row["source_leaf_entry_id"],
            snapshot_entry_ids=tuple(str(item) for item in entry_ids),
            created_at=row["created_at"],
            promoted_at=row["promoted_at"],
        )

    @staticmethod
    def _lane_from_row(row: sqlite3.Row) -> LaneRecord:
        metadata = _load(row["metadata_json"])
        raw_kind = LaneKind(row["kind"])
        effective_kind = (
            LaneKind.PERSISTENT_BRANCH
            if raw_kind is LaneKind.ARCHIVED
            else raw_kind
        )
        status = LaneStatus(row["status"])
        if raw_kind is LaneKind.ARCHIVED:
            status = LaneStatus.ARCHIVED
        source_lane_id = row["source_lane_id"]
        if source_lane_id is None:
            raw_source_lane_id = metadata.get("sourceLaneId")
            source_lane_id = raw_source_lane_id if isinstance(raw_source_lane_id, str) else None
        return LaneRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            kind=effective_kind,
            base_entry_id=row["base_entry_id"],
            leaf_entry_id=row["leaf_entry_id"],
            created_at=row["created_at"],
            status=status,
            archived_at=row["archived_at"],
            display_name=row["display_name"],
            summary=row["summary"],
            source_lane_id=source_lane_id,
            created_from_entry_id=row["created_from_entry_id"],
            metadata=metadata,
        )

    @staticmethod
    def _lane_event_from_row(row: sqlite3.Row) -> LaneEventRecord:
        return LaneEventRecord(
            event_id=row["id"],
            conversation_id=row["conversation_id"],
            lane_id=row["lane_id"],
            event_seq=row["event_seq"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            data=_load(row["data_json"]),
        )

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> TranscriptEntryRecord:
        return TranscriptEntryRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            parent_id=row["parent_id"],
            lane_id=row["lane_id"],
            seq=row["seq"],
            type=TranscriptEntryType(row["type"]),
            type_version=row["type_version"],
            actor=Actor(row["actor"]),
            status=TranscriptEntryStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            payload=_load(row["payload_json"]),
            context_policy=_load(row["context_policy_json"]),
            display=_load(row["display_json"]),
            source_run_id=row["source_run_id"],
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            lane_id=row["lane_id"],
            trigger_entry_id=row["trigger_entry_id"],
            sibling_group_id=row["sibling_group_id"],
            assistant_entry_id=row["assistant_entry_id"],
            is_active_variant=bool(row["is_active_variant"]),
            status=RunStatus(row["status"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            cancelled_by=row["cancelled_by"],
            error_code=row["error_code"],
            safe_message=row["safe_message"],
            correlation_id=row["correlation_id"],
            trigger_content_override=(
                row["trigger_content_override"]
                if "trigger_content_override" in row.keys()
                else None
            ),
        )

    @staticmethod
    def _model_turn_from_row(row: sqlite3.Row) -> ModelTurnRecord:
        return ModelTurnRecord(
            id=row["id"],
            run_id=row["run_id"],
            turn_index=row["turn_index"],
            status=ModelTurnStatus(row["status"]),
            created_at=row["created_at"],
            provider=row["provider"],
            model=row["model"],
            request_id=row["request_id"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_code=row["error_code"],
            safe_message=row["safe_message"],
        )

    @staticmethod
    def _tool_execution_from_row(row: sqlite3.Row) -> ToolExecutionRecord:
        return ToolExecutionRecord(
            id=row["id"],
            model_turn_id=row["model_turn_id"],
            call_id=row["call_id"],
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            arguments=_load(row["arguments_json"]),
            status=ToolExecutionStatus(row["status"]),
            created_at=row["created_at"],
            approval_id=row["approval_id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error_code=row["error_code"],
            safe_message=row["safe_message"],
            retryable=(
                None if row["retryable"] is None else bool(row["retryable"])
            ),
            correlation_id=row["error_correlation_id"],
            error_details=(
                _load(row["error_details_json"])
                if row["error_details_json"] is not None
                else None
            ),
            result_entry_id=row["result_entry_id"],
        )

    @staticmethod
    def _runtime_event_from_row(row: sqlite3.Row) -> RuntimeEventRecord:
        return RuntimeEventRecord(
            event_id=row["event_id"],
            run_id=row["run_id"],
            model_turn_id=row["model_turn_id"],
            event_seq=row["event_seq"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            correlation_id=row["correlation_id"],
            payload=_load(row["payload_json"]),
        )

    @staticmethod
    def _product_event_from_row(row: sqlite3.Row) -> ProductRuntimeEventRecord:
        return ProductRuntimeEventRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            event_seq=row["event_seq"],
            event_type=row["event_type"],
            run_id=row["run_id"],
            lane_id=row["lane_id"],
            source_event_id=row["source_event_id"],
            occurred_at=row["occurred_at"],
            data=_load(row["data_json"]),
        )

    @staticmethod
    def _context_compaction_from_row(row: sqlite3.Row) -> ContextCompactionRecord:
        covered = _load(row["covered_entry_ids_json"]).get("entryIds", [])
        return ContextCompactionRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            lane_id=row["lane_id"],
            base_entry_id=row["base_entry_id"],
            summary_entry_id=row["summary_entry_id"],
            covered_entry_ids=tuple(str(value) for value in covered),
            tokens_before=row["tokens_before"],
            tokens_after=row["tokens_after"],
            created_at=row["created_at"],
        )
