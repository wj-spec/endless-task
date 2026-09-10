"""工具执行记录：创建、状态流转、审批证据、结果落盘。

从 `storage/sqlite_runtime_v2_repository.py` 拆出（行为零改动）：方法体一字未改，
只是换了宿主类。共享的 `self._database` / `self._write` / 行映射由
`V2RepositoryBase` 提供。
"""

from __future__ import annotations

from .base import V2RepositoryBase


from .base import _dump, _jsonable
from collections.abc import Mapping
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime_v2.domain import Actor, ModelTurnStatus, ToolExecutionRecord, ToolExecutionStatus, TranscriptEntryRecord, TranscriptEntryStatus, TranscriptEntryType
from endless_task.tooling import JsonValue, ToolCallError
import hashlib
import sqlite3
from typing import Any, Optional


class ToolExecutionRepositoryMixin(V2RepositoryBase):
    """见模块 docstring。"""

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
