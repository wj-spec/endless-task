from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

from endless_task.domain.models import ToolCallJournal
from endless_task.domain.repositories import InvalidStateError, NotFoundError, ValidationError
from endless_task.runtime.events import RuntimeEvent
from endless_task.tooling import (
    ApprovalRequest,
    ApprovalStatus,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolActivityCopy,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
)

from .database import Database


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class SqliteRuntimeRepository:
    """Persists runtime transitions and their public events atomically."""

    def __init__(self, database: Database, *, clock=utc_now) -> None:
        self._database = database
        self._clock = clock

    def start_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        provider: str,
        model: str,
    ) -> RuntimeEvent:
        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            if row["turn_status"] != "created" or row["variant_status"] != "created":
                raise InvalidStateError("Response must be created before it can run")

            now = self._clock()
            connection.execute(
                """
                UPDATE response_variants
                SET status = 'running', provider = ?, model = ?, started_at = ?
                WHERE id = ?
                """,
                (provider, model, now, variant_id),
            )
            connection.execute(
                """
                UPDATE turns
                SET status = 'running', started_at = ?, finished_at = NULL
                WHERE id = ?
                """,
                (now, turn_id),
            )
            return self._append_event(
                connection,
                row=row,
                event_type="turn.started",
                occurred_at=now,
                data={"attempt": row["variant_index"], "operation": row["operation"]},
                include_message=False,
            )

    def start_message(self, *, turn_id: str, variant_id: str) -> RuntimeEvent:
        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            self._require_running(row)
            return self._append_event(
                connection,
                row=row,
                event_type="message.started",
                occurred_at=self._clock(),
                data={"role": "assistant"},
            )

    def append_text_delta(
        self,
        *,
        turn_id: str,
        variant_id: str,
        delta: str,
        accumulated_content: str,
    ) -> RuntimeEvent:
        if not delta:
            raise ValidationError("Text delta cannot be empty")

        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            self._require_running(row)
            now = self._clock()
            connection.execute(
                "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
                (accumulated_content, now, row["assistant_message_id"]),
            )
            return self._append_event(
                connection,
                row=row,
                event_type="message.delta",
                occurred_at=now,
                data={"delta": delta},
            )

    def complete_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        content: str,
        finish_reason: str,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
    ) -> tuple[RuntimeEvent, RuntimeEvent]:
        if finish_reason not in ("stop", "length", "content_filter"):
            raise ValidationError("Unsupported successful finish reason")

        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            self._require_running(row)
            now = self._clock()
            connection.execute(
                "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, row["assistant_message_id"]),
            )
            connection.execute(
                """
                UPDATE response_variants
                SET status = 'completed', finish_reason = ?, error_code = NULL,
                    input_tokens = ?, output_tokens = ?, finished_at = ?
                WHERE id = ?
                """,
                (finish_reason, input_tokens, output_tokens, now, variant_id),
            )
            connection.execute(
                "UPDATE turns SET status = 'completed', finished_at = ? WHERE id = ?",
                (now, turn_id),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, row["conversation_id"]),
            )
            message_event = self._append_event(
                connection,
                row=row,
                event_type="message.completed",
                occurred_at=now,
                data={
                    "content": content,
                    "finishReason": finish_reason,
                    **self._usage_data(input_tokens, output_tokens),
                },
            )
            turn_event = self._append_event(
                connection,
                row=row,
                event_type="turn.completed",
                occurred_at=now,
                data={"finishReason": finish_reason},
                include_message=False,
            )
            return message_event, turn_event

    def fail_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        error_code: str,
        safe_message: str,
        retryable: bool,
        correlation_id: str,
        retry_after_ms: Optional[int] = None,
    ) -> RuntimeEvent:
        if not error_code or not safe_message or not correlation_id:
            raise ValidationError("Failure fields cannot be empty")

        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            if row["turn_status"] not in ("created", "running"):
                raise InvalidStateError("Only an active response can fail")
            now = self._clock()
            self._write_terminal_state(
                connection,
                row=row,
                status="failed",
                content=partial_content,
                finish_reason="error",
                error_code=error_code,
                occurred_at=now,
            )
            error: dict[str, object] = {
                "code": error_code,
                "message": safe_message,
                "retryable": retryable,
                "correlationId": correlation_id,
            }
            if retry_after_ms is not None:
                error["retryAfterMs"] = retry_after_ms
            data: dict[str, object] = {"error": error}
            if partial_content:
                data["partialContent"] = partial_content
            return self._append_event(
                connection,
                row=row,
                event_type="turn.failed",
                occurred_at=now,
                data=data,
            )

    def cancel_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        cancelled_by: str = "user",
    ) -> Optional[RuntimeEvent]:
        if cancelled_by not in ("user", "system"):
            raise ValidationError("cancelled_by must be user or system")

        with self._database.transaction() as connection:
            row = self._active_execution(connection, turn_id, variant_id)
            if row["turn_status"] == "cancelled":
                return self._last_event_of_type(connection, turn_id, "turn.cancelled")
            if row["turn_status"] not in ("created", "running"):
                return None

            now = self._clock()
            self._write_terminal_state(
                connection,
                row=row,
                status="cancelled",
                content=partial_content,
                finish_reason="cancelled",
                error_code=None,
                occurred_at=now,
            )
            data: dict[str, object] = {"cancelledBy": cancelled_by}
            if partial_content:
                data["partialContent"] = partial_content
            return self._append_event(
                connection,
                row=row,
                event_type="turn.cancelled",
                occurred_at=now,
                data=data,
            )

    def prepare_tool_call(
        self,
        *,
        call: ToolCall,
        definition: ToolDefinition,
        approval_prompt: Optional[ToolApprovalPrompt] = None,
        auto_authorized: bool = False,
    ) -> tuple[Optional[ApprovalRequest], Optional[RuntimeEvent]]:
        requires_approval = definition.approval_mode is ToolApprovalMode.REQUIRED
        if requires_approval:
            if auto_authorized == (approval_prompt is not None):
                raise ValidationError(
                    "Approval prompt must match the tool approval policy"
                )
        elif approval_prompt is not None or auto_authorized:
            raise ValidationError("Approval prompt must match the tool approval policy")

        with self._database.transaction() as connection:
            row = self._active_execution(
                connection,
                call.turn_id,
                call.response_variant_id,
            )
            self._require_running(row)
            status = (
                ToolCallStatus.WAITING_APPROVAL
                if requires_approval and not auto_authorized
                else ToolCallStatus.CREATED
            )
            try:
                connection.execute(
                    """
                    INSERT INTO tool_calls(
                        turn_id, id, conversation_id, response_variant_id,
                        tool_name, arguments_json, effect, approval_mode, status,
                        created_at, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        call.turn_id,
                        call.id,
                        call.conversation_id,
                        call.response_variant_id,
                        call.tool_name,
                        call.canonical_arguments_json,
                        definition.effect.value,
                        definition.approval_mode.value,
                        status.value,
                        call.created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise InvalidStateError("Tool call was already recorded") from error

            if approval_prompt is None:
                return None, None

            approval_id = f"approval_{uuid.uuid4().hex}"
            connection.execute(
                """
                INSERT INTO approval_requests(
                    id, turn_id, tool_call_id, summary, reason, status,
                    metadata_json, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)
                """,
                (
                    approval_id,
                    call.turn_id,
                    call.id,
                    approval_prompt.summary,
                    approval_prompt.reason,
                    json.dumps(
                        dict(approval_prompt.metadata),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    call.created_at,
                ),
            )
            approval = ApprovalRequest(
                id=approval_id,
                tool_call_id=call.id,
                summary=approval_prompt.summary,
                reason=approval_prompt.reason,
                status=ApprovalStatus.PENDING,
                created_at=call.created_at,
                metadata=approval_prompt.metadata,
            )
            event = self._append_event(
                connection,
                row=row,
                event_type="approval.requested",
                occurred_at=call.created_at,
                data=self._approval_event_data(approval),
                include_message=False,
            )
            return approval, event

    def resolve_approval(
        self,
        approval_id: str,
        status: ApprovalStatus,
    ) -> tuple[ApprovalRequest, Optional[RuntimeEvent]]:
        if status not in (
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
            ApprovalStatus.CANCELLED,
            ApprovalStatus.EXPIRED,
        ):
            raise ValidationError("Approval must resolve to a terminal status")

        with self._database.transaction() as connection:
            approval_row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise NotFoundError("Approval request not found")
            current = ApprovalStatus(approval_row["status"])
            if current is not ApprovalStatus.PENDING:
                if current is status:
                    return self._approval_from_row(approval_row), None
                raise InvalidStateError("Approval request was already resolved")

            row = self._active_execution(
                connection,
                approval_row["turn_id"],
                self._variant_id_for_approval(connection, approval_id),
            )
            self._require_running(row)
            now = self._clock()
            tool_status = (
                ToolCallStatus.WAITING_APPROVAL
                if status is ApprovalStatus.APPROVED
                else ToolCallStatus.CANCELLED
            )
            connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, resolved_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status.value, now, approval_id),
            )
            connection.execute(
                """
                UPDATE tool_calls
                SET status = ?,
                    finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END
                WHERE turn_id = ? AND id = ?
                """,
                (
                    tool_status.value,
                    tool_status.value,
                    now,
                    approval_row["turn_id"],
                    approval_row["tool_call_id"],
                ),
            )
            resolved_row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?",
                (approval_id,),
            ).fetchone()
            approval = self._approval_from_row(resolved_row)
            event = self._append_event(
                connection,
                row=row,
                event_type="approval.resolved",
                occurred_at=now,
                data=self._approval_event_data(approval),
                include_message=False,
            )
            return approval, event

    def get_pending_approval(self, turn_id: str) -> Optional[ApprovalRequest]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE turn_id = ? AND status = 'pending'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
            return self._approval_from_row(row) if row is not None else None

    def start_tool_call(
        self,
        call: ToolCall,
        activity: ToolActivityCopy,
    ) -> RuntimeEvent:
        with self._database.transaction() as connection:
            row = self._active_execution(
                connection,
                call.turn_id,
                call.response_variant_id,
            )
            self._require_running(row)
            current = connection.execute(
                "SELECT status FROM tool_calls WHERE turn_id = ? AND id = ?",
                (call.turn_id, call.id),
            ).fetchone()
            if current is None:
                raise NotFoundError("Tool call not found")
            if current["status"] not in ("created", "waiting_approval"):
                raise InvalidStateError("Tool call is not ready to run")
            now = self._clock()
            connection.execute(
                """
                UPDATE tool_calls SET status = 'running', started_at = ?
                WHERE turn_id = ? AND id = ?
                """,
                (now, call.turn_id, call.id),
            )
            return self._append_event(
                connection,
                row=row,
                event_type="activity.started",
                occurred_at=now,
                data=self._activity_event_data(call.id, "running", activity.running),
                include_message=False,
            )

    def complete_tool_call(
        self,
        call: ToolCall,
        *,
        result_truncated: bool,
        activity: ToolActivityCopy,
    ) -> RuntimeEvent:
        return self._finish_tool_call(
            call,
            status=ToolCallStatus.COMPLETED,
            result_truncated=result_truncated,
            activity_message=activity.completed,
        )

    def fail_tool_call(
        self,
        call: ToolCall,
        *,
        error_code: str,
        activity: ToolActivityCopy,
    ) -> RuntimeEvent:
        return self._finish_tool_call(
            call,
            status=ToolCallStatus.FAILED,
            error_code=error_code,
            activity_message=activity.failed,
        )

    def cancel_tool_call(
        self,
        call: ToolCall,
        *,
        activity: ToolActivityCopy,
    ) -> RuntimeEvent:
        return self._finish_tool_call(
            call,
            status=ToolCallStatus.CANCELLED,
            activity_message=activity.cancelled,
        )

    def list_tool_calls(self, turn_id: str) -> tuple:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, tool_name, arguments_json, status "
                "FROM tool_calls WHERE turn_id = ? ORDER BY rowid",
                (turn_id,),
            ).fetchall()
        return tuple(
            ToolCallJournal(
                id=row["id"],
                tool_name=row["tool_name"],
                arguments=json.loads(row["arguments_json"]),
                status=row["status"],
            )
            for row in rows
        )

    def list_events(
        self,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> Sequence[RuntimeEvent]:
        with self._database.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM turns WHERE id = ?", (turn_id,)
            ).fetchone()
            if not exists:
                raise NotFoundError("Turn not found")
            rows = connection.execute(
                """
                SELECT payload_json FROM runtime_events
                WHERE turn_id = ? AND sequence > ? ORDER BY sequence
                """,
                (turn_id, after_sequence),
            ).fetchall()
            return tuple(self._event_from_json(row["payload_json"]) for row in rows)

    def recover_interrupted(self) -> Sequence[RuntimeEvent]:
        recovered: list[RuntimeEvent] = []
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT
                    t.id AS turn_id,
                    t.conversation_id,
                    t.status AS turn_status,
                    rv.id AS variant_id,
                    rv.status AS variant_status,
                    rv.assistant_message_id,
                    rv.variant_index,
                    rv.operation,
                    m.content
                FROM turns t
                JOIN response_variants rv ON rv.id = t.active_response_variant_id
                JOIN messages m ON m.id = rv.assistant_message_id
                WHERE t.status IN ('created', 'running')
                ORDER BY t.created_at, t.id
                """
            ).fetchall()
            for row in rows:
                now = self._clock()
                running_tools = connection.execute(
                    """
                    SELECT id FROM tool_calls
                    WHERE turn_id = ? AND status = 'running'
                    ORDER BY created_at, id
                    """,
                    (row["turn_id"],),
                ).fetchall()
                connection.execute(
                    """
                    UPDATE approval_requests
                    SET status = 'cancelled', resolved_at = ?
                    WHERE turn_id = ? AND status = 'pending'
                    """,
                    (now, row["turn_id"]),
                )
                connection.execute(
                    """
                    UPDATE tool_calls
                    SET status = 'cancelled', finished_at = ?
                    WHERE turn_id = ?
                      AND status IN ('created', 'waiting_approval', 'running')
                    """,
                    (now, row["turn_id"]),
                )
                for tool_row in running_tools:
                    self._append_event(
                        connection,
                        row=row,
                        event_type="activity.cancelled",
                        occurred_at=now,
                        data=self._activity_event_data(
                            tool_row["id"],
                            "cancelled",
                            "操作因本地服务中断而停止",
                        ),
                        include_message=False,
                    )
                self._write_terminal_state(
                    connection,
                    row=row,
                    status="failed",
                    content=row["content"],
                    finish_reason="error",
                    error_code="runtime_interrupted",
                    occurred_at=now,
                )
                error = {
                    "code": "runtime_interrupted",
                    "message": "本地服务中断了本次生成，可以重试。",
                    "retryable": True,
                    "correlationId": f"recovery_{uuid.uuid4().hex}",
                }
                data: dict[str, object] = {"error": error}
                if row["content"]:
                    data["partialContent"] = row["content"]
                recovered.append(
                    self._append_event(
                        connection,
                        row=row,
                        event_type="turn.failed",
                        occurred_at=now,
                        data=data,
                    )
                )
        return tuple(recovered)

    def _finish_tool_call(
        self,
        call: ToolCall,
        *,
        status: ToolCallStatus,
        result_truncated: bool = False,
        error_code: Optional[str] = None,
        activity_message: str,
    ) -> RuntimeEvent:
        if status not in (
            ToolCallStatus.COMPLETED,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
        ):
            raise ValidationError("Tool call must finish with a terminal status")
        with self._database.transaction() as connection:
            row = self._active_execution(
                connection,
                call.turn_id,
                call.response_variant_id,
            )
            current = connection.execute(
                "SELECT status FROM tool_calls WHERE turn_id = ? AND id = ?",
                (call.turn_id, call.id),
            ).fetchone()
            if current is None:
                raise NotFoundError("Tool call not found")
            if current["status"] in ("completed", "failed", "cancelled"):
                raise InvalidStateError("Tool call is already finished")
            now = self._clock()
            connection.execute(
                """
                UPDATE tool_calls
                SET status = ?, result_truncated = ?, error_code = ?, finished_at = ?
                WHERE turn_id = ? AND id = ?
                """,
                (
                    status.value,
                    int(result_truncated),
                    error_code,
                    now,
                    call.turn_id,
                    call.id,
                ),
            )
            event_type = {
                ToolCallStatus.COMPLETED: "activity.completed",
                ToolCallStatus.FAILED: "activity.failed",
                ToolCallStatus.CANCELLED: "activity.cancelled",
            }[status]
            return self._append_event(
                connection,
                row=row,
                event_type=event_type,
                occurred_at=now,
                data=self._activity_event_data(
                    call.id,
                    status.value,
                    activity_message,
                ),
                include_message=False,
            )

    @staticmethod
    def _variant_id_for_approval(
        connection: sqlite3.Connection,
        approval_id: str,
    ) -> str:
        row = connection.execute(
            """
            SELECT tc.response_variant_id
            FROM approval_requests ar
            JOIN tool_calls tc
              ON tc.turn_id = ar.turn_id AND tc.id = ar.tool_call_id
            WHERE ar.id = ?
            """,
            (approval_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("Approval tool call not found")
        return str(row["response_variant_id"])

    @staticmethod
    def _approval_from_row(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"],
            tool_call_id=row["tool_call_id"],
            summary=row["summary"],
            reason=row["reason"],
            status=ApprovalStatus(row["status"]),
            created_at=row["created_at"],
            resolved_at=row["resolved_at"],
            metadata=json.loads(row["metadata_json"]),
        )

    @staticmethod
    def _approval_event_data(approval: ApprovalRequest) -> dict[str, object]:
        result: dict[str, object] = {
            "approvalId": approval.id,
            "toolCallId": approval.tool_call_id,
            "summary": approval.summary,
            "reason": approval.reason,
            "status": approval.status.value,
            "createdAt": approval.created_at,
            "metadata": dict(approval.metadata),
        }
        if approval.resolved_at is not None:
            result["resolvedAt"] = approval.resolved_at
        return result

    @staticmethod
    def _activity_event_data(
        activity_id: str,
        status: str,
        message: str,
    ) -> dict[str, object]:
        return {
            "activityId": activity_id,
            "status": status,
            "message": message,
        }

    def _active_execution(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        variant_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT
                t.id AS turn_id,
                t.conversation_id,
                t.active_response_variant_id,
                t.status AS turn_status,
                rv.id AS variant_id,
                rv.status AS variant_status,
                rv.assistant_message_id,
                rv.variant_index,
                rv.operation,
                m.content
            FROM turns t
            JOIN response_variants rv ON rv.id = ? AND rv.turn_id = t.id
            JOIN messages m ON m.id = rv.assistant_message_id
            WHERE t.id = ?
            """,
            (variant_id, turn_id),
        ).fetchone()
        if not row:
            raise NotFoundError("Turn or response variant not found")
        if row["active_response_variant_id"] != variant_id:
            raise InvalidStateError("Response variant is not active")
        if row["turn_status"] != row["variant_status"]:
            raise InvalidStateError("Turn and active response variant states differ")
        return row

    @staticmethod
    def _require_running(row: sqlite3.Row) -> None:
        if row["turn_status"] != "running" or row["variant_status"] != "running":
            raise InvalidStateError("Response is not running")

    @staticmethod
    def _write_terminal_state(
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        status: str,
        content: str,
        finish_reason: str,
        error_code: Optional[str],
        occurred_at: str,
    ) -> None:
        connection.execute(
            "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
            (content, occurred_at, row["assistant_message_id"]),
        )
        connection.execute(
            """
            UPDATE response_variants
            SET status = ?, finish_reason = ?, error_code = ?, finished_at = ?
            WHERE id = ?
            """,
            (status, finish_reason, error_code, occurred_at, row["variant_id"]),
        )
        connection.execute(
            "UPDATE turns SET status = ?, finished_at = ? WHERE id = ?",
            (status, occurred_at, row["turn_id"]),
        )
        connection.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (occurred_at, row["conversation_id"]),
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        row: sqlite3.Row,
        event_type: str,
        occurred_at: str,
        data: Mapping[str, object],
        include_message: bool = True,
    ) -> RuntimeEvent:
        sequence_row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
            FROM runtime_events WHERE turn_id = ?
            """,
            (row["turn_id"],),
        ).fetchone()
        sequence = sequence_row["next_sequence"]
        event_id = f"{row['turn_id']}:{sequence}"
        event = RuntimeEvent(
            version=1,
            event_id=event_id,
            sequence=sequence,
            type=event_type,
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            response_variant_id=row["variant_id"],
            message_id=row["assistant_message_id"] if include_message else None,
            occurred_at=occurred_at,
            data=dict(data),
        )
        connection.execute(
            """
            INSERT INTO runtime_events(
                event_id, turn_id, sequence, event_type, payload_json, occurred_at,
                response_variant_id, message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.turn_id,
                event.sequence,
                event.type,
                self._event_to_json(event),
                event.occurred_at,
                event.response_variant_id,
                event.message_id,
            ),
        )
        return event

    @staticmethod
    def _usage_data(
        input_tokens: Optional[int],
        output_tokens: Optional[int],
    ) -> dict[str, object]:
        data: dict[str, object] = {}
        if input_tokens is not None:
            data["inputTokens"] = input_tokens
        if output_tokens is not None:
            data["outputTokens"] = output_tokens
        return data

    @staticmethod
    def _event_to_json(event: RuntimeEvent) -> str:
        payload: dict[str, object] = {
            "version": event.version,
            "eventId": event.event_id,
            "sequence": event.sequence,
            "type": event.type,
            "conversationId": event.conversation_id,
            "turnId": event.turn_id,
            "occurredAt": event.occurred_at,
            "data": dict(event.data),
        }
        if event.response_variant_id is not None:
            payload["responseVariantId"] = event.response_variant_id
        if event.message_id is not None:
            payload["messageId"] = event.message_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _event_from_json(payload_json: str) -> RuntimeEvent:
        payload = json.loads(payload_json)
        return RuntimeEvent(
            version=payload["version"],
            event_id=payload["eventId"],
            sequence=payload["sequence"],
            type=payload["type"],
            conversation_id=payload["conversationId"],
            turn_id=payload["turnId"],
            response_variant_id=payload.get("responseVariantId"),
            message_id=payload.get("messageId"),
            occurred_at=payload["occurredAt"],
            data=payload["data"],
        )

    def _last_event_of_type(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        event_type: str,
    ) -> Optional[RuntimeEvent]:
        row = connection.execute(
            """
            SELECT payload_json FROM runtime_events
            WHERE turn_id = ? AND event_type = ?
            ORDER BY sequence DESC LIMIT 1
            """,
            (turn_id, event_type),
        ).fetchone()
        return self._event_from_json(row["payload_json"]) if row else None
