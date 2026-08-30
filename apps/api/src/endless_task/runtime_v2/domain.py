from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class LaneKind(str, Enum):
    MAIN = "main"
    PERSISTENT_BRANCH = "persistent_branch"
    TEMPORARY = "temporary"  # legacy lane kind；v1.1 仅用于独立临时 Conversation 的 root lane
    ARCHIVED = "archived"  # legacy compat；新归档写入 LaneStatus.ARCHIVED


class LaneStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MemoryScope(str, Enum):
    USER_GLOBAL = "user_global"
    WORKSPACE = "workspace"
    CONVERSATION_TREE = "conversation_tree"
    BRANCH = "branch"
    TEMPORARY = "temporary"
    RUN_SCRATCH = "run_scratch"


class MemoryPromotionStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class TranscriptEntryStatus(str, Enum):
    STREAMING = "streaming"
    FINAL = "final"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Actor(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    RUNTIME = "runtime"
    SYSTEM = "system"


class TranscriptEntryType(str, Enum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESULT = "approval_result"
    SYSTEM_NOTICE = "system_notice"
    ERROR = "error"
    CONTEXT_SUMMARY = "context_summary"
    ARTIFACT_REF = "artifact_ref"
    TASK_REF = "task_ref"
    MEMORY_PROPOSAL_REF = "memory_proposal_ref"
    PLAN = "plan"


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPACTING = "compacting"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class ModelTurnStatus(str, Enum):
    CREATED = "created"
    PROJECTING_CONTEXT = "projecting_context"
    WAITING_PROVIDER_SLOT = "waiting_provider_slot"
    STREAMING = "streaming"
    EXECUTING_TOOLS = "executing_tools"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ToolExecutionStatus(str, Enum):
    CREATED = "created"
    VALIDATING = "validating"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CrashRecoveryClassification(str, Enum):
    RECOVERABLE = "recoverable"
    NEEDS_USER_ACTION = "needs_user_action"
    NON_RECOVERABLE = "non_recoverable"


class CrashRecoveryAction(str, Enum):
    RESUME = "resume"
    AWAIT_USER_DECISION = "await_user_decision"
    FINALIZE_CANCELLATION = "finalize_cancellation"
    MARK_FAILED = "mark_failed"


class CrashRecoveryReason(str, Enum):
    INTERRUPTED_RUN = "interrupted_run"
    INTERRUPTED_MODEL_TURN = "interrupted_model_turn"
    CANCELLATION_PENDING = "cancellation_pending"
    WAITING_APPROVAL = "waiting_approval"
    TOOL_SIDE_EFFECT_UNCERTAIN = "tool_side_effect_uncertain"
    TOOL_RESULT_MISSING = "tool_result_missing"
    STATE_EVENT_CONFLICT = "state_event_conflict"
    JOURNAL_SEQUENCE_CORRUPT = "journal_sequence_corrupt"


@dataclass(frozen=True)
class LaneRecord:
    id: str
    conversation_id: str
    kind: LaneKind
    created_at: str
    base_entry_id: Optional[str] = None
    leaf_entry_id: Optional[str] = None
    status: LaneStatus = LaneStatus.ACTIVE
    archived_at: Optional[str] = None
    display_name: Optional[str] = None
    summary: Optional[str] = None
    source_lane_id: Optional[str] = None
    created_from_entry_id: Optional[str] = None
    metadata: dict[str, Any] | None = None

    @property
    def is_archived(self) -> bool:
        return self.status is LaneStatus.ARCHIVED


@dataclass(frozen=True)
class TemporaryConversationRecord:
    conversation_id: str
    source_conversation_id: Optional[str]
    source_lane_id: Optional[str]
    source_base_entry_id: Optional[str]
    source_leaf_entry_id: Optional[str]
    snapshot_entry_ids: tuple[str, ...]
    created_at: str
    promoted_at: Optional[str] = None


@dataclass(frozen=True)
class LaneEventRecord:
    event_id: str
    conversation_id: str
    lane_id: str
    event_seq: int
    event_type: str
    occurred_at: str
    data: dict[str, Any]


@dataclass(frozen=True)
class LanePromotionRecord:
    promoted_lane: LaneRecord
    previous_main_lane: LaneRecord
    pointer: ConversationPointer


@dataclass(frozen=True)
class RuntimeV2MemoryRecord:
    id: str
    scope: MemoryScope
    kind: str
    content: str
    status: str
    conversation_id: str
    workspace_id: Optional[str] = None
    lane_id: Optional[str] = None
    run_id: Optional[str] = None
    source_memory_id: Optional[str] = None
    source_entry_id: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class RuntimeV2MemoryPromotion:
    id: str
    source_memory_id: str
    target_scope: MemoryScope
    target_workspace_id: Optional[str]
    target_lane_id: Optional[str]
    status: MemoryPromotionStatus
    created_at: str
    updated_at: str
    resolved_memory_id: Optional[str] = None
    conflict_memory_id: Optional[str] = None
    resolved_at: Optional[str] = None


@dataclass(frozen=True)
class TranscriptEntryRecord:
    id: str
    conversation_id: str
    parent_id: Optional[str]
    lane_id: str
    seq: int
    type: TranscriptEntryType
    type_version: int
    actor: Actor
    status: TranscriptEntryStatus
    created_at: str
    updated_at: str
    payload: dict[str, Any]
    context_policy: dict[str, Any]
    display: dict[str, Any]
    source_run_id: Optional[str] = None


@dataclass(frozen=True)
class RunRecord:
    id: str
    conversation_id: str
    lane_id: str
    trigger_entry_id: str
    sibling_group_id: str
    status: RunStatus
    created_at: str
    assistant_entry_id: Optional[str] = None
    is_active_variant: bool = False
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancelled_by: Optional[str] = None
    error_code: Optional[str] = None
    safe_message: Optional[str] = None
    correlation_id: Optional[str] = None


@dataclass(frozen=True)
class RuntimeV2MessageSubmission:
    lane: LaneRecord
    user_entry: TranscriptEntryRecord
    run: RunRecord
    created: bool


@dataclass(frozen=True)
class ModelTurnRecord:
    id: str
    run_id: str
    turn_index: int
    status: ModelTurnStatus
    created_at: str
    provider: Optional[str] = None
    model: Optional[str] = None
    request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    safe_message: Optional[str] = None


@dataclass(frozen=True)
class ToolExecutionRecord:
    id: str
    model_turn_id: str
    call_id: str
    tool_name: str
    arguments_hash: str
    arguments: dict[str, Any]
    status: ToolExecutionStatus
    created_at: str
    approval_id: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error_code: Optional[str] = None
    safe_message: Optional[str] = None
    result_entry_id: Optional[str] = None


@dataclass(frozen=True)
class RuntimeEventRecord:
    event_id: str
    run_id: str
    model_turn_id: Optional[str]
    event_seq: int
    event_type: str
    occurred_at: str
    correlation_id: Optional[str]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ProductRuntimeEventRecord:
    id: str
    conversation_id: str
    event_seq: int
    event_type: str
    run_id: Optional[str]
    lane_id: Optional[str]
    source_event_id: str
    occurred_at: str
    data: dict[str, Any]


@dataclass(frozen=True)
class ConversationPointer:
    conversation_id: str
    active_lane_id: str
    active_run_id: Optional[str] = None
    active_run_variant_id: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass(frozen=True)
class ContextCompactionRecord:
    id: str
    conversation_id: str
    lane_id: str
    base_entry_id: str
    summary_entry_id: str
    covered_entry_ids: tuple[str, ...]
    tokens_before: int
    tokens_after: int
    created_at: str
