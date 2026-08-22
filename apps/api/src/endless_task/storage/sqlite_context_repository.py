from __future__ import annotations

import json
from typing import Callable, Optional, Sequence

from endless_task.runtime.context import (
    ContextSnapshot,
    ConversationSummaryRevision,
    IncludedTurn,
)

from .database import Database
from .sqlite_chat_repository import IdFactory, new_id, utc_now


Clock = Callable[[], str]


class SqliteContextRepository:
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

    def get_summary_revision(
        self,
        *,
        conversation_id: str,
        through_turn_ordinal: int,
        prompt_version: str,
    ) -> Optional[ConversationSummaryRevision]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM conversation_summary_revisions
                WHERE conversation_id = ? AND through_turn_ordinal = ?
                  AND prompt_version = ?
                """,
                (conversation_id, through_turn_ordinal, prompt_version),
            ).fetchone()
        return self._summary_from_row(row) if row is not None else None

    def save_summary_revision(
        self,
        *,
        conversation_id: str,
        through_turn_ordinal: int,
        prompt_version: str,
        content: str,
        input_token_estimate: int,
    ) -> ConversationSummaryRevision:
        with self._database.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM conversation_summary_revisions
                WHERE conversation_id = ? AND through_turn_ordinal = ?
                  AND prompt_version = ?
                """,
                (conversation_id, through_turn_ordinal, prompt_version),
            ).fetchone()
            if existing is not None:
                return self._summary_from_row(existing)

            summary_id = self._id_factory("summary")
            connection.execute(
                """
                INSERT INTO conversation_summary_revisions(
                    id, conversation_id, through_turn_ordinal, prompt_version,
                    content, input_token_estimate, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary_id,
                    conversation_id,
                    through_turn_ordinal,
                    prompt_version,
                    content,
                    input_token_estimate,
                    self._clock(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM conversation_summary_revisions WHERE id = ?",
                (summary_id,),
            ).fetchone()
        return self._summary_from_row(row)

    def save_context_snapshot(
        self,
        *,
        turn_id: str,
        response_variant_id: str,
        system_prompt_version: str,
        summary_revision_id: Optional[str],
        included_turns: Sequence[IncludedTurn],
        input_token_estimate: int,
        reserved_output_tokens: int,
    ) -> ContextSnapshot:
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM context_snapshots WHERE response_variant_id = ?",
                (response_variant_id,),
            ).fetchone()
            if existing is not None:
                return self._snapshot_from_row(existing)

            snapshot_id = self._id_factory("context")
            included_json = json.dumps(
                [
                    {
                        "turnOrdinal": item.turn_ordinal,
                        "responseVariantId": item.response_variant_id,
                    }
                    for item in included_turns
                ],
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO context_snapshots(
                    id, turn_id, response_variant_id, system_prompt_version,
                    summary_revision_id, included_turns_json,
                    input_token_estimate, reserved_output_tokens, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    turn_id,
                    response_variant_id,
                    system_prompt_version,
                    summary_revision_id,
                    included_json,
                    input_token_estimate,
                    reserved_output_tokens,
                    self._clock(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM context_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
        return self._snapshot_from_row(row)

    def get_context_snapshot(self, response_variant_id: str) -> Optional[ContextSnapshot]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM context_snapshots WHERE response_variant_id = ?",
                (response_variant_id,),
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    @staticmethod
    def _summary_from_row(row) -> ConversationSummaryRevision:
        return ConversationSummaryRevision(
            id=row["id"],
            conversation_id=row["conversation_id"],
            through_turn_ordinal=row["through_turn_ordinal"],
            prompt_version=row["prompt_version"],
            content=row["content"],
            input_token_estimate=row["input_token_estimate"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _snapshot_from_row(row) -> ContextSnapshot:
        included = tuple(
            IncludedTurn(
                turn_ordinal=item["turnOrdinal"],
                response_variant_id=item["responseVariantId"],
            )
            for item in json.loads(row["included_turns_json"])
        )
        return ContextSnapshot(
            id=row["id"],
            turn_id=row["turn_id"],
            response_variant_id=row["response_variant_id"],
            system_prompt_version=row["system_prompt_version"],
            summary_revision_id=row["summary_revision_id"],
            included_turns=included,
            input_token_estimate=row["input_token_estimate"],
            reserved_output_tokens=row["reserved_output_tokens"],
            created_at=row["created_at"],
        )
