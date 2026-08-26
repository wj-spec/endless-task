from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from endless_task.domain.models import (
    Conversation,
    ConversationKind,
    ConversationSnapshot,
    ConversationStatus,
    FinishReason,
    Message,
    MessageRole,
    ResponseVariant,
    ResponseVariantCommandResult,
    ResponseVariantOperation,
    ResponseVariantSnapshot,
    ResponseVariantStatus,
    Turn,
    TurnSnapshot,
    TurnStatus,
)
from endless_task.domain.repositories import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ValidationError,
)

from .database import Database

Clock = Callable[[], str]
IdFactory = Callable[[str], str]

MAX_BRANCH_DEPTH = 8


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class SqliteChatRepository:
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

    def create_conversation(self) -> Conversation:
        conversation_id = self._id_factory("conv")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO conversations(
                    id, title, status, next_turn_ordinal, title_is_manual,
                    created_at, updated_at, archived_at
                ) VALUES (?, '新对话', 'active', 1, 0, ?, ?, NULL)
                """,
                (conversation_id, now, now),
            )
            return self._get_conversation(connection, conversation_id)

    def create_or_reuse_empty_conversation(self) -> Conversation:
        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT c.*
                FROM conversations c
                WHERE c.status = 'active'
                  AND c.kind = 'normal'
                  AND NOT EXISTS (
                      SELECT 1 FROM turns t WHERE t.conversation_id = c.id
                  )
                ORDER BY c.updated_at DESC, c.id DESC
                LIMIT 1
                """
            ).fetchone()
            if existing:
                return self._conversation_from_row(existing)

            conversation_id = self._id_factory("conv")
            now = self._clock()
            connection.execute(
                """
                INSERT INTO conversations(
                    id, title, status, next_turn_ordinal, title_is_manual,
                    created_at, updated_at, archived_at
                ) VALUES (?, '新对话', 'active', 1, 0, ?, ?, NULL)
                """,
                (conversation_id, now, now),
            )
            return self._get_conversation(connection, conversation_id)

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._database.connect() as connection:
            return self._get_conversation(connection, conversation_id)

    def list_conversations(
        self,
        *,
        status: ConversationStatus = ConversationStatus.ACTIVE,
        title_query: Optional[str] = None,
        kind: Optional[ConversationKind] = None,
    ) -> Sequence[Conversation]:
        sql = "SELECT * FROM conversations WHERE status = ?"
        params: list[object] = [status.value]
        if title_query and title_query.strip():
            sql += " AND instr(lower(title), lower(?)) > 0"
            params.append(title_query.strip())
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind.value)
        sql += " ORDER BY updated_at DESC, id DESC"

        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            return tuple(self._conversation_from_row(row) for row in rows)

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        normalized = " ".join(title.split())
        if not normalized:
            raise ValidationError("Conversation title cannot be empty")

        with self._database.transaction() as connection:
            self._get_conversation(connection, conversation_id)
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, title_is_manual = 1, updated_at = ?
                WHERE id = ?
                """,
                (normalized, self._clock(), conversation_id),
            )
            return self._get_conversation(connection, conversation_id)

    def set_conversation_status(
        self,
        conversation_id: str,
        status: ConversationStatus,
    ) -> Conversation:
        now = self._clock()
        with self._database.transaction() as connection:
            self._get_conversation(connection, conversation_id)
            active = connection.execute(
                """
                SELECT 1 FROM turns
                WHERE conversation_id = ? AND status IN ('created', 'running')
                """,
                (conversation_id,),
            ).fetchone()
            if status is ConversationStatus.ARCHIVED and active:
                raise InvalidStateError("Stop the active turn before archiving")

            connection.execute(
                """
                UPDATE conversations
                SET status = ?, archived_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    now if status is ConversationStatus.ARCHIVED else None,
                    now,
                    conversation_id,
                ),
            )
            conversation = self._get_conversation(connection, conversation_id)
        return conversation

    def delete_conversation(self, conversation_id: str) -> None:
        with self._database.transaction() as connection:
            self._get_conversation(connection, conversation_id)
            active = connection.execute(
                """
                WITH RECURSIVE subtree(id) AS (
                    SELECT id FROM conversations WHERE id = ?
                    UNION ALL
                    SELECT c.id FROM conversations c
                    JOIN subtree s ON c.parent_conversation_id = s.id
                )
                SELECT 1 FROM turns
                WHERE conversation_id IN (SELECT id FROM subtree)
                  AND status IN ('created', 'running')
                """,
                (conversation_id,),
            ).fetchone()
            if active:
                raise InvalidStateError("Stop the active turn before deleting the conversation")
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def create_branch(
        self,
        *,
        parent_conversation_id: str,
        fork_turn_id: Optional[str] = None,
        kind: ConversationKind = ConversationKind.EPHEMERAL,
        title: Optional[str] = None,
    ) -> Conversation:
        with self._database.transaction() as connection:
            parent = self._get_conversation(connection, parent_conversation_id)
            depth = self._branch_depth(connection, parent_conversation_id)
            if depth >= MAX_BRANCH_DEPTH:
                raise InvalidStateError("Branch nesting is too deep")

            if fork_turn_id is None:
                fork_row = connection.execute(
                    "SELECT * FROM turns WHERE conversation_id = ? ORDER BY ordinal DESC LIMIT 1",
                    (parent_conversation_id,),
                ).fetchone()
                if fork_row is None:
                    raise InvalidStateError("Cannot branch a conversation without turns")
            else:
                fork_row = connection.execute(
                    "SELECT * FROM turns WHERE id = ? AND conversation_id = ?",
                    (fork_turn_id, parent_conversation_id),
                ).fetchone()
                if fork_row is None:
                    raise NotFoundError("Fork turn not found in parent conversation")

            branch_id = self._id_factory("conv")
            now = self._clock()
            normalized = " ".join((title or "").split())
            branch_title = normalized or f"《{parent.title}》· 分支"
            connection.execute(
                """
                INSERT INTO conversations(
                    id, title, status, next_turn_ordinal, title_is_manual,
                    created_at, updated_at, archived_at,
                    parent_conversation_id, fork_turn_id, kind
                ) VALUES (?, ?, 'active', 1, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    branch_id,
                    branch_title,
                    1 if normalized else 0,
                    now,
                    now,
                    parent_conversation_id,
                    fork_row["id"],
                    kind.value,
                ),
            )
            return self._get_conversation(connection, branch_id)

    def promote_conversation(self, conversation_id: str) -> Conversation:
        with self._database.transaction() as connection:
            conversation = self._get_conversation(connection, conversation_id)
            if conversation.kind is ConversationKind.NORMAL:
                raise ConflictError("Only ephemeral conversations can be promoted")
            connection.execute(
                """
                UPDATE conversations
                SET kind = ?, promoted_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (ConversationKind.NORMAL.value, self._clock(), self._clock(), conversation_id),
            )
            promoted = self._get_conversation(connection, conversation_id)
        return promoted

    def list_branches(self, conversation_id: str) -> Sequence[Conversation]:
        with self._database.connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                SELECT * FROM conversations
                WHERE parent_conversation_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (conversation_id,),
            ).fetchall()
            return tuple(self._conversation_from_row(row) for row in rows)

    def list_lineage_turns(self, conversation_id: str) -> Sequence[TurnSnapshot]:
        with self._database.connect() as connection:
            hops: list[tuple[str, str]] = []
            current_id = conversation_id
            for _ in range(MAX_BRANCH_DEPTH + 1):
                conversation = self._get_conversation(connection, current_id)
                if conversation.parent_conversation_id is None:
                    break
                if conversation.fork_turn_id is None:
                    raise InvalidStateError("Branch is missing its fork turn")
                hops.append((conversation.parent_conversation_id, conversation.fork_turn_id))
                current_id = conversation.parent_conversation_id
            else:
                raise InvalidStateError("Branch nesting is too deep")

            lineage: list[TurnSnapshot] = []
            for ancestor_id, fork_turn_id in reversed(hops):
                fork_turn = self._get_turn(connection, fork_turn_id)
                if fork_turn.conversation_id != ancestor_id:
                    raise InvalidStateError("Fork turn does not belong to its ancestor")
                rows = connection.execute(
                    """
                    SELECT id FROM turns
                    WHERE conversation_id = ? AND ordinal <= ?
                    ORDER BY ordinal
                    """,
                    (ancestor_id, fork_turn.ordinal),
                ).fetchall()
                lineage.extend(
                    self._get_turn_snapshot(connection, row["id"]) for row in rows
                )
            return tuple(lineage)

    def list_descendant_ids(self, conversation_id: str) -> Sequence[str]:
        with self._database.connect() as connection:
            self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                """
                WITH RECURSIVE subtree(id) AS (
                    SELECT c.id FROM conversations c
                    WHERE c.parent_conversation_id = ?
                    UNION ALL
                    SELECT c.id FROM conversations c
                    JOIN subtree s ON c.parent_conversation_id = s.id
                )
                SELECT id FROM subtree
                """,
                (conversation_id,),
            ).fetchall()
            return tuple(row["id"] for row in rows)

    def _branch_depth(self, connection: sqlite3.Connection, conversation_id: str) -> int:
        depth = 0
        current_id = conversation_id
        for _ in range(MAX_BRANCH_DEPTH + 1):
            row = connection.execute(
                "SELECT parent_conversation_id FROM conversations WHERE id = ?",
                (current_id,),
            ).fetchone()
            if row is None or row["parent_conversation_id"] is None:
                return depth
            depth += 1
            current_id = row["parent_conversation_id"]
        raise InvalidStateError("Branch nesting is too deep")

    def create_turn(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        content: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> TurnSnapshot:
        if not client_request_id.strip():
            raise ValidationError("client_request_id cannot be empty")
        if not content.strip():
            raise ValidationError("Message content cannot be empty")

        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT turn_id FROM client_requests
                WHERE conversation_id = ? AND client_request_id = ?
                """,
                (conversation_id, client_request_id),
            ).fetchone()
            if existing:
                return self._get_turn_snapshot(connection, existing["turn_id"])

            conversation = self._get_conversation(connection, conversation_id)
            if conversation.status is ConversationStatus.ARCHIVED:
                raise InvalidStateError("Cannot add a turn to an archived conversation")

            active = connection.execute(
                """
                SELECT id FROM turns
                WHERE conversation_id = ? AND status IN ('created', 'running')
                """,
                (conversation_id,),
            ).fetchone()
            if active:
                raise ConflictError("Conversation already has an active turn")

            now = self._clock()
            turn_id = self._id_factory("turn")
            user_message_id = self._id_factory("msg")
            assistant_message_id = self._id_factory("msg")
            variant_id = self._id_factory("variant")

            connection.execute(
                """
                INSERT INTO turns(
                    id, conversation_id, ordinal, user_message_id,
                    active_response_variant_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'created', ?)
                """,
                (
                    turn_id,
                    conversation_id,
                    conversation.next_turn_ordinal,
                    user_message_id,
                    variant_id,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO messages(
                    id, conversation_id, turn_id, role, content, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        user_message_id,
                        conversation_id,
                        turn_id,
                        MessageRole.USER.value,
                        content,
                        now,
                        now,
                    ),
                    (
                        assistant_message_id,
                        conversation_id,
                        turn_id,
                        MessageRole.ASSISTANT.value,
                        "",
                        now,
                        now,
                    ),
                ),
            )
            connection.execute(
                """
                INSERT INTO response_variants(
                    id, turn_id, assistant_message_id, variant_index, operation,
                    status, provider, model, created_at
                ) VALUES (?, ?, ?, 1, 'create', 'created', ?, ?, ?)
                """,
                (variant_id, turn_id, assistant_message_id, provider, model, now),
            )
            connection.execute(
                """
                INSERT INTO client_requests(
                    conversation_id, client_request_id, turn_id, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (conversation_id, client_request_id, turn_id, now),
            )

            title = conversation.title
            if not conversation.title_is_manual and conversation.next_turn_ordinal == 1:
                title = self._automatic_title(content)
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, next_turn_ordinal = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    title,
                    conversation.next_turn_ordinal + 1,
                    now,
                    conversation_id,
                ),
            )
            return self._get_turn_snapshot(connection, turn_id)

    def get_turn(self, turn_id: str) -> TurnSnapshot:
        with self._database.connect() as connection:
            return self._get_turn_snapshot(connection, turn_id)

    def get_conversation_snapshot(self, conversation_id: str) -> ConversationSnapshot:
        with self._database.connect() as connection:
            conversation = self._get_conversation(connection, conversation_id)
            rows = connection.execute(
                "SELECT id FROM turns WHERE conversation_id = ? ORDER BY ordinal",
                (conversation_id,),
            ).fetchall()
            return ConversationSnapshot(
                conversation=conversation,
                turns=tuple(self._get_turn_snapshot(connection, row["id"]) for row in rows),
            )

    def create_response_variant(
        self,
        *,
        turn_id: str,
        command_request_id: str,
        operation: ResponseVariantOperation,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ResponseVariantCommandResult:
        if operation not in (
            ResponseVariantOperation.RETRY,
            ResponseVariantOperation.REGENERATE,
        ):
            raise ValidationError("Only retry or regenerate can create another response variant")
        if not command_request_id.strip():
            raise ValidationError("command_request_id cannot be empty")

        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT command_type, response_variant_id
                FROM command_requests
                WHERE turn_id = ? AND command_request_id = ?
                """,
                (turn_id, command_request_id),
            ).fetchone()
            if existing:
                if existing["command_type"] != operation.value:
                    raise ConflictError("command_request_id was used for another operation")
                return ResponseVariantCommandResult(
                    turn_snapshot=self._get_turn_snapshot(connection, turn_id),
                    response_variant_id=existing["response_variant_id"],
                )

            turn = self._get_turn(connection, turn_id)
            latest = connection.execute(
                "SELECT MAX(ordinal) AS ordinal FROM turns WHERE conversation_id = ?",
                (turn.conversation_id,),
            ).fetchone()
            if latest["ordinal"] != turn.ordinal:
                raise InvalidStateError("Only the latest turn can create another response variant")

            if operation is ResponseVariantOperation.RETRY and turn.status not in (
                TurnStatus.FAILED,
                TurnStatus.CANCELLED,
            ):
                raise InvalidStateError("Retry requires a failed or cancelled turn")
            if (
                operation is ResponseVariantOperation.REGENERATE
                and turn.status is not TurnStatus.COMPLETED
            ):
                raise InvalidStateError("Regenerate requires a completed turn")

            next_index_row = connection.execute(
                """
                SELECT COALESCE(MAX(variant_index), 0) + 1 AS next_index
                FROM response_variants WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            next_index = next_index_row["next_index"]
            now = self._clock()
            variant_id = self._id_factory("variant")
            assistant_message_id = self._id_factory("msg")

            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, turn_id, role, content, created_at, updated_at
                ) VALUES (?, ?, ?, 'assistant', '', ?, ?)
                """,
                (assistant_message_id, turn.conversation_id, turn_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO response_variants(
                    id, turn_id, assistant_message_id, variant_index, operation,
                    status, provider, model, created_at
                ) VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?)
                """,
                (
                    variant_id,
                    turn_id,
                    assistant_message_id,
                    next_index,
                    operation.value,
                    provider,
                    model,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE turns
                SET active_response_variant_id = ?, status = 'created',
                    started_at = NULL, finished_at = NULL
                WHERE id = ?
                """,
                (variant_id, turn_id),
            )
            connection.execute(
                """
                INSERT INTO command_requests(
                    turn_id, command_request_id, command_type,
                    response_variant_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (turn_id, command_request_id, operation.value, variant_id, now),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, turn.conversation_id),
            )
            return ResponseVariantCommandResult(
                turn_snapshot=self._get_turn_snapshot(connection, turn_id),
                response_variant_id=variant_id,
            )

    def mark_response_running(
        self,
        *,
        turn_id: str,
        variant_id: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> TurnSnapshot:
        now = self._clock()
        with self._database.transaction() as connection:
            turn, variant = self._get_active_pair(connection, turn_id, variant_id)
            if turn.status is not TurnStatus.CREATED or variant.status is not ResponseVariantStatus.CREATED:
                raise InvalidStateError("Response must be created before it can run")
            connection.execute(
                """
                UPDATE response_variants
                SET status = 'running', provider = COALESCE(?, provider),
                    model = COALESCE(?, model), started_at = ?
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
            return self._get_turn_snapshot(connection, turn_id)

    def complete_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        content: str,
        finish_reason: FinishReason,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> TurnSnapshot:
        if finish_reason not in (
            FinishReason.STOP,
            FinishReason.LENGTH,
            FinishReason.CONTENT_FILTER,
        ):
            raise ValidationError("Completed responses require a successful finish reason")
        return self._finish_response(
            turn_id=turn_id,
            variant_id=variant_id,
            content=content,
            status=ResponseVariantStatus.COMPLETED,
            finish_reason=finish_reason,
            error_code=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def fail_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        error_code: str,
    ) -> TurnSnapshot:
        if not error_code.strip():
            raise ValidationError("error_code cannot be empty")
        return self._finish_response(
            turn_id=turn_id,
            variant_id=variant_id,
            content=partial_content,
            status=ResponseVariantStatus.FAILED,
            finish_reason=FinishReason.ERROR,
            error_code=error_code,
        )

    def cancel_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
    ) -> TurnSnapshot:
        return self._finish_response(
            turn_id=turn_id,
            variant_id=variant_id,
            content=partial_content,
            status=ResponseVariantStatus.CANCELLED,
            finish_reason=FinishReason.CANCELLED,
            error_code=None,
        )

    def select_response_variant(self, *, turn_id: str, variant_id: str) -> TurnSnapshot:
        with self._database.transaction() as connection:
            turn = self._get_turn(connection, turn_id)
            latest = connection.execute(
                "SELECT MAX(ordinal) AS ordinal FROM turns WHERE conversation_id = ?",
                (turn.conversation_id,),
            ).fetchone()
            if latest["ordinal"] != turn.ordinal:
                raise InvalidStateError("Response selection is frozen for historical turns")
            if turn.status in (TurnStatus.CREATED, TurnStatus.RUNNING):
                raise InvalidStateError("Cannot select a response while the turn is active")

            row = connection.execute(
                "SELECT * FROM response_variants WHERE id = ? AND turn_id = ?",
                (variant_id, turn_id),
            ).fetchone()
            if not row:
                raise NotFoundError("Response variant not found")
            variant = self._response_variant_from_row(row)
            if variant.status is not ResponseVariantStatus.COMPLETED:
                raise InvalidStateError("Only a completed response variant can be selected")

            now = self._clock()
            connection.execute(
                """
                UPDATE turns
                SET active_response_variant_id = ?, status = 'completed',
                    started_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (variant_id, variant.started_at, variant.finished_at, turn_id),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, turn.conversation_id),
            )
            return self._get_turn_snapshot(connection, turn_id)

    def _finish_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        content: str,
        status: ResponseVariantStatus,
        finish_reason: FinishReason,
        error_code: Optional[str],
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> TurnSnapshot:
        now = self._clock()
        with self._database.transaction() as connection:
            turn, variant = self._get_active_pair(connection, turn_id, variant_id)
            allowed = (
                (TurnStatus.RUNNING, ResponseVariantStatus.RUNNING)
                if status is ResponseVariantStatus.COMPLETED
                else (
                    (TurnStatus.CREATED, ResponseVariantStatus.CREATED),
                    (TurnStatus.RUNNING, ResponseVariantStatus.RUNNING),
                )
            )
            current = (turn.status, variant.status)
            if status is ResponseVariantStatus.COMPLETED:
                if current != allowed:
                    raise InvalidStateError("Only a running response can complete")
            elif current not in allowed:
                raise InvalidStateError("Only a created or running response can stop")

            connection.execute(
                "UPDATE messages SET content = ?, updated_at = ? WHERE id = ?",
                (content, now, variant.assistant_message_id),
            )
            connection.execute(
                """
                UPDATE response_variants
                SET status = ?, finish_reason = ?, error_code = ?,
                    input_tokens = ?, output_tokens = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    finish_reason.value,
                    error_code,
                    input_tokens,
                    output_tokens,
                    now,
                    variant_id,
                ),
            )
            connection.execute(
                "UPDATE turns SET status = ?, finished_at = ? WHERE id = ?",
                (status.value, now, turn_id),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, turn.conversation_id),
            )
            return self._get_turn_snapshot(connection, turn_id)

    def _get_active_pair(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
        variant_id: str,
    ) -> tuple[Turn, ResponseVariant]:
        turn = self._get_turn(connection, turn_id)
        if turn.active_response_variant_id != variant_id:
            raise InvalidStateError("Response variant is not active")
        row = connection.execute(
            "SELECT * FROM response_variants WHERE id = ? AND turn_id = ?",
            (variant_id, turn_id),
        ).fetchone()
        if not row:
            raise NotFoundError("Response variant not found")
        return turn, self._response_variant_from_row(row)

    def _get_conversation(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> Conversation:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not row:
            raise NotFoundError("Conversation not found")
        return self._conversation_from_row(row)

    def _get_turn(self, connection: sqlite3.Connection, turn_id: str) -> Turn:
        row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if not row:
            raise NotFoundError("Turn not found")
        return self._turn_from_row(row)

    def _get_turn_snapshot(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> TurnSnapshot:
        turn = self._get_turn(connection, turn_id)
        user_row = connection.execute(
            "SELECT * FROM messages WHERE id = ?",
            (turn.user_message_id,),
        ).fetchone()
        if not user_row:
            raise NotFoundError("Turn user message not found")

        variant_rows = connection.execute(
            """
            SELECT * FROM response_variants
            WHERE turn_id = ? ORDER BY variant_index
            """,
            (turn_id,),
        ).fetchall()
        response_variants = []
        for row in variant_rows:
            variant = self._response_variant_from_row(row)
            message_row = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (variant.assistant_message_id,),
            ).fetchone()
            if not message_row:
                raise NotFoundError("Response variant message not found")
            response_variants.append(
                ResponseVariantSnapshot(
                    variant=variant,
                    assistant_message=self._message_from_row(message_row),
                )
            )

        return TurnSnapshot(
            turn=turn,
            user_message=self._message_from_row(user_row),
            response_variants=tuple(response_variants),
        )

    @staticmethod
    def _automatic_title(content: str) -> str:
        normalized = " ".join(content.split())
        return normalized[:30] or "新对话"

    @staticmethod
    def _conversation_from_row(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            title=row["title"],
            status=ConversationStatus(row["status"]),
            next_turn_ordinal=row["next_turn_ordinal"],
            title_is_manual=bool(row["title_is_manual"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
            parent_conversation_id=row["parent_conversation_id"],
            fork_turn_id=row["fork_turn_id"],
            kind=ConversationKind(row["kind"]),
            promoted_at=row["promoted_at"],
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> Turn:
        return Turn(
            id=row["id"],
            conversation_id=row["conversation_id"],
            ordinal=row["ordinal"],
            user_message_id=row["user_message_id"],
            active_response_variant_id=row["active_response_variant_id"],
            status=TurnStatus(row["status"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            conversation_id=row["conversation_id"],
            turn_id=row["turn_id"],
            role=MessageRole(row["role"]),
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _response_variant_from_row(row: sqlite3.Row) -> ResponseVariant:
        return ResponseVariant(
            id=row["id"],
            turn_id=row["turn_id"],
            assistant_message_id=row["assistant_message_id"],
            index=row["variant_index"],
            operation=ResponseVariantOperation(row["operation"]),
            status=ResponseVariantStatus(row["status"]),
            provider=row["provider"],
            model=row["model"],
            finish_reason=FinishReason(row["finish_reason"]) if row["finish_reason"] else None,
            error_code=row["error_code"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
