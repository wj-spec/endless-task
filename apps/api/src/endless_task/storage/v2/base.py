"""v2 仓储的共享底座：连接写入、序号、行 → 记录映射。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）。各族的 mixin
都继承它，因此 `self._database` / `self._write` / `self._*_from_row` 在混入后
仍然可用；聚合类 `SqliteRuntimeV2Repository` 只是把它们组合起来，对外的方法面
一字不变（由 tests/test_storage_method_inventory.py 冻结）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from endless_task.domain.repositories import ConflictError, InvalidStateError, NotFoundError
from endless_task.runtime_v2.domain import (
    ConversationPointer,
    LaneKind,
    LaneStatus,
    LaneEventRecord,
    LaneRecord,
    ModelTurnStatus,
    ProductRuntimeEventRecord,
    RunStatus,
    RuntimeEventRecord,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
    TranscriptEntryStatus,
    TranscriptEntryType,
    TemporaryConversationRecord,
    Actor,
    ContextCompactionRecord,
    ModelTurnRecord,
    RunRecord,
)

from ..database import Database


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


class V2RepositoryBase:

    #: per-thread write-reentrancy guard (06 §30.5).
    _write_depth = threading.local()

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

    @staticmethod
    def _next_seq(
        connection: sqlite3.Connection,
        query: str,
        parameters: Sequence[Any],
    ) -> int:
        row = connection.execute(query, parameters).fetchone()
        return int(row[0])

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
    def _get_lane_row(connection: sqlite3.Connection, lane_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM v2_lanes WHERE id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Lane not found: {lane_id}")
        return row

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
        event_seq = V2RepositoryBase._next_seq(
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
        return V2RepositoryBase._lane_event_from_row(
            connection.execute(
                "SELECT * FROM v2_lane_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        )

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
