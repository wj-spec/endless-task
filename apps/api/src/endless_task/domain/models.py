from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .task_schedule import TaskSchedule


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ConversationKind(str, Enum):
    NORMAL = "normal"
    EPHEMERAL = "ephemeral"


class TurnStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ResponseVariantOperation(str, Enum):
    CREATE = "create"
    RETRY = "retry"
    REGENERATE = "regenerate"


class ResponseVariantStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FinishReason(str, Enum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Conversation:
    id: str
    title: str
    status: ConversationStatus
    next_turn_ordinal: int
    title_is_manual: bool
    created_at: str
    updated_at: str
    archived_at: Optional[str] = None
    parent_conversation_id: Optional[str] = None
    fork_turn_id: Optional[str] = None
    kind: ConversationKind = ConversationKind.NORMAL
    promoted_at: Optional[str] = None


@dataclass(frozen=True)
class Turn:
    id: str
    conversation_id: str
    ordinal: int
    user_message_id: str
    active_response_variant_id: Optional[str]
    status: TurnStatus
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    turn_id: str
    role: MessageRole
    content: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ResponseVariant:
    id: str
    turn_id: str
    assistant_message_id: str
    index: int
    operation: ResponseVariantOperation
    status: ResponseVariantStatus
    provider: Optional[str]
    model: Optional[str]
    finish_reason: Optional[FinishReason]
    error_code: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


@dataclass(frozen=True)
class ResponseVariantSnapshot:
    variant: ResponseVariant
    assistant_message: Message


@dataclass(frozen=True)
class TurnSnapshot:
    turn: Turn
    user_message: Message
    response_variants: Tuple[ResponseVariantSnapshot, ...]


@dataclass(frozen=True)
class ResponseVariantCommandResult:
    turn_snapshot: TurnSnapshot
    response_variant_id: str


@dataclass(frozen=True)
class ConversationSnapshot:
    conversation: Conversation
    turns: Tuple[TurnSnapshot, ...]


class MemoryKind(str, Enum):
    PREFERENCE = "preference"
    FACT = "fact"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DELETED = "deleted"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    kind: MemoryKind
    content: str
    status: MemoryStatus
    source_conversation_id: str
    source_turn_id: str
    write_origin: str
    created_at: str
    updated_at: str
    expired_at: Optional[str] = None
    deleted_at: Optional[str] = None
    source_proposal_id: Optional[str] = None
    expired_reason: Optional[str] = None
    superseded_by: Optional[str] = None


class MemoryProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class MemoryProposal:
    id: str
    conversation_id: str
    turn_id: str
    kind: MemoryKind
    content: str
    reason: str
    status: MemoryProposalStatus
    created_at: str
    updated_at: str
    resolved_memory_id: Optional[str] = None
    resolved_at: Optional[str] = None


class PermissionMode(str, Enum):
    CONFIRM_EVERY_TIME = "confirm_every_time"
    TRUST_LOCAL_WRITES = "trust_local_writes"
    TRUST_ALL = "trust_all"

    @property
    def rank(self) -> int:
        return {
            PermissionMode.CONFIRM_EVERY_TIME: 0,
            PermissionMode.TRUST_LOCAL_WRITES: 1,
            PermissionMode.TRUST_ALL: 2,
        }[self]

    def is_escalation_from(self, other: "PermissionMode") -> bool:
        return self.rank > other.rank

    def covers(self, effect: str) -> bool:
        if effect == "read_only":
            return True
        if effect == "local_write":
            return self.rank >= 1
        if effect == "external_action":
            return self.rank >= 2
        return False


class ArtifactKind(str, Enum):
    MARKDOWN = "markdown"
    TEXT = "text"


class ArtifactStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


class ArtifactVersionOperation(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    CHAT_CONTINUE = "chat_continue"
    ROLLBACK = "rollback"


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    title: str
    kind: ArtifactKind
    status: ArtifactStatus
    current_version_ordinal: int
    created_at: str
    updated_at: str
    deleted_at: Optional[str] = None


@dataclass(frozen=True)
class ArtifactVersionRecord:
    id: str
    artifact_id: str
    ordinal: int
    content: str
    operation: ArtifactVersionOperation
    source_conversation_id: str
    source_turn_id: str
    source_labels: Tuple[str, ...]
    note: Optional[str]
    created_at: str


@dataclass(frozen=True)
class ArtifactSnapshot:
    artifact: ArtifactRecord
    current_version: ArtifactVersionRecord


class ArtifactProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ArtifactProposal:
    id: str
    conversation_id: str
    turn_id: str
    title: str
    kind: ArtifactKind
    content: str
    reason: str
    status: ArtifactProposalStatus
    created_at: str
    updated_at: str
    source_labels: Tuple[str, ...] = ()
    target_artifact_id: Optional[str] = None
    base_version_ordinal: Optional[int] = None
    resolved_artifact_id: Optional[str] = None
    resolved_at: Optional[str] = None


class TaskStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    commitment: str
    schedule: "TaskSchedule"
    status: TaskStatus
    source_conversation_id: str
    source_turn_id: str
    source_proposal_id: Optional[str]
    created_at: str
    updated_at: str
    cancelled_at: Optional[str] = None
    resumed_at: Optional[str] = None


class TaskProposalStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskProposal:
    id: str
    conversation_id: str
    turn_id: str
    title: str
    commitment: str
    schedule: "TaskSchedule"
    reason: str
    status: TaskProposalStatus
    created_at: str
    updated_at: str
    resolved_task_id: Optional[str] = None
    resolved_at: Optional[str] = None


class TaskRunTrigger(str, Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class ReminderStatus(str, Enum):
    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"


class NotificationKind(str, Enum):
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_AWAITING = "run_awaiting"


class TaskRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskRun:
    id: str
    task_id: str
    trigger: TaskRunTrigger
    status: TaskRunStatus
    conversation_id: str
    turn_id: Optional[str]
    error: Optional[str]
    started_at: str
    finished_at: Optional[str] = None
    awaiting_user: bool = False
    awaiting_note: Optional[str] = None
    attempt: int = 1
    retryable: bool = False


@dataclass(frozen=True)
class Reminder:
    id: str
    title: str
    commitment: str
    due_at: str
    status: ReminderStatus
    source_conversation_id: str
    source_turn_id: str
    source_proposal_id: Optional[str]
    created_at: str
    updated_at: str
    fired_at: Optional[str] = None
    cancelled_at: Optional[str] = None


@dataclass(frozen=True)
class Notification:
    id: str
    kind: NotificationKind
    task_id: str
    run_id: str
    conversation_id: str
    title: str
    body: str
    created_at: str
    read_at: Optional[str] = None


class KnowledgeSourceKind(str, Enum):
    FILE = "file"
    NOTE = "note"


class KnowledgeSourceOrigin(str, Enum):
    USER = "user"
    AGENT = "agent"


class KnowledgeSourceStatus(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    DELETED = "deleted"


class KnowledgeScope(str, Enum):
    SOURCE = "source"
    MEMORY = "memory"
    ARTIFACT = "artifact"
    CONVERSATION = "conversation"


@dataclass(frozen=True)
class KnowledgeSource:
    id: str
    kind: KnowledgeSourceKind
    origin: KnowledgeSourceOrigin
    title: str
    content: str
    status: KnowledgeSourceStatus
    file_name: Optional[str] = None
    source_conversation_id: Optional[str] = None
    proposed_by_turn_id: Optional[str] = None
    user_edited_at: Optional[str] = None
    expires_at: Optional[str] = None
    expired_at: Optional[str] = None
    deleted_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class KnowledgeHit:
    scope: KnowledgeScope
    ref_id: str
    title: str
    snippet: str
    origin: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class ToolCallJournal:
    id: str
    tool_name: str
    arguments: dict
    status: str
