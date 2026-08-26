from __future__ import annotations

from typing import Optional, Protocol, Sequence, Tuple

from .models import (
    ArtifactKind,
    ArtifactProposal,
    ArtifactProposalStatus,
    ArtifactRecord,
    ToolCallJournal,
    ArtifactSnapshot,
    ArtifactVersionOperation,
    ArtifactVersionRecord,
    Conversation,
    ConversationKind,
    ConversationSnapshot,
    ConversationStatus,
    FinishReason,
    KnowledgeSource,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
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
        kind: Optional[ConversationKind] = None,
    ) -> Sequence[Conversation]:
        ...

    def create_branch(
        self,
        *,
        parent_conversation_id: str,
        fork_turn_id: Optional[str] = None,
        kind: ConversationKind = ConversationKind.EPHEMERAL,
        title: Optional[str] = None,
    ) -> Conversation:
        ...

    def promote_conversation(self, conversation_id: str) -> Conversation:
        ...

    def list_branches(self, conversation_id: str) -> Sequence[Conversation]:
        ...

    def list_lineage_turns(self, conversation_id: str) -> Sequence[TurnSnapshot]:
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


class KnowledgeRepository(Protocol):
    def create_source(
        self,
        *,
        kind: KnowledgeSourceKind,
        origin: KnowledgeSourceOrigin,
        title: str,
        content: str,
        file_name: Optional[str] = None,
        source_conversation_id: Optional[str] = None,
        proposed_by_turn_id: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> KnowledgeSource:
        ...

    def get_source(self, source_id: str) -> KnowledgeSource:
        ...

    def list_sources(
        self, status: KnowledgeSourceStatus = KnowledgeSourceStatus.ACTIVE
    ) -> Sequence[KnowledgeSource]:
        ...

    def update_source(
        self,
        source_id: str,
        *,
        title: Optional[str] = None,
        content: Optional[str] = None,
        file_name: Optional[str] = None,
        expires_at: Optional[str] = None,
        by_user: bool = True,
    ) -> KnowledgeSource:
        ...

    def expire_source(self, source_id: str) -> KnowledgeSource:
        ...

    def restore_source(self, source_id: str) -> KnowledgeSource:
        ...

    def delete_source(self, source_id: str) -> KnowledgeSource:
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


class ArtifactRepository(Protocol):
    def create_artifact(
        self,
        *,
        title: str,
        kind: ArtifactKind,
        content: str,
        source_conversation_id: str,
        source_turn_id: str,
        source_labels: Sequence[str] = (),
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        ...

    def append_version(
        self,
        *,
        artifact_id: str,
        content: str,
        operation: ArtifactVersionOperation,
        source_conversation_id: str,
        source_turn_id: str,
        source_labels: Sequence[str] = (),
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        ...

    def rollback_to_version(
        self,
        *,
        artifact_id: str,
        target_ordinal: int,
        source_conversation_id: str,
        source_turn_id: str,
        note: Optional[str] = None,
    ) -> ArtifactSnapshot:
        ...

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        ...

    def get_current_version(self, artifact_id: str) -> ArtifactVersionRecord:
        ...

    def get_version(self, artifact_id: str, ordinal: int) -> ArtifactVersionRecord:
        ...

    def list_versions(self, artifact_id: str) -> Sequence[ArtifactVersionRecord]:
        ...

    def list_artifacts(
        self, *, include_deleted: bool = False
    ) -> Sequence[ArtifactRecord]:
        ...

    def delete_artifact(self, artifact_id: str) -> ArtifactRecord:
        ...


class ArtifactProposalRepository(Protocol):
    def create_proposal(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        title: str,
        kind: ArtifactKind,
        content: str,
        reason: str,
        source_labels: Sequence[str] = (),
        target_artifact_id: Optional[str] = None,
    ) -> ArtifactProposal:
        ...

    def get_proposal(self, proposal_id: str) -> ArtifactProposal:
        ...

    def list_proposals(
        self, *, conversation_id: str, include_resolved: bool = False
    ) -> Sequence[ArtifactProposal]:
        ...

    def find_pending_by_content(self, content: str) -> Optional[ArtifactProposal]:
        ...

    def accept_proposal(
        self, proposal_id: str
    ) -> Tuple[ArtifactProposal, ArtifactRecord]:
        ...

    def reject_proposal(self, proposal_id: str) -> ArtifactProposal:
        ...

