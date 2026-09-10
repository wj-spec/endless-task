"""run 与 model turn 的状态机与持久化。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）：方法体一字未改，
只是换了宿主类。共享的 `self._database` / `self._write` / 行映射由
`V2RepositoryBase` 提供。
"""

from __future__ import annotations

from .base import V2RepositoryBase


from .base import ACTIVE_RUN_STATUSES, _ACTIVE_MODEL_TURN_STATUSES, _ACTIVE_TOOL_EXECUTION_STATUSES, _dump
from collections.abc import Mapping
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime_v2.domain import Actor, LaneKind, LaneStatus, ModelTurnRecord, ModelTurnStatus, RunRecord, RunStatus, ToolExecutionStatus, TranscriptEntryRecord, TranscriptEntryStatus, TranscriptEntryType
import sqlite3
from typing import Any, Optional, Sequence


class RunRepositoryMixin(V2RepositoryBase):
    """见模块 docstring。"""

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
