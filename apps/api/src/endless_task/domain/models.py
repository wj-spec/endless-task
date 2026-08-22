from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class ConversationStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


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
