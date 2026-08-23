from __future__ import annotations

from typing import Optional, Protocol, Sequence, Tuple

from .models import (
    Conversation,
    ConversationSnapshot,
    ConversationStatus,
    FinishReason,
    MemoryKind,
    MemoryProposal,
    MemoryRecord,
    MemoryStatus,
    ResponseVariantCommandResult,
    ResponseVariantOperation,
    TurnSnapshot,
)


class RepositoryError(RuntimeError):
    """Base error with a stable code for application-layer mapping."""

    code = "repository_error"


class NotFoundError(RepositoryError):
    code = "not_found"


class ConflictError(RepositoryError):
    code = "conflict"


class InvalidStateError(RepositoryError):
    code = "invalid_state"


class ValidationError(RepositoryError):
    code = "validation_error"


class ChatRepository(Protocol):
    def create_conversation(self) -> Conversation:
        ...

    def create_or_reuse_empty_conversation(self) -> Conversation:
        ...

    def get_conversation(self, conversation_id: str) -> Conversation:
        ...

    def list_conversations(
        self,
        *,
        status: ConversationStatus = ConversationStatus.ACTIVE,
        title_query: Optional[str] = None,
    ) -> Sequence[Conversation]:
        ...

    def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        ...

    def set_conversation_status(
        self,
        conversation_id: str,
        status: ConversationStatus,
    ) -> Conversation:
        ...

    def delete_conversation(self, conversation_id: str) -> None:
        ...

    def create_turn(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        content: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> TurnSnapshot:
        ...

    def get_turn(self, turn_id: str) -> TurnSnapshot:
        ...

    def get_conversation_snapshot(self, conversation_id: str) -> ConversationSnapshot:
        ...

    def create_response_variant(
        self,
        *,
        turn_id: str,
        command_request_id: str,
        operation: ResponseVariantOperation,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> ResponseVariantCommandResult:
        ...

    def mark_response_running(self, *, turn_id: str, variant_id: str) -> TurnSnapshot:
        ...

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
        ...

    def fail_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        error_code: str,
    ) -> TurnSnapshot:
        ...

    def cancel_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
    ) -> TurnSnapshot:
        ...

    def select_response_variant(self, *, turn_id: str, variant_id: str) -> TurnSnapshot:
        ...


class MemoryRepository(Protocol):
    def create_memory(
        self,
        *,
        kind: MemoryKind,
        content: str,
        source_conversation_id: str,
        source_turn_id: str,
    ) -> MemoryRecord:
        ...

    def get_memory(self, memory_id: str) -> MemoryRecord:
        ...

    def list_memories(
        self, *, include_deleted: bool = False
    ) -> Sequence[MemoryRecord]:
        ...

    def update_memory_content(self, memory_id: str, content: str) -> MemoryRecord:
        ...

    def delete_memory(self, memory_id: str) -> MemoryRecord:
        ...


class MemoryProposalRepository(Protocol):
    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        kind: MemoryKind,
        content: str,
        reason: str,
    ) -> MemoryProposal:
        ...

    def get_proposal(self, proposal_id: str) -> MemoryProposal:
        ...

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[MemoryProposal]:
        ...

    def find_pending_by_content(self, content: str) -> Optional[MemoryProposal]:
        ...

    def accept_proposal(
        self, proposal_id: str
    ) -> Tuple[MemoryProposal, "MemoryRecord"]:
        ...

    def reject_proposal(self, proposal_id: str) -> MemoryProposal:
        ...
