from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from fastapi import FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from endless_task.domain.task_schedule import ReminderDue
from endless_task.domain.models import (
    ConversationKind,
    ConversationSnapshot,
    ConversationStatus,
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeProposalType,
    KnowledgeSourceStatus,
    PermissionMode,
    RetrievalEventKind,
    TurnStatus,
)
from endless_task.domain.models import TaskRunTrigger
from endless_task.domain.repositories import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    RepositoryError,
    ValidationError,
)
from endless_task.files import FileError
from endless_task.files.read_tool import ReadTextFileTool
from endless_task.runtime import (
    AssistantRuntime,
    FakeProvider,
    KnowledgeQueryRewriter,
    KnowledgeReranker,
    OpenAICompatibleProvider,
    P0ContextBuilder,
    RuntimeConfiguration,
    RuntimeEvent,
    RuntimeEventBroker,
    TurnController,
    UnconfiguredProvider,
)
from endless_task.runtime.provider import ModelProvider, ProviderMessage
from endless_task.runtime.provider_manager import ProviderManager
from endless_task.runtime_v2 import (
    LaneKind,
    LaneRecord,
    MemoryScope,
    ProductRuntimeEventRecord,
    RunRecord,
    RunStatus,
    RuntimeV2ConversationRuntimeStatus,
    RuntimeV2GlobalRuntimeStatus,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
    RuntimeV2RuntimeSelectionService,
    RuntimeV2SessionGateway,
    ToolApprovalDecision,
    product_event_json,
)
from endless_task.artifacts import (
    ArtifactProposalService,
    SourceReferenceResolver,
)
from endless_task.artifacts.export_service import (
    ExportError,
    build_export,
    content_disposition_header,
    export_filename,
)
from endless_task.artifacts.read_tool import ReadArtifactTool
from endless_task.workspace_runtime import (
    ReadSkillFileTool,
    DeleteWorkspaceFileTool,
    EffectLog,
    ListWorkspaceDirTool,
    ReadWorkspaceFileTool,
    RunShellTool,
    WorkspaceResolver,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.artifact_store import ArtifactFileStore
from endless_task.workspace_runtime.browse import browse_directory
from endless_task.security import configure_safe_logging
from endless_task.knowledge import (
    CitationFeedbackProvider,
    EmbeddingIndexer,
    KnowledgeLifecycleService,
    KnowledgeProposalService,
    build_embedder,
)
from endless_task.knowledge.ingestion import IngestionError, ingest_file_bytes
from endless_task.mcp_runtime import McpManager
from endless_task.proposals.budget import ProposalBudget
from endless_task.memory import MemoryConflictService, MemoryProposalService
from endless_task.tasks import (
    TaskNotificationService,
    TaskProposalService,
    TaskRunReviewService,
    TaskScheduler,
    TaskWorker,
)
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
    SqliteChatRepository,
    SqliteContextRepository,
    SqliteMemoryProposalRepository,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
    SqliteMemoryRepository,
    SqlitePreferencesRepository,
    SqliteTextFileRepository,
    SqliteRuntimeRepository,
    SqliteRuntimeV2Repository,
    SqliteRuntimeV2MemoryRepository,
    SqliteTaskRepository,
    SqliteTaskProposalRepository,
    SqliteNotificationRepository,
    SqliteReminderRepository,
    SqliteRetrievalEventRepository,
    SqliteTaskRunRepository,
    SqliteWorkspaceRepository,
)
from endless_task.storage.sqlite_mcp_server_repository import (
    McpServerDraft,
    SqliteMcpServerRepository,
)
from endless_task.storage.sqlite_provider_profile_repository import (
    ProviderProfileDraft,
    SqliteProviderProfileRepository,
)
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.storage.sqlite_knowledge_repository import scope_tier
from endless_task.skills import Skill, SkillService, build_available_skills_prompt
from endless_task.tooling import ApprovalStatus, ToolRegistry

from .serialization import (
    artifact_json,
    artifact_proposal_json,
    artifact_version_json,
    memory_proposal_json,
    knowledge_proposal_json,
    knowledge_source_json,
    memory_record_json,
    workspace_json,
    approval_request_json,
    compact_turn_snapshot_json,
    conversation_json,
    conversation_snapshot_json,
    response_variant_by_id,
    runtime_event_json,
    turn_command_json,
    uploaded_text_file_json,
    task_json,
    task_proposal_json,
    notification_json,
    reminder_json,
    task_run_json,
)


logger = logging.getLogger(__name__)
TERMINAL_TURN_STATUSES = {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED}
CONFIG_VERSION = 1

ARTIFACT_AWARENESS_PROMPT_VERSION = "p3.1-v1"
ARTIFACT_AWARENESS_CLAUSE = (
    "\n- 当本轮回答属于值得整体保留的独立成文结果（长回答、计划、改写稿、报告、总结等）时，"
    "可以在回复末尾追加一句简短的陈述式预告，说明该结果可以保留为文档；"
    "不要用追问口吻推销，普通问答、闲聊或简短回答一律不要提及文档保留。"
    "\n- 用户要求修改已保存的文档结果时，先用 read_artifact 读取当前内容，"
    "再在回复中给出完整修改后的文档，不要只给片段或口头承诺。"
)

TASK_AWARENESS_PROMPT_VERSION = "p4.9-v1"
TASK_AWARENESS_CLAUSE = (
    "\n- 用户要求周期性做某事时（每天、每周、每月等），回答必须明确复述完整承诺"
    "（周期、时间、做什么），并说明该安排在用户确认后才生效；不得声称已经安排。"
    "\n- 用户提出一次性定时事项时，回答必须明确复述承诺与具体时间，"
    "并说明该提醒在用户确认后才生效；不得声称已经安排。"
    "\n- 安排是否生效，以用户在提案卡片上的确认为准；用户在聊天中的文字"
    "（如“确认”“生效”“可以”）不构成确认，不得声称安排已生效，"
    "可引导用户在卡片上确认。"
)


def default_data_directory() -> Path:
    configured = os.environ.get("ENDLESS_TASK_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Endless Task"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Endless Task"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return (
        Path(xdg_data_home).expanduser() / "endless-task"
        if xdg_data_home
        else Path.home() / ".local" / "share" / "endless-task"
    )


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def runtime_environment() -> dict[str, str]:
    """Merge the local .env file under real environment variables."""
    configured = os.environ.get("ENDLESS_TASK_ENV_FILE")
    candidates = (
        [Path(configured).expanduser()] if configured else [Path.cwd() / ".env"]
    )
    merged: dict[str, str] = {}
    for candidate in candidates:
        merged.update(_load_env_file(candidate))
    merged.update(os.environ)
    return merged


@dataclass(frozen=True)
class AppSettings:
    database_path: Path
    config_version: int = CONFIG_VERSION
    runtime: str = "v2"
    runtime_rollback: bool = False
    provider_name: str = "fake"
    model: str = "fake-model"
    base_url: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    provider_timeout_seconds: float = 60.0
    system_prompt: str = "你是 Endless Task，一个可靠、简洁的个人助手。\n- 能直接回答的问题用自然语言直接回答，不要调用工具。\n- 信息不足时先向用户追问关键信息，不要猜测。\n- 只有问题确实需要会话附件内容时才调用文件工具。\n- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。"
    system_prompt_version: str = "p1-v1"
    context_window_tokens: int = 32_768
    max_output_tokens: int = 2_048
    summary_token_limit: int = 1_024
    max_concurrent_model_calls: int = 2
    max_concurrent_task_runs: int = 1
    max_agent_iterations: int = 4
    max_tool_calls_per_turn: int = 8
    agent_timeout_seconds: float = 120.0
    approval_timeout_seconds: float = 1800.0
    max_message_characters: int = 100_000
    max_file_bytes: int = 1_000_000
    max_files_per_conversation: int = 10
    heartbeat_seconds: float = 15.0
    memory_proposals_enabled: bool = True
    memory_marker_gate_enabled: bool = True
    artifact_proposals_enabled: bool = True
    task_proposals_enabled: bool = True
    knowledge_proposals_enabled: bool = True
    knowledge_query_rewrite_enabled: bool = False
    knowledge_rerank_enabled: bool = False
    knowledge_scope_weights: dict = field(default_factory=dict)
    knowledge_synonym_map: dict = field(default_factory=dict)
    knowledge_duplicate_threshold: float = 0.5
    knowledge_decay_enabled: bool = True
    knowledge_decay_min_age_days: int = 30
    knowledge_decay_recheck_days: int = 90
    knowledge_decay_interval_hours: float = 24.0
    knowledge_feedback_enabled: bool = True
    embedding_enabled: bool = False
    embedding_backend: str = "local"
    embedding_model: Optional[str] = None
    embedding_local_repo: str = "Xenova/bge-small-zh-v1.5"
    embedding_local_url_base: str = "https://huggingface.co"
    embedding_max_chars: int = 1500
    embedding_batch_size: int = 8
    hybrid_literal_weight: float = 0.4
    hybrid_semantic_weight: float = 0.6
    proposal_daily_budget: int = 6
    proposal_cooldown_minutes: int = 30
    proposal_quiet_start: str = "23:00"
    proposal_quiet_end: str = "07:00"
    scheduler_enabled: bool = True
    scheduler_tick_seconds: float = 30.0
    task_run_review_enabled: bool = True
    notifications_enabled: bool = True
    task_max_attempts: int = 3
    task_retry_backoff_seconds: float = 60.0
    shell_timeout_seconds: float = 120.0
    shell_no_change_timeout_seconds: float = 60.0
    shell_max_output_bytes: int = 65_536
    workspace_max_write_bytes: int = 512_000

    def __post_init__(self) -> None:
        if self.config_version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}"
            )
        if self.runtime not in {"v1", "v2"}:
            raise ValueError("ENDLESS_TASK_RUNTIME must be v1 or v2")
        if self.provider_timeout_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("Timeout values must be positive")
        if self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("Context window must be larger than max output tokens")
        if self.summary_token_limit < 0:
            raise ValueError("Summary token limit cannot be negative")
        if self.max_concurrent_model_calls <= 0:
            raise ValueError("Maximum concurrent model calls must be positive")
        if self.max_agent_iterations <= 0 or self.max_tool_calls_per_turn <= 0:
            raise ValueError("Agent loop limits must be positive")
        if self.agent_timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        if self.approval_timeout_seconds <= 0:
            raise ValueError("Approval timeout must be positive")
        if self.scheduler_tick_seconds <= 0:
            raise ValueError("Scheduler tick must be positive")
        if self.embedding_max_chars <= 0 or self.embedding_batch_size <= 0:
            raise ValueError("Embedding limits must be positive")
        if self.hybrid_literal_weight < 0 or self.hybrid_semantic_weight < 0:
            raise ValueError("Hybrid weights cannot be negative")
        if not 0 < self.knowledge_duplicate_threshold <= 1:
            raise ValueError("Knowledge duplicate threshold must be in (0, 1]")
        if self.knowledge_decay_min_age_days < 1 or self.knowledge_decay_recheck_days < 1:
            raise ValueError("Knowledge decay windows must be positive")
        if self.knowledge_decay_interval_hours <= 0:
            raise ValueError("Knowledge decay interval must be positive")
        if self.task_max_attempts < 1:
            raise ValueError("Task max attempts must be positive")
        if self.task_retry_backoff_seconds <= 0:
            raise ValueError("Task retry backoff must be positive")
        if self.max_message_characters <= 0:
            raise ValueError("Maximum message characters must be positive")
        if self.max_file_bytes <= 0 or self.max_files_per_conversation <= 0:
            raise ValueError("File limits must be positive")

    @classmethod
    def from_environment(cls) -> "AppSettings":
        env = runtime_environment()
        configured = env.get("ENDLESS_TASK_DB_PATH")
        provider_name = env.get("ENDLESS_TASK_PROVIDER", "fake").strip().lower()
        defaults = {
            "fake": ("fake-model", None),
            "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
            "openai": ("gpt-4.1-mini", "https://api.openai.com/v1"),
            "openai-compatible": ("", None),
        }
        default_model, default_base_url = defaults.get(provider_name, ("", None))
        api_key = env.get("ENDLESS_TASK_API_KEY")
        if api_key is None and provider_name == "deepseek":
            api_key = env.get("DEEPSEEK_API_KEY")
        if api_key is None and provider_name == "openai":
            api_key = env.get("OPENAI_API_KEY")
        database_path = (
            Path(configured).expanduser().resolve()
            if configured
            else default_data_directory() / "endless-task.db"
        )
        return cls(
            database_path=database_path,
            runtime=env.get("ENDLESS_TASK_RUNTIME", "v2").strip().lower(),
            runtime_rollback=_parse_flag(
                env.get("ENDLESS_TASK_RUNTIME_ROLLBACK", "0")
            ),
            memory_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_PROPOSALS", "1")
            ),
            memory_marker_gate_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_MARKER_GATE", "1")
            ),
            artifact_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_ARTIFACT_PROPOSALS", "1")
            ),
            task_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_TASK_PROPOSALS", "1")
            ),
            knowledge_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_PROPOSALS", "1")
            ),
            knowledge_query_rewrite_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_QUERY_REWRITE", "0")
            ),
            knowledge_rerank_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_RERANK", "0")
            ),
            knowledge_scope_weights=_parse_json_mapping(
                env.get("ENDLESS_TASK_KNOWLEDGE_SCOPE_WEIGHTS", "")
            ),
            knowledge_synonym_map=_parse_json_mapping(
                env.get("ENDLESS_TASK_KNOWLEDGE_SYNONYMS", "")
            ),
            knowledge_duplicate_threshold=float(
                env.get("ENDLESS_TASK_KNOWLEDGE_DUPLICATE_THRESHOLD", "0.5")
            ),
            knowledge_decay_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY", "1")
            ),
            knowledge_decay_min_age_days=int(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_MIN_AGE_DAYS", "30")
            ),
            knowledge_decay_recheck_days=int(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_RECHECK_DAYS", "90")
            ),
            knowledge_decay_interval_hours=float(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_INTERVAL_HOURS", "24")
            ),
            knowledge_feedback_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_FEEDBACK", "1")
            ),
            embedding_enabled=_parse_flag(env.get("ENDLESS_TASK_EMBEDDING", "0")),
            embedding_backend=env.get(
                "ENDLESS_TASK_EMBEDDING_BACKEND", "local"
            ).strip().lower(),
            embedding_model=env.get("ENDLESS_TASK_EMBEDDING_MODEL", "").strip()
            or None,
            embedding_local_repo=env.get(
                "ENDLESS_TASK_EMBEDDING_LOCAL_REPO", "Xenova/bge-small-zh-v1.5"
            ).strip(),
            embedding_local_url_base=env.get(
                "ENDLESS_TASK_EMBEDDING_LOCAL_URL_BASE", "https://huggingface.co"
            ).strip(),
            embedding_max_chars=int(
                env.get("ENDLESS_TASK_EMBEDDING_MAX_CHARS", "1500")
            ),
            embedding_batch_size=int(env.get("ENDLESS_TASK_EMBEDDING_BATCH", "8")),
            hybrid_literal_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[0],
            hybrid_semantic_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[1],
            proposal_daily_budget=int(
                env.get("ENDLESS_TASK_PROPOSAL_DAILY_BUDGET", "6")
            ),
            proposal_cooldown_minutes=int(
                env.get("ENDLESS_TASK_PROPOSAL_COOLDOWN_MINUTES", "30")
            ),
            proposal_quiet_start=env.get(
                "ENDLESS_TASK_PROPOSAL_QUIET_START", "23:00"
            ).strip(),
            proposal_quiet_end=env.get(
                "ENDLESS_TASK_PROPOSAL_QUIET_END", "07:00"
            ).strip(),
            scheduler_enabled=_parse_flag(env.get("ENDLESS_TASK_SCHEDULER", "1")),
            task_run_review_enabled=_parse_flag(
                env.get("ENDLESS_TASK_RUN_REVIEW", "1")
            ),
            notifications_enabled=_parse_flag(
                env.get("ENDLESS_TASK_NOTIFICATIONS", "1")
            ),
            task_max_attempts=int(env.get("ENDLESS_TASK_TASK_MAX_ATTEMPTS", "3")),
            task_retry_backoff_seconds=float(
                env.get("ENDLESS_TASK_TASK_RETRY_BACKOFF", "60")
            ),
            shell_timeout_seconds=float(
                env.get("ENDLESS_TASK_SHELL_TIMEOUT_SECONDS", "120")
            ),
            shell_no_change_timeout_seconds=float(
                env.get("ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS", "60")
            ),
            shell_max_output_bytes=int(
                env.get("ENDLESS_TASK_SHELL_MAX_OUTPUT_BYTES", "65536")
            ),
            workspace_max_write_bytes=int(
                env.get("ENDLESS_TASK_WORKSPACE_MAX_WRITE_BYTES", "512000")
            ),
            scheduler_tick_seconds=float(
                env.get("ENDLESS_TASK_SCHEDULER_TICK", "30")
            ),
            config_version=int(env.get("ENDLESS_TASK_CONFIG_VERSION", "1")),
            provider_name=provider_name,
            model=env.get("ENDLESS_TASK_MODEL", default_model),
            base_url=env.get("ENDLESS_TASK_BASE_URL", default_base_url),
            api_key=api_key,
            provider_timeout_seconds=float(
                env.get("ENDLESS_TASK_PROVIDER_TIMEOUT_SECONDS", "60")
            ),
            system_prompt=env.get(
                "ENDLESS_TASK_SYSTEM_PROMPT",
                "你是 Endless Task，一个可靠、简洁的个人助手。\n- 能直接回答的问题用自然语言直接回答，不要调用工具。\n- 信息不足时先向用户追问关键信息，不要猜测。\n- 只有问题确实需要会话附件内容时才调用文件工具。\n- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。",
            ),
            system_prompt_version=env.get(
                "ENDLESS_TASK_SYSTEM_PROMPT_VERSION",
                "p1-v1",
            ),
            context_window_tokens=int(
                env.get("ENDLESS_TASK_CONTEXT_WINDOW_TOKENS", "32768")
            ),
            max_output_tokens=int(
                env.get("ENDLESS_TASK_MAX_OUTPUT_TOKENS", "2048")
            ),
            summary_token_limit=int(
                env.get("ENDLESS_TASK_SUMMARY_TOKEN_LIMIT", "1024")
            ),
            max_concurrent_model_calls=int(
                env.get("ENDLESS_TASK_MAX_CONCURRENT_MODEL_CALLS", "2")
            ),
            max_concurrent_task_runs=int(
                env.get("ENDLESS_TASK_MAX_TASK_RUNS", "1")
            ),
            max_agent_iterations=int(
                env.get("ENDLESS_TASK_MAX_AGENT_ITERATIONS", "4")
            ),
            max_tool_calls_per_turn=int(
                env.get("ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN", "8")
            ),
            agent_timeout_seconds=float(
                env.get("ENDLESS_TASK_AGENT_TIMEOUT_SECONDS", "120")
            ),
            approval_timeout_seconds=float(
                env.get("ENDLESS_TASK_APPROVAL_TIMEOUT_SECONDS", "1800")
            ),
            max_message_characters=int(
                env.get("ENDLESS_TASK_MAX_MESSAGE_CHARACTERS", "100000")
            ),
            max_file_bytes=int(
                env.get("ENDLESS_TASK_MAX_FILE_BYTES", "1000000")
            ),
            max_files_per_conversation=int(
                env.get("ENDLESS_TASK_MAX_FILES_PER_CONVERSATION", "10")
            ),
        )


@dataclass(frozen=True)
class AppContainer:
    settings: AppSettings
    database: Database
    chat_repository: SqliteChatRepository
    runtime_repository: SqliteRuntimeRepository
    file_repository: SqliteTextFileRepository
    memory_repository: SqliteMemoryRepository
    proposal_repository: SqliteMemoryProposalRepository
    preferences_repository: SqlitePreferencesRepository
    artifact_repository: SqliteArtifactRepository
    artifact_proposal_repository: SqliteArtifactProposalRepository
    artifact_proposal_service: Optional[ArtifactProposalService]
    task_repository: SqliteTaskRepository
    task_proposal_repository: SqliteTaskProposalRepository
    task_proposal_service: Optional[TaskProposalService]
    task_run_repository: SqliteTaskRunRepository
    notification_repository: SqliteNotificationRepository
    reminder_repository: SqliteReminderRepository
    knowledge_proposal_repository: SqliteKnowledgeProposalRepository
    knowledge_proposal_service: Optional[KnowledgeProposalService]
    knowledge_repository: SqliteKnowledgeRepository
    retrieval_event_repository: SqliteRetrievalEventRepository
    workspace_repository: SqliteWorkspaceRepository
    workspace_resolver: WorkspaceResolver
    skill_service: SkillService
    provider_profile_repository: SqliteProviderProfileRepository
    provider_manager: ProviderManager
    mcp_server_repository: SqliteMcpServerRepository
    mcp_manager: McpManager
    effect_log: EffectLog
    artifact_file_store: ArtifactFileStore
    knowledge_lifecycle_service: Optional[KnowledgeLifecycleService]
    embedding_indexer: Optional[EmbeddingIndexer]
    proposal_budget: Optional[ProposalBudget]
    task_notification_service: Optional[TaskNotificationService]
    task_worker: TaskWorker
    task_scheduler: TaskScheduler
    reference_resolver: SourceReferenceResolver
    memory_proposal_service: Optional[MemoryProposalService]
    memory_conflict_service: Optional[MemoryConflictService]
    broker: RuntimeEventBroker
    provider: ModelProvider
    runtime: AssistantRuntime
    tool_registry: ToolRegistry
    controller: TurnController
    runtime_v2_repository: SqliteRuntimeV2Repository
    runtime_v2_memory_repository: SqliteRuntimeV2MemoryRepository
    runtime_v2_gateway: RuntimeV2SessionGateway
    runtime_v2_selection_service: RuntimeV2RuntimeSelectionService


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    status: Optional[ConversationStatus] = None
    providerProfileId: Optional[str] = None
    modelOverride: Optional[str] = None


class CreateBranchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forkTurnId: Optional[str] = None


class CreateTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class RuntimeV2MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    laneId: Optional[str] = None


class RuntimeV2ConversationRuntimeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime: Literal["v1", "v2"]


class RuntimeV2CreateLaneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["persistent_branch"] = "persistent_branch"
    sourceLaneId: Optional[str] = None
    baseEntryId: Optional[str] = None
    displayName: Optional[str] = None


class RuntimeV2RenameLaneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    displayName: Optional[str] = None


class RuntimeV2CreateTemporaryConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sourceLaneId: str
    sourceLeafEntryId: Optional[str] = None
    title: Optional[str] = None


class RuntimeV2MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    laneId: Optional[str] = None
    sourceEntryId: Optional[str] = None


class RuntimeV2RunMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    sourceEntryId: Optional[str] = None


class RuntimeV2MemoryPromotionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetScope: Literal[
        "user_global", "workspace", "conversation_tree", "branch"
    ]
    targetLaneId: Optional[str] = None


class RuntimeV2MemoryPromotionResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class RuntimeV2SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class RuntimeV2RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["mark_failed", "retry"]


class UpdateMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class ResolveMemoryProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveKnowledgeProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
    workspaceId: Optional[str] = None


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rootPath: Optional[str] = None


class WorkspacePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rootPath: Optional[str] = None


class CreateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaceId: Optional[str] = None


class RollbackArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetOrdinal: int
    sourceConversationId: str
    sourceTurnId: str
    note: Optional[str] = None


class ProviderProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    defaultModel: str
    baseUrl: str = ""
    apiKeyRef: str = ""
    timeoutSeconds: float = 60.0
    enabled: bool = True


class ProviderProfilePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    defaultModel: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKeyRef: Optional[str] = None
    timeoutSeconds: Optional[float] = None
    enabled: Optional[bool] = None


class ProviderDefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profileId: str


class SkillPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: bool


class McpServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = {}
    enabled: bool = True
    toolCallTimeoutSeconds: float = 60.0


class McpServerPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    transport: Optional[Literal["stdio", "http"]] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None
    toolCallTimeoutSeconds: Optional[float] = None


class RevealPathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sourceConversationId: str
    sourceTurnId: str
    note: Optional[str] = None


class ResolveArtifactProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveTaskProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]


class SetPermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    acknowledge: bool = False


class ApiRequestError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _correlation_id() -> str:
    return f"corr_{uuid.uuid4().hex}"


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "correlationId": _correlation_id(),
            }
        },
    )


def _repository_error_status(error: RepositoryError) -> int:
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, (ConflictError, InvalidStateError)):
        return 409
    if isinstance(error, ValidationError):
        return 400
    return 500


def _event_sse(event: RuntimeEvent) -> str:
    payload = json.dumps(
        runtime_event_json(event),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {payload}\n\n"


def _runtime_v2_product_sse(event: ProductRuntimeEventRecord) -> str:
    payload = json.dumps(
        product_event_json(event),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {event.event_seq}\nevent: {event.event_type}\ndata: {payload}\n\n"


def _runtime_v2_lane_json(
    lane: LaneRecord,
    *,
    active_lane_id: Optional[str] = None,
) -> dict[str, object]:
    source_lane_id = lane.source_lane_id
    title = lane.display_name or lane.summary
    return {
        "id": lane.id,
        "conversationId": lane.conversation_id,
        "kind": lane.kind.value,
        "status": lane.status.value,
        "archived": lane.is_archived,
        "archivedAt": lane.archived_at,
        "displayName": lane.display_name,
        "summary": lane.summary,
        "title": title,
        "baseEntryExcerpt": lane.summary,
        "baseEntryId": lane.base_entry_id,
        "leafEntryId": lane.leaf_entry_id,
        "createdFromEntryId": lane.created_from_entry_id,
        "createdAt": lane.created_at,
        "sourceLaneId": source_lane_id,
        "isMain": lane.id == active_lane_id or (
            active_lane_id is None and lane.kind is LaneKind.MAIN
        ),
    }


def _runtime_v2_run_variant_json(run: RunRecord) -> dict[str, object]:
    return {
        "runId": run.id,
        "conversationId": run.conversation_id,
        "laneId": run.lane_id,
        "triggerEntryId": run.trigger_entry_id,
        "siblingGroupId": run.sibling_group_id,
        "assistantEntryId": run.assistant_entry_id,
        "status": run.status.value,
        "isActiveVariant": run.is_active_variant,
        "createdAt": run.created_at,
        "finishedAt": run.finished_at,
    }


def _runtime_v2_memory_json(memory: RuntimeV2MemoryRecord) -> dict[str, object]:
    return {
        "id": memory.id,
        "scope": memory.scope.value,
        "kind": memory.kind,
        "content": memory.content,
        "status": memory.status,
        "conversationId": memory.conversation_id,
        "workspaceId": memory.workspace_id,
        "laneId": memory.lane_id,
        "runId": memory.run_id,
        "sourceMemoryId": memory.source_memory_id,
        "sourceEntryId": memory.source_entry_id,
        "createdAt": memory.created_at,
        "updatedAt": memory.updated_at,
    }


def _runtime_v2_memory_promotion_json(
    promotion: RuntimeV2MemoryPromotion,
) -> dict[str, object]:
    return {
        "id": promotion.id,
        "memoryId": promotion.source_memory_id,
        "targetScope": promotion.target_scope.value,
        "targetWorkspaceId": promotion.target_workspace_id,
        "targetLaneId": promotion.target_lane_id,
        "status": promotion.status.value,
        "resolvedMemoryId": promotion.resolved_memory_id,
        "conflictMemoryId": promotion.conflict_memory_id,
        "createdAt": promotion.created_at,
        "updatedAt": promotion.updated_at,
        "resolvedAt": promotion.resolved_at,
    }


def _runtime_v2_global_runtime_json(
    status: RuntimeV2GlobalRuntimeStatus,
) -> dict[str, object]:
    return {
        "defaultRuntime": status.default_runtime,
        "rollbackForced": status.rollback_forced,
        "migrationState": status.migration_state,
        "conversationCount": status.conversation_count,
        "mappedConversationCount": status.mapped_conversation_count,
        "conversationTreeCount": status.conversation_tree_count,
        "pendingMigrationCount": status.pending_migration_count,
        "rollbackReconciliationCount": status.rollback_reconciliation_count,
    }


def _runtime_v2_conversation_runtime_json(
    status: RuntimeV2ConversationRuntimeStatus,
) -> dict[str, object]:
    return {
        "conversationId": status.conversation_id,
        "treeConversationId": status.tree_conversation_id,
        "defaultRuntime": status.default_runtime,
        "rollbackForced": status.rollback_forced,
        "overrideRuntime": status.override_runtime,
        "effectiveRuntime": status.effective_runtime,
        "canUseV2": status.can_use_v2,
        "requiresMigration": status.requires_migration,
        "v1ReadOnly": status.v1_read_only,
        "rollbackReconciliationRequired": status.rollback_reconciliation_required,
        "reason": status.reason,
    }


def _resolve_runtime_v2_conversation(
    container: AppContainer,
    conversation_id: str,
    *,
    write: bool,
) -> str:
    status = container.runtime_v2_selection_service.describe(conversation_id)
    if write and status.effective_runtime != "v2":
        raise ApiRequestError(
            "runtime_v2_not_selected",
            "该会话当前未选择 Runtime v2，请先切换会话 runtime。",
            status_code=409,
        )
    return status.tree_conversation_id


def _assert_v1_write_allowed(
    container: AppContainer,
    conversation_id: str,
) -> None:
    status = container.runtime_v2_selection_service.describe(conversation_id)
    if status.v1_read_only and not status.rollback_forced:
        raise ApiRequestError(
            "v1_read_only",
            "该会话的 v1 数据已迁移归档，请使用 Runtime v2；如需回写 v1，请启用全局回滚。",
            status_code=409,
        )


def _parse_last_event_id(turn_id: str, value: Optional[str]) -> int:
    if value is None or value == "":
        return 0
    prefix, separator, raw_sequence = value.rpartition(":")
    if separator != ":" or prefix != turn_id:
        raise ApiRequestError(
            "invalid_last_event_id",
            "Last-Event-ID 不属于当前 Turn。",
        )
    try:
        sequence = int(raw_sequence)
    except ValueError as error:
        raise ApiRequestError(
            "invalid_last_event_id",
            "Last-Event-ID 的 sequence 无效。",
        ) from error
    if sequence < 0:
        raise ApiRequestError(
            "invalid_last_event_id",
            "Last-Event-ID 的 sequence 无效。",
        )
    return sequence


def _parse_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_hybrid_weights(value: str) -> tuple[float, float]:
    text = (value or "").strip()
    if not text:
        return (0.4, 0.6)
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        raise ValueError(
            "ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS expects 'literal,semantic'"
        )
    literal, semantic = float(parts[0]), float(parts[1])
    if literal < 0 or semantic < 0:
        raise ValueError("Hybrid weights cannot be negative")
    return literal, semantic


def _parse_json_mapping(value: str) -> dict:
    text = (value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON mapping config: {value!r}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got: {value!r}")
    return parsed


def _build_container(
    settings: AppSettings,
    provider: Optional[ModelProvider],
    tool_registry: Optional[ToolRegistry],
) -> AppContainer:
    database = Database(settings.database_path)
    chat_repository = SqliteChatRepository(database)
    context_repository = SqliteContextRepository(database)
    runtime_repository = SqliteRuntimeRepository(database)
    file_repository = SqliteTextFileRepository(
        database,
        max_file_bytes=settings.max_file_bytes,
        max_files_per_conversation=settings.max_files_per_conversation,
    )
    memory_repository = SqliteMemoryRepository(database)
    proposal_repository = SqliteMemoryProposalRepository(database)
    preferences_repository = SqlitePreferencesRepository(database)
    artifact_repository = SqliteArtifactRepository(database)
    workspace_repository = SqliteWorkspaceRepository(database)
    skill_override_repository = SqliteSkillOverrideRepository(database)
    provider_profile_repository = SqliteProviderProfileRepository(database)
    mcp_server_repository = SqliteMcpServerRepository(database)
    artifact_proposal_repository = SqliteArtifactProposalRepository(database)
    task_repository = SqliteTaskRepository(database)
    task_proposal_repository = SqliteTaskProposalRepository(database)
    task_run_repository = SqliteTaskRunRepository(database)
    notification_repository = SqliteNotificationRepository(database)
    reminder_repository = SqliteReminderRepository(database)
    knowledge_proposal_repository = SqliteKnowledgeProposalRepository(database)
    retrieval_event_repository = SqliteRetrievalEventRepository(database)
    scope_weights = None
    if settings.knowledge_scope_weights:
        scope_weights = {
            KnowledgeScope(key): float(weight)
            for key, weight in settings.knowledge_scope_weights.items()
        }
    knowledge_repository = SqliteKnowledgeRepository(
        database,
        scope_weights=scope_weights,
        synonym_map=settings.knowledge_synonym_map or None,
        hybrid_literal_weight=settings.hybrid_literal_weight,
        hybrid_semantic_weight=settings.hybrid_semantic_weight,
    )
    embedding_indexer: Optional[EmbeddingIndexer] = None
    if settings.embedding_enabled:
        embedder = build_embedder(
            backend=settings.embedding_backend,
            embedding_model=settings.embedding_model,
            provider_name=settings.provider_name,
            api_key=settings.api_key,
            base_url=settings.base_url,
            cache_dir=settings.database_path.parent,
            local_repo=settings.embedding_local_repo,
            local_url_base=settings.embedding_local_url_base,
        )
        if embedder is not None:
            embedding_indexer = EmbeddingIndexer(
                database,
                embedder,
                knowledge_repository,
                max_chars=settings.embedding_max_chars,
                batch_size=settings.embedding_batch_size,
            )
            knowledge_repository.set_semantic_searcher(embedding_indexer)
            knowledge_repository.set_embedding_hook(embedding_indexer)
            memory_repository.set_embedding_hook(embedding_indexer)
            artifact_repository.set_embedding_hook(embedding_indexer)
        else:
            logger.warning(
                "Embeddings enabled but backend unavailable; literal search only."
            )
    reference_resolver = SourceReferenceResolver(
        file_repository=file_repository,
        memory_repository=memory_repository,
    )
    memory_proposal_service: Optional[MemoryProposalService] = None
    broker = RuntimeEventBroker()
    selected_provider = provider or _provider_from_settings(settings)
    runtime_v2_repository = SqliteRuntimeV2Repository(database)
    runtime_v2_memory_repository = SqliteRuntimeV2MemoryRepository(database)
    provider_manager = ProviderManager(
        repository=provider_profile_repository,
        fallback_provider=selected_provider,
    )
    selected_tool_registry = tool_registry or ToolRegistry()
    runtime_v2_gateway = RuntimeV2SessionGateway(
        chat_repository=chat_repository,
        repository=runtime_v2_repository,
        provider=selected_provider,
            tool_registry=selected_tool_registry,
            model=settings.model,
            max_output_tokens=settings.max_output_tokens,
            max_model_turns=settings.max_agent_iterations,
            temperature=None,
        provider_slot_limit=settings.max_concurrent_model_calls,
        memory_repository=runtime_v2_memory_repository,
    )
    runtime_v2_selection_service = RuntimeV2RuntimeSelectionService(
        database=database,
        repository=runtime_v2_repository,
        default_runtime=settings.runtime,
        rollback_forced=settings.runtime_rollback,
    )
    workspace_resolver = WorkspaceResolver(chat_repository, workspace_repository)
    skill_service = SkillService(
        user_dir=settings.database_path.parent / "skills",
        database_path=settings.database_path,
        override_repository=skill_override_repository,
    )

    def skill_roots_for_conversation(conversation_id: str) -> tuple[Path, ...]:
        binding = workspace_resolver.resolve_binding(conversation_id)
        return skill_service.skill_roots(binding.root if binding else None)

    def skill_prompt_for_workspace(workspace_id: Optional[str]) -> str:
        root = None
        if workspace_id:
            try:
                workspace = workspace_repository.get_workspace(workspace_id)
                root = Path(workspace.root_path).expanduser() if workspace.root_path else None
            except Exception:
                root = None
        return build_available_skills_prompt(
            skill_service.visible_skills(root, workspace_id=workspace_id or "")
        )

    effect_log = EffectLog(settings.database_path.parent / "logs")
    mcp_manager = McpManager(
        repository=mcp_server_repository,
        tool_registry=selected_tool_registry,
        effect_log=effect_log,
    )
    artifact_file_store = ArtifactFileStore(workspace_resolver)
    artifact_proposal_repository.set_artifact_store(artifact_file_store)
    artifact_repository.set_artifact_store(artifact_file_store)
    if tool_registry is None:
        selected_tool_registry.register(ReadTextFileTool(file_repository))
        selected_tool_registry.register(ReadArtifactTool(artifact_repository))
        selected_tool_registry.register(
            ReadWorkspaceFileTool(
                workspace_resolver, max_file_bytes=settings.max_file_bytes
            )
        )
        selected_tool_registry.register(
            WriteWorkspaceFileTool(
                workspace_resolver,
                effect_log,
                max_write_bytes=settings.workspace_max_write_bytes,
            )
        )
        selected_tool_registry.register(ListWorkspaceDirTool(workspace_resolver))
        selected_tool_registry.register(
            ReadSkillFileTool(
                skill_roots_for_conversation,
                max_file_bytes=settings.max_file_bytes,
            )
        )
        selected_tool_registry.register(
            DeleteWorkspaceFileTool(workspace_resolver, effect_log)
        )
        selected_tool_registry.register(
            RunShellTool(
                workspace_resolver,
                effect_log,
                timeout_seconds=settings.shell_timeout_seconds,
                no_change_timeout_seconds=settings.shell_no_change_timeout_seconds,
                max_output_bytes=settings.shell_max_output_bytes,
            )
        )
    system_prompt = settings.system_prompt
    system_prompt_version = settings.system_prompt_version
    if settings.artifact_proposals_enabled:
        system_prompt = system_prompt + ARTIFACT_AWARENESS_CLAUSE
        system_prompt_version = ARTIFACT_AWARENESS_PROMPT_VERSION
    if settings.task_proposals_enabled:
        system_prompt = system_prompt + TASK_AWARENESS_CLAUSE
        system_prompt_version = TASK_AWARENESS_PROMPT_VERSION
    context_builder = P0ContextBuilder(
        chat_repository,
        system_prompt=system_prompt,
        system_prompt_version=system_prompt_version,
        max_context_tokens=settings.context_window_tokens,
        summary_token_limit=settings.summary_token_limit,
        context_repository=context_repository,
        file_repository=file_repository,
        memory_repository=memory_repository,
        artifact_proposal_repository=artifact_proposal_repository,
        artifact_repository=artifact_repository,
        task_repository=task_repository,
        knowledge_repository=knowledge_repository,
        retrieval_event_repository=retrieval_event_repository,
        skill_prompt_builder=skill_prompt_for_workspace,
    )
    runtime = AssistantRuntime(
        chat_repository=chat_repository,
        runtime_repository=runtime_repository,
        context_builder=context_builder,
        provider=selected_provider,
        knowledge_query_rewriter=(
            KnowledgeQueryRewriter(selected_provider, model=settings.model)
            if settings.knowledge_query_rewrite_enabled
            else None
        ),
        knowledge_reranker=(
            KnowledgeReranker(selected_provider, model=settings.model)
            if settings.knowledge_rerank_enabled
            else None
        ),
        knowledge_repository=(
            knowledge_repository if settings.knowledge_rerank_enabled else None
        ),
        configuration=RuntimeConfiguration(
            model=settings.model,
            max_output_tokens=settings.max_output_tokens,
            max_concurrent_model_calls=settings.max_concurrent_model_calls,
            max_agent_iterations=settings.max_agent_iterations,
            max_tool_calls_per_turn=settings.max_tool_calls_per_turn,
            agent_timeout_seconds=settings.agent_timeout_seconds,
            approval_timeout_seconds=settings.approval_timeout_seconds,
        ),
        tool_registry=selected_tool_registry,
        event_publisher=broker,
        permission_mode_provider=lambda: preferences_repository.get_permission_mode()[0],
        workspace_resolver=workspace_resolver,
        provider_resolver=provider_manager.resolve,
    )

    async def build_runtime_v2_context_prefix(
        conversation_id: str,
        lane_id: str,
        run_id: str,
        user_content: str,
    ) -> tuple[ProviderMessage]:
        del lane_id
        conversation = chat_repository.get_conversation(conversation_id)
        return (
            ProviderMessage(
                role="system",
                content=context_builder.build_system_context(
                    conversation_id,
                    user_content,
                    turn_id=run_id,
                    workspace_id=conversation.workspace_id,
                ),
            ),
        )

    runtime_v2_gateway.set_context_prefix_builder(
        build_runtime_v2_context_prefix
    )
    artifact_proposal_service: Optional[ArtifactProposalService] = None
    if settings.artifact_proposals_enabled:
        artifact_proposal_service = ArtifactProposalService(
            provider=selected_provider,
            proposal_repository=artifact_proposal_repository,
            model=settings.model,
            memory_repository=memory_repository,
            runtime_repository=runtime_repository,
            artifact_repository=artifact_repository,
        )

    task_proposal_service: Optional[TaskProposalService] = None
    if settings.task_proposals_enabled:
        task_proposal_service = TaskProposalService(
            provider=selected_provider,
            proposal_repository=task_proposal_repository,
            model=settings.model,
            task_repository=task_repository,
        )

    proposal_budget: Optional[ProposalBudget] = None
    if settings.memory_proposals_enabled or settings.knowledge_proposals_enabled:
        proposal_budget = ProposalBudget(
            database,
            daily_limit=settings.proposal_daily_budget,
            cooldown_minutes=settings.proposal_cooldown_minutes,
            quiet_start=settings.proposal_quiet_start,
            quiet_end=settings.proposal_quiet_end,
        )

    memory_conflict_service: Optional[MemoryConflictService] = None
    on_turn_completed = None
    if settings.memory_proposals_enabled:
        memory_conflict_service = MemoryConflictService(
            provider=selected_provider,
            memory_repository=memory_repository,
            model=settings.model,
        )
        memory_proposal_service = MemoryProposalService(
            provider=selected_provider,
            proposal_repository=proposal_repository,
            memory_repository=memory_repository,
            model=settings.model,
            marker_gate_enabled=settings.memory_marker_gate_enabled,
        )

    knowledge_proposal_service: Optional[KnowledgeProposalService] = None
    if settings.knowledge_proposals_enabled:
        knowledge_proposal_service = KnowledgeProposalService(
            provider=selected_provider,
            proposal_repository=knowledge_proposal_repository,
            knowledge_repository=knowledge_repository,
            model=settings.model,
            chat_repository=chat_repository,
        )

    knowledge_lifecycle_service = KnowledgeLifecycleService(
        knowledge_repository=knowledge_repository,
        proposal_repository=knowledge_proposal_repository,
        retrieval_event_repository=retrieval_event_repository,
        chat_repository=chat_repository,
        budget=proposal_budget,
        duplicate_threshold=settings.knowledge_duplicate_threshold,
        decay_min_age_days=settings.knowledge_decay_min_age_days,
        decay_recheck_days=settings.knowledge_decay_recheck_days,
    )
    if settings.knowledge_feedback_enabled:
        def _resolve_feedback_chunk(ref_id: str):
            try:
                return knowledge_repository.get_chunk(ref_id)
            except NotFoundError:
                return None

        knowledge_repository.set_feedback_provider(
            CitationFeedbackProvider(
                retrieval_event_repository,
                source_resolver=_resolve_feedback_chunk,
            )
        )

    if (
        settings.memory_proposals_enabled
        or settings.artifact_proposals_enabled
        or settings.task_proposals_enabled
        or settings.knowledge_proposals_enabled
        or embedding_indexer is not None
    ):

        async def on_v2_run_completed(run_id: str) -> None:
            run = runtime_v2_repository.get_run(run_id)
            if (
                run.status is not RunStatus.COMPLETED
                or run.assistant_entry_id is None
            ):
                return
            user_entry = runtime_v2_repository.get_entry(run.trigger_entry_id)
            assistant_entry = runtime_v2_repository.get_entry(
                run.assistant_entry_id
            )
            user_message = user_entry.payload.get("content", "")
            assistant_message = assistant_entry.payload.get("content", "")
            if not isinstance(user_message, str) or not isinstance(
                assistant_message, str
            ):
                return
            if not assistant_message.strip():
                return
            conversation = chat_repository.get_conversation(run.conversation_id)
            ephemeral = (
                conversation.kind is ConversationKind.EPHEMERAL
            )
            if memory_proposal_service is not None and not ephemeral:
                if proposal_budget is None or proposal_budget.allow(
                    run.conversation_id
                ):
                    await memory_proposal_service.generate_for_turn(
                        conversation_id=run.conversation_id,
                        turn_id=run.id,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
            if knowledge_proposal_service is not None and not ephemeral:
                if proposal_budget is None or proposal_budget.allow(
                    run.conversation_id
                ):
                    await knowledge_proposal_service.generate_for_turn(
                        conversation_id=run.conversation_id,
                        turn_id=run.id,
                        user_message=user_message,
                        assistant_message=assistant_message,
                    )
            if artifact_proposal_service is not None:
                await artifact_proposal_service.generate_for_turn(
                    conversation_id=run.conversation_id,
                    turn_id=run.id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
            if task_proposal_service is not None:
                await task_proposal_service.generate_for_turn(
                    conversation_id=run.conversation_id,
                    turn_id=run.id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )

        runtime_v2_gateway.set_run_completion_callback(on_v2_run_completed)

        async def on_turn_completed(snapshot) -> None:
            active = next(
                (
                    item
                    for item in snapshot.response_variants
                    if item.variant.id == snapshot.turn.active_response_variant_id
                ),
                None,
            )
            if active is None or not active.assistant_message.content.strip():
                return
            try:
                conversation = chat_repository.get_conversation(
                    snapshot.turn.conversation_id
                )
            except NotFoundError:
                return
            ephemeral = conversation.kind is ConversationKind.EPHEMERAL
            if embedding_indexer is not None and not ephemeral:
                embedding_indexer.submit(
                    KnowledgeScope.CONVERSATION.value, snapshot.turn.id
                )
            if memory_proposal_service is not None and not ephemeral:
                if proposal_budget is None or proposal_budget.allow(
                    snapshot.turn.conversation_id
                ):
                    await memory_proposal_service.generate_for_turn(
                        conversation_id=snapshot.turn.conversation_id,
                        turn_id=snapshot.turn.id,
                        user_message=snapshot.user_message.content,
                        assistant_message=active.assistant_message.content,
                    )
            if knowledge_proposal_service is not None and not ephemeral:
                if proposal_budget is None or proposal_budget.allow(
                    snapshot.turn.conversation_id
                ):
                    await knowledge_proposal_service.generate_for_turn(
                        conversation_id=snapshot.turn.conversation_id,
                        turn_id=snapshot.turn.id,
                        user_message=snapshot.user_message.content,
                        assistant_message=active.assistant_message.content,
                    )
            if artifact_proposal_service is not None:
                await artifact_proposal_service.generate_for_turn(
                    conversation_id=snapshot.turn.conversation_id,
                    turn_id=snapshot.turn.id,
                    user_message=snapshot.user_message.content,
                    assistant_message=active.assistant_message.content,
                )
            if task_proposal_service is not None:
                await task_proposal_service.generate_for_turn(
                    conversation_id=snapshot.turn.conversation_id,
                    turn_id=snapshot.turn.id,
                    user_message=snapshot.user_message.content,
                    assistant_message=active.assistant_message.content,
                )

    controller = TurnController(
        chat_repository=chat_repository,
        runtime=runtime,
        on_turn_completed=on_turn_completed,
    )
    task_notification_service = None
    if settings.notifications_enabled:
        task_notification_service = TaskNotificationService(
            notification_repository=notification_repository
        )
    task_run_review_service = None
    if settings.task_run_review_enabled:
        task_run_review_service = TaskRunReviewService(
            provider=selected_provider, model=settings.model
        )
    task_worker = TaskWorker(
        task_repository=task_repository,
        run_repository=task_run_repository,
        controller=controller,
        chat_repository=chat_repository,
        max_concurrent_task_runs=settings.max_concurrent_task_runs,
        review_service=task_run_review_service,
        notification_service=task_notification_service,
        reminder_repository=reminder_repository,
    )
    task_scheduler = TaskScheduler(
        task_repository=task_repository,
        run_repository=task_run_repository,
        reminder_repository=reminder_repository,
        knowledge_repository=knowledge_repository,
        max_attempts=settings.task_max_attempts,
        retry_backoff=(
            timedelta(seconds=settings.task_retry_backoff_seconds),
            timedelta(seconds=settings.task_retry_backoff_seconds * 5),
        ),
        worker=task_worker,
        tick_seconds=settings.scheduler_tick_seconds,
    )
    return AppContainer(
        settings=settings,
        database=database,
        chat_repository=chat_repository,
        runtime_repository=runtime_repository,
        file_repository=file_repository,
        memory_repository=memory_repository,
        proposal_repository=proposal_repository,
        preferences_repository=preferences_repository,
        artifact_repository=artifact_repository,
        artifact_proposal_repository=artifact_proposal_repository,
        task_repository=task_repository,
        task_proposal_repository=task_proposal_repository,
        task_proposal_service=task_proposal_service,
        task_run_repository=task_run_repository,
        notification_repository=notification_repository,
        reminder_repository=reminder_repository,
        knowledge_proposal_repository=knowledge_proposal_repository,
        knowledge_proposal_service=knowledge_proposal_service,
        knowledge_repository=knowledge_repository,
        retrieval_event_repository=retrieval_event_repository,
        workspace_repository=workspace_repository,
        workspace_resolver=workspace_resolver,
        skill_service=skill_service,
        provider_profile_repository=provider_profile_repository,
        provider_manager=provider_manager,
        mcp_server_repository=mcp_server_repository,
        mcp_manager=mcp_manager,
        effect_log=effect_log,
        artifact_file_store=artifact_file_store,
        knowledge_lifecycle_service=knowledge_lifecycle_service,
        embedding_indexer=embedding_indexer,
        proposal_budget=proposal_budget,
        task_notification_service=task_notification_service,
        task_worker=task_worker,
        task_scheduler=task_scheduler,
        artifact_proposal_service=artifact_proposal_service,
        reference_resolver=reference_resolver,
        memory_proposal_service=memory_proposal_service,
        memory_conflict_service=memory_conflict_service,
        broker=broker,
        provider=selected_provider,
        runtime=runtime,
        tool_registry=selected_tool_registry,
        controller=controller,
        runtime_v2_repository=runtime_v2_repository,
        runtime_v2_memory_repository=runtime_v2_memory_repository,
        runtime_v2_gateway=runtime_v2_gateway,
        runtime_v2_selection_service=runtime_v2_selection_service,
    )


def _provider_from_settings(settings: AppSettings) -> ModelProvider:
    if settings.provider_name == "fake":
        return FakeProvider()
    if settings.provider_name not in {"deepseek", "openai", "openai-compatible"}:
        return UnconfiguredProvider(settings.provider_name or "unknown")
    if not settings.api_key or not settings.model:
        return UnconfiguredProvider(settings.provider_name)
    if settings.provider_name == "openai-compatible" and not settings.base_url:
        return UnconfiguredProvider(settings.provider_name)
    return OpenAICompatibleProvider(
        name=settings.provider_name,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout_seconds=settings.provider_timeout_seconds,
    )


class KnowledgeSourceBody(BaseModel):
    kind: str
    title: str
    content: str
    fileName: Optional[str] = None
    expiresAt: Optional[str] = None
    workspaceId: Optional[str] = None


class KnowledgeSourcePatch(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    fileName: Optional[str] = None
    expiresAt: Optional[str] = None


class SearchBody(BaseModel):
    query: str
    scopes: list[str] = [
        "source",
        "memory",
        "artifact",
        "conversation",
    ]
    limit: int = 8
    workspaceId: Optional[str] = None


class RetrievalEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    label: Optional[str] = None
    scope: Optional[str] = None
    refId: Optional[str] = None
    query: Optional[str] = None
    turnId: Optional[str] = None
    conversationId: Optional[str] = None


async def _run_knowledge_decay_loop(container: "AppContainer") -> None:
    """R5.10：周期性衰减确认。启动后先做一轮补偿检查，之后按间隔循环。"""
    interval_seconds = (
        max(container.settings.knowledge_decay_interval_hours, 0.25) * 3600.0
    )
    await asyncio.sleep(min(300.0, interval_seconds))
    while True:
        try:
            if container.knowledge_lifecycle_service is not None:
                container.knowledge_lifecycle_service.check_decay()
        except Exception:  # noqa: BLE001 周期检查失败不影响服务
            logger.debug("Knowledge decay check failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


def create_app(
    *,
    settings: Optional[AppSettings] = None,
    provider: Optional[ModelProvider] = None,
    tool_registry: Optional[ToolRegistry] = None,
) -> FastAPI:
    selected_settings = settings or AppSettings.from_environment()
    configure_safe_logging(api_key=selected_settings.api_key)
    container = _build_container(selected_settings, provider, tool_registry)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        container.database.initialize()
        env_key_ref = ""
        if container.settings.provider_name == "deepseek" and os.environ.get(
            "DEEPSEEK_API_KEY"
        ):
            env_key_ref = "${DEEPSEEK_API_KEY}"
        elif container.settings.provider_name == "openai" and os.environ.get(
            "OPENAI_API_KEY"
        ):
            env_key_ref = "${OPENAI_API_KEY}"
        container.provider_profile_repository.ensure_builtin_profile(
            name=container.settings.provider_name,
            default_model=container.settings.model,
            base_url=container.settings.base_url or "",
            api_key_ref=env_key_ref,
            timeout_seconds=container.settings.provider_timeout_seconds,
        )
        if container.embedding_indexer is not None:
            container.embedding_indexer.start()
        await container.runtime.recover_interrupted()
        await container.mcp_manager.start_all()
        scheduler_task: Optional[asyncio.Task[None]] = None
        swept_runs = container.task_run_repository.sweep_interrupted_runs()
        if swept_runs and container.task_notification_service is not None:
            for swept_run in swept_runs:
                try:
                    swept_task = container.task_repository.get_task(
                        swept_run.task_id
                    )
                except NotFoundError:
                    continue
                container.task_notification_service.notify_run(
                    swept_task, swept_run
                )
        if container.settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(
                container.task_scheduler.run()
            )
        decay_task: Optional[asyncio.Task[None]] = None
        if (
            container.settings.knowledge_decay_enabled
            and container.knowledge_lifecycle_service is not None
        ):
            decay_task = asyncio.create_task(_run_knowledge_decay_loop(container))
        try:
            yield
        finally:
            if container.embedding_indexer is not None:
                container.embedding_indexer.stop()
            if scheduler_task is not None:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
            if decay_task is not None:
                decay_task.cancel()
                try:
                    await decay_task
                except asyncio.CancelledError:
                    pass
            await container.mcp_manager.stop_all()
            await container.task_worker.drain()
            await container.controller.shutdown()
            await container.runtime_v2_gateway.shutdown()
            await container.provider_manager.close()
            close_provider = getattr(container.provider, "close", None)
            if close_provider is not None:
                await close_provider()

    app = FastAPI(title="Endless Task Local API", version="0.8.0", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )
    app.state.container = container

    @app.exception_handler(RepositoryError)
    async def handle_repository_error(
        _: Request,
        error: RepositoryError,
    ) -> JSONResponse:
        return _error_response(
            status_code=_repository_error_status(error),
            code=error.code,
            message=str(error),
        )

    @app.exception_handler(ApiRequestError)
    async def handle_api_error(_: Request, error: ApiRequestError) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
        )

    @app.exception_handler(FileError)
    async def handle_file_error(_: Request, error: FileError) -> JSONResponse:
        return _error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.safe_message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        del error
        return _error_response(
            status_code=400,
            code="invalid_request",
            message="请求参数格式不正确。",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, error: Exception) -> JSONResponse:
        correlation_id = _correlation_id()
        logger.exception("Unhandled local API error", extra={"correlation_id": correlation_id})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "本地服务发生错误。",
                    "retryable": True,
                    "correlationId": correlation_id,
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, object]:
        selection = container.provider_manager.default_selection()
        return {
            "status": "ok",
            "provider": selection.provider.name,
            "model": selection.model,
            "providerConfigured": not isinstance(
                selection.provider,
                UnconfiguredProvider,
            ),
            "embedding": {
                "enabled": container.settings.embedding_enabled,
                "backend": (
                    container.settings.embedding_backend
                    if container.settings.embedding_enabled
                    else None
                ),
                "model": (
                    container.embedding_indexer.model_name
                    if container.embedding_indexer is not None
                    else None
                ),
                "ready": (
                    container.embedding_indexer is not None
                    and not container.embedding_indexer.unavailable
                ),
            },
        }

    @app.get("/capabilities")
    async def capabilities() -> dict[str, object]:
        skills_by_path: dict[str, Skill] = {}
        for skill in container.skill_service.list_skills():
            skills_by_path[str(skill.file_path)] = skill
        for workspace in container.workspace_repository.list_workspaces():
            if workspace.root_path:
                for skill in container.skill_service.list_skills(
                    Path(workspace.root_path),
                    workspace_id=workspace.id,
                ):
                    skills_by_path[str(skill.file_path)] = skill
        skills = list(skills_by_path.values())
        skill_diagnostics = [
            {
                "path": str(diagnostic.path),
                "code": diagnostic.code,
                "message": diagnostic.message,
            }
            for skill in skills
            for diagnostic in skill.diagnostics
        ]
        skills_state = "degraded" if skill_diagnostics else "ok"

        mcp_statuses = container.mcp_manager.list_statuses()
        mcp_issues: list[str] = []
        for status in mcp_statuses:
            if status.state == "reconnecting":
                mcp_issues.append("mcp:reconnecting")
            elif status.enabled and status.state != "connected":
                mcp_issues.append("mcp:error")
        mcp_state = "degraded" if mcp_issues else "ok"

        provider_selection = container.provider_manager.default_selection()
        provider_configured = not isinstance(
            provider_selection.provider,
            UnconfiguredProvider,
        )
        provider_state = "ok" if provider_configured else "unavailable"
        provider_issues = [] if provider_configured else ["provider:unconfigured"]

        embedding_ready = (
            container.embedding_indexer is not None
            and not container.embedding_indexer.unavailable
        )
        embedding_issues = (
            [] if not container.settings.embedding_enabled or embedding_ready
            else ["embedding:unavailable"]
        )
        embedding_state = "ok" if not embedding_issues else "degraded"

        states = (skills_state, mcp_state, provider_state, embedding_state)
        if "unavailable" in states:
            summary_state = "unavailable"
        elif "degraded" in states:
            summary_state = "degraded"
        else:
            summary_state = "ok"
        issues = [
            *(["skill:diagnostics"] if skill_diagnostics else []),
            *mcp_issues,
            *provider_issues,
            *embedding_issues,
        ]
        return {
            "summary": {"state": summary_state, "issues": issues},
            "runtime": _runtime_v2_global_runtime_json(
                container.runtime_v2_selection_service.global_status()
            ),
            "skills": {
                "state": skills_state,
                "total": len(skills),
                "enabled": sum(not skill.disabled for skill in skills),
                "diagnostics": skill_diagnostics,
            },
            "mcp": {
                "state": mcp_state,
                "servers": [
                    {
                        "id": status.server_id,
                        "name": status.name,
                        "state": status.state,
                        "toolCount": status.tool_count,
                        "lastError": status.last_error,
                    }
                    for status in mcp_statuses
                ],
            },
            "provider": {
                "state": provider_state,
                "defaultProfileId": provider_selection.profile.id,
                "profileName": provider_selection.profile.name,
                "model": provider_selection.model,
                "configured": provider_configured,
                "fallback": False,
            },
            "embedding": {
                "state": embedding_state,
                "enabled": container.settings.embedding_enabled,
                "backend": (
                    container.settings.embedding_backend
                    if container.settings.embedding_enabled
                    else None
                ),
                "ready": embedding_ready,
            },
        }

    @app.get("/settings/permissions")
    async def get_permission_mode() -> dict[str, object]:
        mode, updated_at = container.preferences_repository.get_permission_mode()
        return {"mode": mode.value, "updatedAt": updated_at}

    @app.post("/settings/permissions")
    async def set_permission_mode(
        body: SetPermissionBody,
    ) -> dict[str, object]:
        try:
            target = PermissionMode(body.mode)
        except ValueError as error:
            raise ApiRequestError(
                "invalid_request",
                "mode 必须是 confirm_every_time / trust_local_writes / trust_all。",
            ) from error
        current, _ = container.preferences_repository.get_permission_mode()
        if target.is_escalation_from(current) and not body.acknowledge:
            raise ApiRequestError(
                "invalid_request",
                "提权需要 acknowledge=true 显式确认。",
            )
        mode, updated_at = container.preferences_repository.set_permission_mode(
            target
        )
        return {"mode": mode.value, "updatedAt": updated_at}

    @app.post("/conversations", status_code=201)
    async def create_conversation(
        body: Optional[CreateConversationBody] = None,
    ) -> dict[str, object]:
        workspace_id = _resolve_workspace_reference(
            body.workspaceId if body is not None else None
        )
        return conversation_json(
            container.chat_repository.create_or_reuse_empty_conversation(
                workspace_id
            )
        )

    @app.get("/conversations")
    async def list_conversations(
        status: ConversationStatus = Query(ConversationStatus.ACTIVE),
        query: Optional[str] = Query(None),
        workspace: Optional[str] = Query(None),
    ) -> dict[str, object]:
        workspace_id: Optional[str] = None
        general_only = False
        text = (workspace or "").strip()
        if text == "general":
            general_only = True
        elif text:
            workspace_id = _resolve_workspace_reference(text)
            if workspace_id is None:
                general_only = True
        conversations = container.chat_repository.list_conversations(
            status=status,
            title_query=query,
            workspace_id=workspace_id,
            general_only=general_only,
        )
        return {"items": [conversation_json(item) for item in conversations]}

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_conversation_snapshot(conversation_id)
        if snapshot.conversation.parent_conversation_id is not None:
            lineage = container.chat_repository.list_lineage_turns(conversation_id)
            snapshot = ConversationSnapshot(
                conversation=snapshot.conversation,
                turns=tuple(lineage) + tuple(snapshot.turns),
            )
        payload = conversation_snapshot_json(
            snapshot,
            events_by_turn={
                turn.turn.id: tuple(
                    container.runtime_repository.list_events(turn.turn.id)
                )
                for turn in snapshot.turns
            },
        )
        payload["files"] = [
            uploaded_text_file_json(item)
            for item in container.file_repository.list_files(conversation_id)
        ]
        if snapshot.conversation.parent_conversation_id is not None:
            try:
                parent = container.chat_repository.get_conversation(
                    snapshot.conversation.parent_conversation_id
                )
                payload["parentTitle"] = parent.title
            except NotFoundError:
                payload["parentTitle"] = None
        return payload

    @app.post("/conversations/{conversation_id}/files", status_code=201)
    async def upload_conversation_file(
        conversation_id: str,
        request: Request,
        filename: str = Query(..., min_length=1, max_length=512),
    ) -> dict[str, object]:
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > container.settings.max_file_bytes:
                raise FileError(
                    "file_too_large",
                    f"文件超过 {container.settings.max_file_bytes} 字节的本地限制。",
                    status_code=413,
                )
            content.extend(chunk)
        uploaded = container.file_repository.create_file(
            conversation_id=conversation_id,
            original_name=filename,
            media_type=request.headers.get("content-type", "application/octet-stream"),
            content=bytes(content),
        )
        return uploaded_text_file_json(uploaded)

    @app.delete("/conversations/{conversation_id}/files/{file_id}", status_code=204)
    async def delete_conversation_file(
        conversation_id: str,
        file_id: str,
    ) -> Response:
        container.file_repository.delete_file(
            conversation_id=conversation_id,
            file_id=file_id,
        )
        return Response(status_code=204)

    @app.patch("/conversations/{conversation_id}")
    async def patch_conversation(
        conversation_id: str,
        body: ConversationPatch,
    ) -> dict[str, object]:
        if (
            body.title is None
            and body.status is None
            and body.providerProfileId is None
            and body.modelOverride is None
        ):
            raise ApiRequestError(
                "invalid_request",
                "至少需要提供 title、status、providerProfileId 或 modelOverride。",
            )
        conversation = container.chat_repository.get_conversation(conversation_id)
        if body.title is not None:
            conversation = container.chat_repository.rename_conversation(
                conversation_id,
                body.title,
            )
        if body.status is not None:
            conversation = container.chat_repository.set_conversation_status(
                conversation_id,
                body.status,
            )
        if body.providerProfileId is not None or body.modelOverride is not None:
            conversation = container.chat_repository.set_conversation_model(
                conversation_id,
                provider_profile_id=body.providerProfileId,
                model_override=body.modelOverride,
            )
        return conversation_json(conversation)

    @app.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str) -> Response:
        descendant_ids = container.chat_repository.list_descendant_ids(
            conversation_id
        )
        for target_id in (conversation_id, *descendant_ids):
            container.reminder_repository.cancel_reminders_for_conversation(
                target_id
            )
            container.task_repository.cancel_tasks_for_conversation(target_id)
            container.task_proposal_repository.cancel_proposals_for_conversation(
                target_id
            )
            container.proposal_repository.cancel_proposals_for_conversation(
                target_id
            )
        container.chat_repository.delete_conversation(conversation_id)
        return Response(status_code=204)

    @app.post("/conversations/{conversation_id}/branches", status_code=201)
    async def create_branch(
        conversation_id: str,
        body: CreateBranchBody,
    ) -> dict[str, object]:
        _assert_v1_write_allowed(container, conversation_id)
        branch = container.chat_repository.create_branch(
            parent_conversation_id=conversation_id,
            fork_turn_id=body.forkTurnId,
            kind=ConversationKind.EPHEMERAL,
        )
        return {"conversation": conversation_json(branch)}

    @app.get("/conversations/{conversation_id}/branches")
    async def list_branches(conversation_id: str) -> dict[str, object]:
        branches = container.chat_repository.list_branches(conversation_id)
        return {"items": [conversation_json(item) for item in branches]}

    @app.post("/conversations/{conversation_id}/promote")
    async def promote_conversation(conversation_id: str) -> dict[str, object]:
        _assert_v1_write_allowed(container, conversation_id)
        conversation = container.chat_repository.promote_conversation(
            conversation_id
        )
        return {"conversation": conversation_json(conversation)}

    @app.get("/memories")
    async def list_memories(include_deleted: bool = False) -> dict[str, object]:
        memories = container.memory_repository.list_memories(
            include_deleted=include_deleted
        )
        titles: dict[str, str] = {}
        for item in memories:
            conversation_id = item.source_conversation_id
            if conversation_id and conversation_id not in titles:
                try:
                    titles[conversation_id] = container.chat_repository.get_conversation(
                        conversation_id
                    ).title
                except NotFoundError:
                    titles[conversation_id] = ""
        return {
            "items": [
                memory_record_json(
                    item,
                    source_title=titles.get(item.source_conversation_id) or None,
                )
                for item in memories
            ]
        }

    @app.patch("/memories/{memory_id}")
    async def update_memory(
        memory_id: str, body: UpdateMemoryBody
    ) -> dict[str, object]:
        record = container.memory_repository.update_memory_content(
            memory_id, body.content
        )
        return {"memory": memory_record_json(record)}

    @app.delete("/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: str) -> Response:
        container.memory_repository.delete_memory(memory_id)
        return Response(status_code=204)

    # ---------- P5 知识源与联合检索 ----------

    def _resolve_workspace_reference(value: Optional[str]) -> Optional[str]:
        """把请求里的归属值落为列值："general"/空 → None（全局）；其余校验存在。"""
        text = (value or "").strip()
        if not text or text == "general":
            return None
        container.workspace_repository.get_workspace(text)
        return text

    def _emit_knowledge_duplicates(source) -> None:
        service = container.knowledge_lifecycle_service
        if service is None:
            return
        try:
            service.detect_duplicates(source)
        except Exception:  # noqa: BLE001 去重检测不影响写入本身
            logger.debug("Knowledge duplicate detection failed", exc_info=True)

    def _provider_profile_json(profile) -> dict[str, object]:
        return {
            "id": profile.id,
            "name": profile.name,
            "kind": profile.kind,
            "baseUrl": profile.base_url,
            "defaultModel": profile.default_model,
            "timeoutSeconds": profile.timeout_seconds,
            "enabled": profile.enabled,
            "isBuiltin": profile.is_builtin,
            "isDefault": (
                profile.id
                == container.provider_profile_repository.get_default_profile_id()
            ),
            "configured": profile.is_builtin
            or bool(profile.api_key_ref.strip() or profile.base_url.strip()),
            "createdAt": profile.created_at,
            "updatedAt": profile.updated_at,
        }

    @app.get("/providers")
    async def list_providers() -> dict[str, object]:
        return {
            "items": [
                _provider_profile_json(profile)
                for profile in container.provider_profile_repository.list_profiles()
            ]
        }

    @app.post("/providers", status_code=201)
    async def create_provider_profile(
        body: ProviderProfileBody,
    ) -> dict[str, object]:
        profile = container.provider_profile_repository.create_profile(
            ProviderProfileDraft(
                name=body.name,
                default_model=body.defaultModel,
                base_url=body.baseUrl,
                api_key_ref=body.apiKeyRef,
                timeout_seconds=body.timeoutSeconds,
                enabled=body.enabled,
            )
        )
        return {"profile": _provider_profile_json(profile)}

    @app.patch("/providers/{profile_id}")
    async def patch_provider_profile(
        profile_id: str, body: ProviderProfilePatchBody
    ) -> dict[str, object]:
        current = container.provider_profile_repository.get_profile(profile_id)
        profile = container.provider_profile_repository.update_profile(
            profile_id,
            ProviderProfileDraft(
                name=body.name if body.name is not None else current.name,
                default_model=(
                    body.defaultModel
                    if body.defaultModel is not None
                    else current.default_model
                ),
                base_url=body.baseUrl if body.baseUrl is not None else current.base_url,
                api_key_ref=(
                    body.apiKeyRef
                    if body.apiKeyRef is not None
                    else current.api_key_ref
                ),
                timeout_seconds=(
                    body.timeoutSeconds
                    if body.timeoutSeconds is not None
                    else current.timeout_seconds
                ),
                enabled=body.enabled if body.enabled is not None else current.enabled,
            )
        )
        await container.provider_manager.invalidate(profile.id)
        return {"profile": _provider_profile_json(profile)}

    @app.delete("/providers/{profile_id}", status_code=204)
    async def delete_provider_profile(profile_id: str) -> None:
        await container.provider_manager.invalidate(profile_id)
        container.provider_profile_repository.delete_profile(profile_id)

    @app.post("/providers/default")
    async def set_default_provider(body: ProviderDefaultBody) -> dict[str, object]:
        container.provider_profile_repository.set_default_profile_id(body.profileId)
        profile = container.provider_profile_repository.get_profile(body.profileId)
        return {"profile": _provider_profile_json(profile)}

    def _mcp_json(config, status) -> dict[str, object]:
        return {
            "id": config.id,
            "name": config.name,
            "transport": config.transport,
            "command": config.command,
            "args": list(config.args),
            "cwd": config.cwd,
            "url": config.url,
            "enabled": config.enabled,
            "toolCallTimeoutSeconds": config.tool_call_timeout_seconds,
            "state": status.state,
            "toolCount": status.tool_count,
            "lastError": status.last_error,
            "tools": [
                {
                    "publicName": tool.public_name,
                    "rawName": tool.raw_name,
                    "description": tool.description,
                    "effect": tool.effect,
                    "requiresExplicitConfirmation": (
                        tool.requires_explicit_confirmation
                    ),
                }
                for tool in status.tools
            ],
            "createdAt": config.created_at,
            "updatedAt": config.updated_at,
        }

    @app.get("/mcp/servers")
    async def list_mcp_servers() -> dict[str, object]:
        items = []
        for config in container.mcp_server_repository.list_servers():
            items.append(_mcp_json(config, container.mcp_manager.status(config.id)))
        return {"items": items}

    @app.post("/mcp/servers", status_code=201)
    async def create_mcp_server(body: McpServerBody) -> dict[str, object]:
        config = container.mcp_server_repository.create_server(
            McpServerDraft(
                name=body.name,
                transport=body.transport,
                command=body.command,
                args=tuple(body.args),
                env=body.env,
                cwd=body.cwd,
                url=body.url,
                headers=body.headers,
                enabled=body.enabled,
                tool_call_timeout_seconds=body.toolCallTimeoutSeconds,
            )
        )
        try:
            await container.mcp_manager.reload(config.id)
        except Exception:
            pass
        return {"server": _mcp_json(config, container.mcp_manager.status(config.id))}

    @app.patch("/mcp/servers/{server_id}")
    async def patch_mcp_server(
        server_id: str, body: McpServerPatchBody
    ) -> dict[str, object]:
        current = container.mcp_server_repository.get_server(server_id)
        name = body.name if body.name is not None else current.name
        transport = body.transport if body.transport is not None else current.transport
        command = body.command if body.command is not None else current.command
        args = tuple(body.args) if body.args is not None else current.args
        env = body.env if body.env is not None else current.env
        cwd = body.cwd if body.cwd is not None else current.cwd
        url = body.url if body.url is not None else current.url
        headers = body.headers if body.headers is not None else current.headers
        enabled = body.enabled if body.enabled is not None else current.enabled
        timeout = (
            body.toolCallTimeoutSeconds
            if body.toolCallTimeoutSeconds is not None
            else current.tool_call_timeout_seconds
        )
        config = container.mcp_server_repository.update_server(
            server_id,
            McpServerDraft(
                name=name,
                transport=transport,
                command=command,
                args=args,
                env=env,
                cwd=cwd,
                url=url,
                headers=headers,
                enabled=enabled,
                tool_call_timeout_seconds=timeout,
            )
        )
        try:
            await container.mcp_manager.reload(config.id)
        except Exception:
            pass
        return {"server": _mcp_json(config, container.mcp_manager.status(config.id))}

    @app.delete("/mcp/servers/{server_id}", status_code=204)
    async def delete_mcp_server(server_id: str) -> None:
        await container.mcp_manager.disconnect(server_id)
        container.mcp_server_repository.delete_server(server_id)

    @app.post("/mcp/servers/{server_id}/reload")
    async def reload_mcp_server(server_id: str) -> dict[str, object]:
        try:
            status = await container.mcp_manager.reload(server_id)
        except Exception:
            status = container.mcp_manager.status(server_id)
        config = container.mcp_server_repository.get_server(server_id)
        return {"server": _mcp_json(config, status)}

    @app.get("/skills")
    async def list_skills(
        workspace: Optional[str] = Query(None),
    ) -> dict[str, object]:
        root = None
        if workspace:
            try:
                item = container.workspace_repository.get_workspace(workspace)
                root = (
                    Path(item.root_path).expanduser()
                    if item.root_path
                    else None
                )
            except Exception:
                root = None
        items = container.skill_service.list_skills(
            root, workspace_id=workspace or ""
        )
        return {
            "userSkillsDirectory": str(
                container.settings.database_path.parent / "skills"
            ),
            "items": [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope.value,
                    "filePath": str(skill.file_path),
                    "disabled": skill.disabled,
                    "disableModelInvocation": skill.disable_model_invocation,
                    "diagnostics": [
                        {
                            "code": item.code,
                            "message": item.message,
                            "path": str(item.path),
                        }
                        for item in skill.diagnostics
                    ],
                }
                for skill in items
            ],
        }

    @app.patch("/skills/{scope}/{name}")
    async def patch_skill(
        scope: Literal["user", "workspace"],
        name: str,
        body: SkillPatchBody,
        workspace: Optional[str] = Query(None),
    ) -> dict[str, object]:
        from endless_task.skills import SkillScope

        container.skill_service.set_disabled(
            scope=SkillScope(scope),
            name=name,
            disabled=body.disabled,
            workspace_id=workspace or "",
        )
        return {"disabled": body.disabled}

    @app.get("/workspaces")
    async def list_workspaces() -> dict[str, object]:
        workspaces = container.workspace_repository.list_workspaces()
        return {"items": [workspace_json(item) for item in workspaces]}

    @app.post("/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceBody) -> dict[str, object]:
        workspace = container.workspace_repository.create_workspace(body.name)
        if body.rootPath is not None and body.rootPath.strip():
            workspace = container.workspace_repository.bind_root_path(
                workspace.id, body.rootPath
            )
        return {"workspace": workspace_json(workspace)}

    @app.patch("/workspaces/{workspace_id}")
    async def patch_workspace(
        workspace_id: str, body: WorkspacePatchBody
    ) -> dict[str, object]:
        if body.rootPath is None:
            workspace = container.workspace_repository.unbind_root_path(workspace_id)
        else:
            workspace = container.workspace_repository.bind_root_path(
                workspace_id, body.rootPath
            )
        return {"workspace": workspace_json(workspace)}

    @app.get("/filesystem/browse")
    async def browse_filesystem(
        path: Optional[str] = Query(None),
        show_hidden: bool = Query(False),
    ) -> dict[str, object]:
        current, items = browse_directory(
            path, show_hidden=show_hidden, home=Path.home()
        )
        return {
            "currentPath": current,
            "items": [
                {
                    "name": item.name,
                    "path": item.path,
                    "kind": item.kind,
                    "size": item.size,
                    "writable": item.writable,
                    "isHidden": item.is_hidden,
                }
                for item in items
            ],
        }

    @app.post("/filesystem/reveal")
    async def reveal_path(body: RevealPathBody) -> dict[str, object]:
        import subprocess

        target = Path(body.path).expanduser().resolve()
        if not target.exists():
            raise ValidationError("路径不存在，无法在访达中显示。")
        if sys.platform != "darwin":
            return {"revealed": False, "message": "当前平台不支持打开系统文件管理器。"}
        try:
            subprocess.Popen(["open", "-R", str(target)])
        except OSError as error:
            raise ValidationError("无法打开系统文件管理器。") from error
        return {"revealed": True}

    @app.get("/workspaces/{workspace_id}/shell-log")
    async def list_shell_log(
        workspace_id: str, limit: int = Query(100, ge=1, le=500)
    ) -> dict[str, object]:
        container.workspace_repository.get_workspace(workspace_id)
        entries = container.effect_log.list_for_workspace(
            workspace_id, limit=limit
        )
        return {"items": entries}

    @app.get("/knowledge-sources")
    async def list_knowledge_sources(
        status: str = "active", workspace: Optional[str] = Query(None)
    ) -> dict[str, object]:
        try:
            status_enum = KnowledgeSourceStatus(status)
        except ValueError as error:
            raise ValidationError(f"Unknown knowledge source status: {status}") from error
        workspace_value = (workspace or "").strip() or None
        workspace_filter: Optional[str] = None
        if workspace_value is not None:
            if workspace_value == "general":
                workspace_filter = container.knowledge_repository.WORKSPACE_GENERAL
            else:
                workspace_filter = _resolve_workspace_reference(workspace_value)
        sources = container.knowledge_repository.list_sources(
            status=status_enum, workspace_id=workspace_filter
        )
        return {"items": [knowledge_source_json(item) for item in sources]}

    @app.post("/knowledge-sources", status_code=201)
    async def create_knowledge_source(body: KnowledgeSourceBody) -> dict[str, object]:
        try:
            kind = KnowledgeSourceKind(body.kind)
        except ValueError as error:
            raise ValidationError(f"Unknown knowledge source kind: {body.kind}") from error
        source = container.knowledge_repository.create_source(
            kind=kind,
            origin=KnowledgeSourceOrigin.USER,
            title=body.title,
            content=body.content,
            file_name=body.fileName,
            expires_at=body.expiresAt,
            workspace_id=_resolve_workspace_reference(body.workspaceId),
        )
        _emit_knowledge_duplicates(source)
        return {"source": knowledge_source_json(source)}

    @app.patch("/knowledge-sources/{source_id}")
    async def update_knowledge_source(
        source_id: str, body: KnowledgeSourcePatch
    ) -> dict[str, object]:
        source = container.knowledge_repository.update_source(
            source_id,
            title=body.title,
            content=body.content,
            file_name=body.fileName,
            expires_at=body.expiresAt,
        )
        return {"source": knowledge_source_json(source)}

    @app.post("/knowledge-sources/{source_id}/expire")
    async def expire_knowledge_source(source_id: str) -> dict[str, object]:
        source = container.knowledge_repository.expire_source(source_id)
        return {"source": knowledge_source_json(source)}

    @app.post("/knowledge-sources/{source_id}/restore")
    async def restore_knowledge_source(source_id: str) -> dict[str, object]:
        source = container.knowledge_repository.restore_source(source_id)
        return {"source": knowledge_source_json(source)}

    @app.delete("/knowledge-sources/{source_id}", status_code=204)
    async def delete_knowledge_source(source_id: str) -> Response:
        container.knowledge_repository.delete_source(source_id)
        return Response(status_code=204)

    @app.post("/knowledge-sources/import", status_code=201)
    async def import_knowledge_source(
        file: UploadFile = File(...),
        title: Optional[str] = Form(default=None),
        workspaceId: Optional[str] = Form(default=None),
    ) -> dict[str, object]:
        raw = await file.read()
        file_name = (file.filename or "").strip() or "未命名文件"
        try:
            ingested = ingest_file_bytes(raw, file_name=file_name)
        except IngestionError as error:
            raise ApiRequestError(
                error.code, error.safe_message, status_code=400
            ) from error
        source = container.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.FILE,
            origin=KnowledgeSourceOrigin.USER,
            title=(title or "").strip() or file_name,
            content=ingested.text,
            file_name=file_name,
            file_size=ingested.size,
            file_sha256=ingested.sha256,
            workspace_id=_resolve_workspace_reference(workspaceId),
        )
        _emit_knowledge_duplicates(source)
        return {
            "source": knowledge_source_json(source),
            "truncated": ingested.truncated,
            "encoding": ingested.encoding,
        }

    @app.get("/turns/{turn_id}/citations")
    async def get_turn_citations(turn_id: str) -> dict[str, object]:
        event = container.retrieval_event_repository.latest_injection_for_turn(
            turn_id
        )
        if event is None or not event.detail:
            return {"items": []}
        citations = event.detail.get("citations") or []
        items: list[dict[str, object]] = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            entry = dict(citation)
            if citation.get("scope") == "conversation":
                with container.database.connect() as connection:
                    row = connection.execute(
                        "SELECT conversation_id FROM turns WHERE id = ?",
                        (str(citation.get("refId", "")),),
                    ).fetchone()
                if row is not None:
                    entry["conversationId"] = row["conversation_id"]
            items.append(entry)
        return {"items": items}

    @app.post("/retrieval-events", status_code=201)
    async def create_retrieval_event(
        body: RetrievalEventBody,
    ) -> dict[str, object]:
        if body.kind != "citation_click":
            raise ApiRequestError(
                "invalid_request", "仅支持记录角标点击事件。", status_code=400
            )
        event = container.retrieval_event_repository.record(
            RetrievalEventKind.CITATION_CLICK,
            body.query or "",
            conversation_id=body.conversationId,
            turn_id=body.turnId,
            detail={
                "label": body.label,
                "scope": body.scope,
                "refId": body.refId,
            },
        )
        return {"id": event.id}

    @app.get("/retrieval-stats")
    async def get_retrieval_stats() -> dict[str, object]:
        return {"stats": container.retrieval_event_repository.summarize()}

    @app.post("/knowledge-lifecycle/decay-check")
    async def run_knowledge_decay_check() -> dict[str, object]:
        service = container.knowledge_lifecycle_service
        if service is None:
            raise ApiRequestError(
                "not_available", "知识生命周期服务未启用。", status_code=400
            )
        proposals = service.check_decay()
        return {
            "created": len(proposals),
            "items": [knowledge_proposal_json(item) for item in proposals],
        }

    @app.post("/search")
    async def search_knowledge(body: SearchBody) -> dict[str, object]:
        scopes: list[KnowledgeScope] = []
        for value in body.scopes:
            try:
                scopes.append(KnowledgeScope(value))
            except ValueError as error:
                raise ValidationError(f"Unknown search scope: {value}") from error
        limit = max(1, min(body.limit, 20))
        workspace_filter: Optional[str] = None
        if body.workspaceId is not None:
            text = body.workspaceId.strip()
            if text == "general":
                workspace_filter = container.knowledge_repository.WORKSPACE_GENERAL
            elif text:
                workspace_filter = _resolve_workspace_reference(text)
        grouped = container.knowledge_repository.search(
            body.query, scopes, limit, workspace_id=workspace_filter
        )
        # 分组顺序对齐注入优先级：curated（知识源/记忆）在前，层内按配置顺序。
        ordered_scopes = sorted(
            scopes,
            key=lambda item: (
                scope_tier(item),
                scopes.index(item),
            ),
        )
        groups: list[dict[str, object]] = []
        hit_counts: dict[str, int] = {}
        for scope in ordered_scopes:
            hits = grouped.get(scope, [])
            items: list[dict[str, object]] = []
            for hit in hits:
                entry: dict[str, object] = {
                    "refId": hit.ref_id,
                    "title": hit.title,
                    "snippet": hit.snippet,
                }
                if scope is KnowledgeScope.SOURCE:
                    source_id = hit.source_id or hit.ref_id
                    try:
                        source = container.knowledge_repository.get_source(source_id)
                    except NotFoundError:
                        continue
                    entry["origin"] = source.origin.value
                    entry["kind"] = source.kind.value
                    entry["updatedAt"] = source.updated_at
                    if hit.chunk_seq is not None:
                        entry["sourceId"] = source_id
                        entry["chunkSeq"] = hit.chunk_seq
                elif scope is KnowledgeScope.MEMORY:
                    try:
                        memory = container.memory_repository.get_memory(hit.ref_id)
                    except NotFoundError:
                        continue
                    entry["updatedAt"] = memory.updated_at
                elif scope is KnowledgeScope.ARTIFACT:
                    try:
                        artifact = container.artifact_repository.get_artifact(hit.ref_id)
                    except NotFoundError:
                        continue
                    entry["updatedAt"] = artifact.updated_at
                elif scope is KnowledgeScope.CONVERSATION:
                    with container.database.connect() as connection:
                        row = connection.execute(
                            "SELECT conversation_id FROM turns WHERE id = ?",
                            (hit.ref_id,),
                        ).fetchone()
                    if row is None:
                        continue
                    entry["conversationId"] = row["conversation_id"]
                items.append(entry)
            if items:
                groups.append({"scope": scope.value, "hits": items})
            hit_counts[scope.value] = len(items)
        try:
            container.retrieval_event_repository.record(
                RetrievalEventKind.SEARCH,
                body.query,
                hit_counts=hit_counts,
            )
        except Exception:  # noqa: BLE001 埋点失败不影响检索结果
            logger.debug("Failed to record search event", exc_info=True)
        return {"groups": groups}

    @app.post("/memory-proposals/{proposal_id}/resolve")
    async def resolve_memory_proposal(
        proposal_id: str, body: ResolveMemoryProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.proposal_repository.reject_proposal(proposal_id)
            return {"proposal": memory_proposal_json(proposal)}
        proposal, memory = container.proposal_repository.accept_proposal(
            proposal_id
        )
        if container.memory_conflict_service is not None:
            await container.memory_conflict_service.resolve_conflicts_for(memory)
        return {
            "proposal": memory_proposal_json(proposal),
            "memory": memory_record_json(memory),
        }

    @app.get("/conversations/{conversation_id}/memory-proposals")
    async def list_memory_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [memory_proposal_json(item) for item in proposals]}

    @app.get("/conversations/{conversation_id}/knowledge-proposals")
    async def list_knowledge_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.knowledge_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [knowledge_proposal_json(item) for item in proposals]}

    @app.post("/knowledge-proposals/{proposal_id}/resolve")
    async def resolve_knowledge_proposal(
        proposal_id: str, body: ResolveKnowledgeProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.knowledge_proposal_repository.reject_proposal(
                proposal_id
            )
            return {"proposal": knowledge_proposal_json(proposal)}
        workspace_override = container.knowledge_proposal_repository._UNSET_WORKSPACE
        if body.workspaceId is not None:
            workspace_override = _resolve_workspace_reference(body.workspaceId)
        proposal, source = container.knowledge_proposal_repository.accept_proposal(
            proposal_id, workspace_override=workspace_override
        )
        if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
            _emit_knowledge_duplicates(source)
        return {
            "proposal": knowledge_proposal_json(proposal),
            "source": knowledge_source_json(source),
        }

    @app.get("/tasks")
    async def list_tasks(include_cancelled: bool = False) -> dict[str, object]:
        tasks = container.task_repository.list_tasks(
            include_cancelled=include_cancelled
        )
        return {"items": [task_json(item) for item in tasks]}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict[str, object]:
        return {"task": task_json(container.task_repository.get_task(task_id))}

    @app.post("/tasks/{task_id}/run", status_code=202)
    async def run_task(task_id: str) -> dict[str, object]:
        run = await container.task_worker.start(
            task_id, trigger=TaskRunTrigger.MANUAL
        )
        return {"run": task_run_json(run)}

    @app.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.pause_task(task_id)
        return {"task": task_json(task)}

    @app.post("/tasks/{task_id}/resume")
    async def resume_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.resume_task(task_id)
        return {"task": task_json(task)}

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict[str, object]:
        task = container.task_repository.cancel_task(task_id)
        return {"task": task_json(task)}

    @app.get("/tasks/{task_id}/runs")
    async def list_task_runs(task_id: str) -> dict[str, object]:
        runs = container.task_run_repository.list_runs(task_id=task_id)
        return {"items": [task_run_json(item) for item in runs]}

    @app.get("/notifications")
    async def list_notifications(
        unread_only: bool = False,
    ) -> dict[str, object]:
        items = container.notification_repository.list_notifications(
            unread_only=unread_only
        )
        return {"items": [notification_json(item) for item in items]}

    @app.post("/notifications/read-all")
    async def read_all_notifications() -> dict[str, object]:
        return {"count": container.notification_repository.mark_all_read()}

    @app.post("/notifications/{notification_id}/read")
    async def read_notification(notification_id: str) -> dict[str, object]:
        notification = container.notification_repository.mark_read(
            notification_id
        )
        return {"notification": notification_json(notification)}

    @app.get("/proposals/pending")
    async def list_pending_proposals() -> dict[str, object]:
        items: list[dict[str, object]] = []
        for proposal in container.artifact_proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "artifact",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.title,
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.task_proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "task",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.title,
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "memory",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.content[:60],
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.knowledge_proposal_repository.list_pending(
            limit=50
        ):
            if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
                title = f"知识：{str(proposal.payload.get('title', ''))[:50]}"
            else:
                title = f"知识过期：{str(proposal.payload.get('title', ''))[:50]}"
            items.append(
                {
                    "id": proposal.id,
                    "kind": "knowledge",
                    "conversationId": proposal.conversation_id,
                    "title": title,
                    "createdAt": proposal.created_at,
                }
            )
        items.sort(key=lambda item: str(item["createdAt"]))
        return {"items": items}

    @app.get("/reminders")
    async def list_reminders(include_cancelled: bool = False) -> dict[str, object]:
        items = container.reminder_repository.list_reminders(
            include_cancelled=include_cancelled
        )
        return {"items": [reminder_json(item) for item in items]}

    @app.post("/reminders/{reminder_id}/cancel")
    async def cancel_reminder(reminder_id: str) -> dict[str, object]:
        reminder = container.reminder_repository.cancel_reminder(reminder_id)
        return {"reminder": reminder_json(reminder)}

    @app.get("/conversations/{conversation_id}/task-proposals")
    async def list_task_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.task_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [task_proposal_json(item) for item in proposals]}

    @app.post("/task-proposals/{proposal_id}/resolve")
    async def resolve_task_proposal(
        proposal_id: str, body: ResolveTaskProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.task_proposal_repository.reject_proposal(
                proposal_id
            )
            return {"proposal": task_proposal_json(proposal)}
        peek = container.task_proposal_repository.get_proposal(proposal_id)
        if isinstance(peek.schedule, ReminderDue):
            proposal, reminder = (
                container.task_proposal_repository.accept_proposal_as_reminder(
                    proposal_id
                )
            )
            return {
                "proposal": task_proposal_json(proposal),
                "reminder": reminder_json(reminder),
            }
        proposal, task = container.task_proposal_repository.accept_proposal(
            proposal_id
        )
        return {
            "proposal": task_proposal_json(proposal),
            "task": task_json(task),
        }

    @app.get("/artifacts")
    async def list_artifacts(include_deleted: bool = False) -> dict[str, object]:
        records = container.artifact_repository.list_artifacts(
            include_deleted=include_deleted
        )
        return {"items": [artifact_json(item) for item in records]}

    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> dict[str, object]:
        artifact = container.artifact_repository.get_artifact(artifact_id)
        version = container.artifact_repository.get_current_version(artifact_id)
        references = container.reference_resolver.resolve(
            version.source_labels,
            conversation_id=version.source_conversation_id,
        )
        return {
            "artifact": artifact_json(artifact),
            "currentVersion": artifact_version_json(
                version, source_references=references
            ),
        }

    @app.get("/artifacts/{artifact_id}/versions")
    async def list_artifact_versions(artifact_id: str) -> dict[str, object]:
        versions = container.artifact_repository.list_versions(artifact_id)
        items = []
        for version in versions:
            references = container.reference_resolver.resolve(
                version.source_labels,
                conversation_id=version.source_conversation_id,
            )
            items.append(
                artifact_version_json(version, source_references=references)
            )
        return {"items": items}

    @app.get("/artifacts/{artifact_id}/export")
    async def export_artifact(
        artifact_id: str, format: str = "markdown"
    ) -> Response:
        artifact = container.artifact_repository.get_artifact(artifact_id)
        version = container.artifact_repository.get_current_version(artifact_id)
        try:
            data, media_type = build_export(artifact, version, fmt=format)
            filename = export_filename(artifact, fmt=format)
        except ExportError as error:
            return _error_response(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
            )
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Disposition": content_disposition_header(filename)
            },
        )

    @app.post("/artifacts/{artifact_id}/rollback")
    async def rollback_artifact(
        artifact_id: str, body: RollbackArtifactBody
    ) -> dict[str, object]:
        snapshot = container.artifact_repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=body.targetOrdinal,
            source_conversation_id=body.sourceConversationId,
            source_turn_id=body.sourceTurnId,
            note=body.note,
        )
        artifact = snapshot.artifact
        if artifact.storage_path and body.sourceConversationId:
            binding = container.artifact_file_store.binding_for(
                body.sourceConversationId
            )
            if binding is not None:
                try:
                    container.artifact_file_store.restore(
                        binding,
                        artifact.storage_path,
                        snapshot.current_version.content,
                    )
                except Exception:  # noqa: BLE001 文件恢复失败不阻断 DB
                    pass
        return {
            "artifact": artifact_json(snapshot.artifact),
            "currentVersion": artifact_version_json(snapshot.current_version),
        }

    @app.get("/conversations/{conversation_id}/artifact-proposals")
    async def list_artifact_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.artifact_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [artifact_proposal_json(item) for item in proposals]}

    @app.get("/conversations/{conversation_id}/workspace")
    async def get_conversation_workspace(conversation_id: str) -> dict[str, object]:
        container.chat_repository.get_conversation(conversation_id)
        artifacts = container.artifact_repository.list_artifacts_for_conversation(
            conversation_id
        )
        proposals = container.artifact_proposal_repository.list_proposals(
            conversation_id=conversation_id
        )
        artifact_items = [artifact_json(item) for item in artifacts]
        proposal_items = [artifact_proposal_json(item) for item in proposals]
        return {
            "conversationId": conversation_id,
            "visible": bool(artifact_items or proposal_items),
            "artifacts": artifact_items,
            "pendingProposals": proposal_items,
        }

    @app.get("/api/v2/conversations/{conversation_id}/lanes")
    async def list_runtime_v2_lanes(
        conversation_id: str,
        includeArchived: bool = False,
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        lanes = container.runtime_v2_gateway.list_lanes(
            target_conversation_id,
            include_archived=includeArchived,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        active_lane_id = pointer.active_lane_id if pointer is not None else None
        return {
            "conversationId": target_conversation_id,
            "activeLaneId": active_lane_id,
            "items": tuple(
                _runtime_v2_lane_json(lane, active_lane_id=active_lane_id)
                for lane in lanes
            ),
        }

    @app.post("/api/v2/conversations/{conversation_id}/lanes", status_code=201)
    async def create_runtime_v2_lane(
        conversation_id: str,
        body: RuntimeV2CreateLaneBody,
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        source_lane_id = body.sourceLaneId or pointer.active_lane_id
        base_entry_id = body.baseEntryId
        if base_entry_id is None:
            source_lane = container.runtime_v2_repository.get_lane(source_lane_id)
            base_entry_id = source_lane.leaf_entry_id
            if base_entry_id is None:
                raise ConflictError("Source lane has no base entry")
        result = await container.runtime_v2_gateway.create_lane_branch(
            conversation_id=target_conversation_id,
            source_lane_id=source_lane_id,
            base_entry_id=base_entry_id,
            kind=LaneKind.PERSISTENT_BRANCH,
            display_name=body.displayName,
        )
        return {
            "lane": _runtime_v2_lane_json(result.lane),
            "sourceLane": _runtime_v2_lane_json(result.source_lane),
            "baseEntryId": result.base_entry_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
        }

    @app.post("/api/v2/lanes/{lane_id}/promote")
    async def promote_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lane = container.runtime_v2_repository.get_lane(lane_id)
        result = await container.runtime_v2_gateway.promote_lane(
            conversation_id=lane.conversation_id,
            target_lane_id=lane_id,
        )
        return {
            "lane": _runtime_v2_lane_json(result.promoted_lane),
            "previousMainLane": _runtime_v2_lane_json(result.previous_main_lane),
            "activeLaneId": result.pointer.active_lane_id,
        }

    @app.patch("/api/v2/lanes/{lane_id}")
    async def rename_runtime_v2_lane(
        lane_id: str,
        body: RuntimeV2RenameLaneBody,
    ) -> dict[str, object]:
        lane = await container.runtime_v2_gateway.rename_lane(
            lane_id,
            body.displayName,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            lane.conversation_id
        )
        return {
            "lane": _runtime_v2_lane_json(
                lane,
                active_lane_id=pointer.active_lane_id if pointer is not None else None,
            )
        }

    @app.post("/api/v2/lanes/{lane_id}/archive")
    async def archive_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lanes = await container.runtime_v2_gateway.archive_lane(lane_id)
        active_lane_id = None
        if lanes:
            pointer = container.runtime_v2_repository.get_conversation_pointer(
                lanes[0].conversation_id
            )
            active_lane_id = pointer.active_lane_id if pointer is not None else None
        return {
            "items": tuple(
                _runtime_v2_lane_json(lane, active_lane_id=active_lane_id)
                for lane in lanes
            )
        }

    @app.post("/api/v2/lanes/{lane_id}/restore")
    async def restore_runtime_v2_lane(lane_id: str) -> dict[str, object]:
        lanes = await container.runtime_v2_gateway.restore_lane(lane_id)
        active_lane_id = None
        if lanes:
            pointer = container.runtime_v2_repository.get_conversation_pointer(
                lanes[0].conversation_id
            )
            active_lane_id = pointer.active_lane_id if pointer is not None else None
        return {
            "items": tuple(
                _runtime_v2_lane_json(lane, active_lane_id=active_lane_id)
                for lane in lanes
            )
        }

    @app.post(
        "/api/v2/conversations/{conversation_id}/temporary-conversations",
        status_code=201,
    )
    async def create_runtime_v2_temporary_conversation(
        conversation_id: str,
        body: RuntimeV2CreateTemporaryConversationBody,
    ) -> dict[str, object]:
        source_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        temporary_conversation_id, lane = (
            await container.runtime_v2_gateway.create_temporary_conversation(
                source_conversation_id=source_conversation_id,
                source_lane_id=body.sourceLaneId,
                source_leaf_entry_id=body.sourceLeafEntryId,
                title=body.title,
            )
        )
        conversation = container.chat_repository.get_conversation(
            temporary_conversation_id
        )
        return {
            "conversation": conversation_json(conversation),
            "lane": _runtime_v2_lane_json(lane, active_lane_id=lane.id),
        }

    @app.post("/api/v2/temporary-conversations/{conversation_id}/promote")
    async def promote_runtime_v2_temporary_conversation(
        conversation_id: str,
    ) -> dict[str, object]:
        await container.runtime_v2_gateway.promote_temporary_conversation(
            conversation_id
        )
        return {
            "conversation": conversation_json(
                container.chat_repository.get_conversation(conversation_id)
            )
        }

    @app.delete(
        "/api/v2/temporary-conversations/{conversation_id}",
        status_code=204,
    )
    async def delete_runtime_v2_temporary_conversation(
        conversation_id: str,
    ) -> Response:
        await container.runtime_v2_gateway.delete_temporary_conversation(
            conversation_id
        )
        return Response(status_code=204)

    @app.get("/api/v2/runs/{run_id}/variants")
    async def list_runtime_v2_run_variants(run_id: str) -> dict[str, object]:
        variants = container.runtime_v2_gateway.list_run_variants(run_id)
        return {
            "runId": run_id,
            "siblingGroupId": variants[0].sibling_group_id if variants else None,
            "items": tuple(_runtime_v2_run_variant_json(variant) for variant in variants),
        }

    @app.post("/api/v2/runs/{run_id}/regenerate", status_code=202)
    async def regenerate_runtime_v2_run(run_id: str) -> dict[str, object]:
        result = await container.runtime_v2_gateway.regenerate_run(run_id)
        return {
            "oldRunId": result.old_run_id,
            "newRunId": result.new_run_id,
            "laneId": result.lane_id,
            "triggerEntryId": result.trigger_entry_id,
            "siblingGroupId": result.sibling_group_id,
            "eventsUrl": (
                f"/api/v2/conversations/"
                f"{container.runtime_v2_repository.get_run(run_id).conversation_id}/events"
            ),
        }

    @app.post("/api/v2/runs/{run_id}/select")
    async def select_runtime_v2_run_variant(run_id: str) -> dict[str, object]:
        selected = await container.runtime_v2_gateway.select_run_variant(run_id)
        return {
            "runId": selected.id,
            "assistantEntryId": selected.assistant_entry_id,
            "isActiveVariant": selected.is_active_variant,
        }

    @app.get("/api/v2/runtime")
    async def get_runtime_v2_runtime_status() -> dict[str, object]:
        return _runtime_v2_global_runtime_json(
            container.runtime_v2_selection_service.global_status()
        )

    @app.get("/api/v2/conversations/{conversation_id}/runtime")
    async def get_runtime_v2_conversation_runtime(
        conversation_id: str,
    ) -> dict[str, object]:
        status = container.runtime_v2_selection_service.describe(conversation_id)
        return _runtime_v2_conversation_runtime_json(status)

    @app.post("/api/v2/conversations/{conversation_id}/runtime")
    async def set_runtime_v2_conversation_runtime(
        conversation_id: str,
        body: RuntimeV2ConversationRuntimeBody,
    ) -> dict[str, object]:
        status = container.runtime_v2_selection_service.set_override(
            conversation_id,
            runtime=body.runtime,
        )
        return _runtime_v2_conversation_runtime_json(status)

    @app.get("/api/v2/conversations/{conversation_id}/memories")
    async def list_runtime_v2_memories(
        conversation_id: str,
        lane_id: str = Query(...),
        run_id: Optional[str] = Query(None),
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        memories = container.runtime_v2_gateway.list_memories(
            conversation_id=target_conversation_id,
            lane_id=lane_id,
            run_id=run_id,
        )
        return {
            "conversationId": target_conversation_id,
            "laneId": lane_id,
            "items": tuple(_runtime_v2_memory_json(memory) for memory in memories),
        }

    @app.post("/api/v2/conversations/{conversation_id}/memories", status_code=201)
    async def create_runtime_v2_memory(
        conversation_id: str,
        body: RuntimeV2MemoryBody,
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            target_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 lane pointer")
        lane_id = body.laneId or pointer.active_lane_id
        memory = container.runtime_v2_gateway.create_lane_memory(
            conversation_id=target_conversation_id,
            lane_id=lane_id,
            kind=body.kind,
            content=body.content,
            source_entry_id=body.sourceEntryId,
        )
        return {"memory": _runtime_v2_memory_json(memory)}

    @app.post("/api/v2/runs/{run_id}/memories", status_code=201)
    async def create_runtime_v2_run_memory(
        run_id: str,
        body: RuntimeV2RunMemoryBody,
    ) -> dict[str, object]:
        run = container.runtime_v2_repository.get_run(run_id)
        memory = container.runtime_v2_gateway.create_run_memory(
            conversation_id=run.conversation_id,
            run_id=run_id,
            kind=body.kind,
            content=body.content,
            source_entry_id=body.sourceEntryId,
        )
        return {"memory": _runtime_v2_memory_json(memory)}

    @app.post("/api/v2/memories/{memory_id}/promotions", status_code=201)
    async def create_runtime_v2_memory_promotion(
        memory_id: str,
        body: RuntimeV2MemoryPromotionBody,
    ) -> dict[str, object]:
        promotion = container.runtime_v2_gateway.create_memory_promotion(
            memory_id=memory_id,
            target_scope=MemoryScope(body.targetScope),
            target_lane_id=body.targetLaneId,
        )
        return {"promotion": _runtime_v2_memory_promotion_json(promotion)}

    @app.get("/api/v2/conversations/{conversation_id}/memory-promotions")
    async def list_runtime_v2_memory_promotions(
        conversation_id: str,
        include_resolved: bool = Query(False),
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        promotions = container.runtime_v2_gateway.list_memory_promotions(
            conversation_id=target_conversation_id,
            include_resolved=include_resolved,
        )
        return {
            "conversationId": target_conversation_id,
            "items": tuple(
                _runtime_v2_memory_promotion_json(promotion)
                for promotion in promotions
            ),
        }

    @app.post("/api/v2/memory-promotions/{promotion_id}/resolve")
    async def resolve_runtime_v2_memory_promotion(
        promotion_id: str,
        body: RuntimeV2MemoryPromotionResolveBody,
    ) -> dict[str, object]:
        promotion, memory = container.runtime_v2_gateway.resolve_memory_promotion(
            promotion_id,
            accept=body.decision == "accept",
        )
        return {
            "promotion": _runtime_v2_memory_promotion_json(promotion),
            "memory": (
                _runtime_v2_memory_json(memory)
                if memory is not None
                else None
            ),
        }

    @app.post("/api/v2/conversations/{conversation_id}/messages", status_code=202)
    async def create_runtime_v2_message(
        conversation_id: str,
        body: RuntimeV2MessageBody,
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=True,
        )
        if len(body.content) > container.settings.max_message_characters:
            raise ApiRequestError(
                "message_too_large",
                "消息内容超过本地配置允许的长度。",
                status_code=413,
            )
        handle = await container.runtime_v2_gateway.send(
            target_conversation_id,
            body.content,
            lane_id=body.laneId,
        )
        return {
            "conversationId": handle.conversation_id,
            "laneId": handle.lane_id,
            "runId": handle.run_id,
            "userMessageId": handle.user_message_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
        }

    @app.get("/api/v2/conversations/{conversation_id}/snapshot")
    async def get_runtime_v2_snapshot(
        conversation_id: str,
        lane_id: Optional[str] = Query(default=None),
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        return container.runtime_v2_gateway.snapshot(
            target_conversation_id,
            lane_id=lane_id,
        )

    @app.get("/api/v2/conversations/{conversation_id}/recovery")
    async def get_runtime_v2_recovery(
        conversation_id: str,
    ) -> dict[str, object]:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        reports = container.runtime_v2_gateway.recovery_reports(
            target_conversation_id
        )
        return {
            "conversationId": target_conversation_id,
            "interruptedRuns": tuple(
                {
                    "runId": report.record.id,
                    "status": report.record.status.value,
                    "classification": report.classification.value,
                    "action": report.action.value,
                    "findings": tuple(
                        {
                            "reason": finding.reason.value,
                            "message": finding.message,
                            "modelTurnId": finding.model_turn_id,
                            "toolExecutionId": finding.tool_execution_id,
                        }
                        for finding in report.findings
                    ),
                }
                for report in reports
            ),
        }

    @app.post("/api/v2/runs/{run_id}/recovery")
    async def resolve_runtime_v2_recovery(
        run_id: str,
        body: RuntimeV2RecoveryBody,
    ) -> dict[str, object]:
        result = await container.runtime_v2_gateway.resolve_recovery(
            run_id,
            retry=body.action == "retry",
        )
        return {
            "runId": result.run_id,
            "action": result.action,
            "laneId": result.lane_id,
            "newRunId": result.new_run_id,
        }

    @app.get("/api/v2/conversations/{conversation_id}/events")
    async def get_runtime_v2_events(
        conversation_id: str,
        after_seq: int = Query(0, ge=0),
        lane_id: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        target_conversation_id = _resolve_runtime_v2_conversation(
            container,
            conversation_id,
            write=False,
        )
        initial_events = container.runtime_v2_gateway.project_events(
            target_conversation_id
        )
        latest_event_seq = initial_events[-1].event_seq if initial_events else 0
        if after_seq > latest_event_seq:
            raise ApiRequestError(
                "invalid_after_seq",
                "after_seq 超过当前会话事件游标。",
            )

        async def stream() -> AsyncIterator[str]:
            snapshot = container.runtime_v2_gateway.snapshot(
                target_conversation_id,
                lane_id=lane_id,
            )
            snapshot_event_seq = int(snapshot["lastEventSeq"])
            snapshot_payload = json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield (
                f"id: snapshot-{conversation_id}-{snapshot_event_seq}\n"
                "event: conversation.snapshot_ready\n"
                f"data: {snapshot_payload}\n\n"
            )
            cursor = snapshot_event_seq
            last_heartbeat = asyncio.get_running_loop().time()
            poll_interval = min(0.05, container.settings.heartbeat_seconds)
            while True:
                events = container.runtime_v2_gateway.project_events(
                    target_conversation_id
                )
                if lane_id is not None:
                    events = tuple(
                        event for event in events if event.lane_id == lane_id
                    )
                emitted = False
                for event in events:
                    if event.event_seq <= cursor:
                        continue
                    cursor = event.event_seq
                    emitted = True
                    yield _runtime_v2_product_sse(event)
                if not container.runtime_v2_gateway.has_active_run(
                    target_conversation_id
                ):
                    final_events = container.runtime_v2_gateway.project_events(
                        target_conversation_id
                    )
                    if lane_id is not None:
                        final_events = tuple(
                            event
                            for event in final_events
                            if event.lane_id == lane_id
                        )
                    for event in final_events:
                        if event.event_seq <= cursor:
                            continue
                        cursor = event.event_seq
                        yield _runtime_v2_product_sse(event)
                    return
                now = asyncio.get_running_loop().time()
                if (
                    not emitted
                    and now - last_heartbeat >= container.settings.heartbeat_seconds
                ):
                    last_heartbeat = now
                    yield ": heartbeat\n\n"
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v2/runs/{run_id}/steer")
    async def steer_runtime_v2_run(
        run_id: str,
        body: RuntimeV2SteerBody,
    ) -> dict[str, object]:
        accepted = await container.runtime_v2_gateway.steer(
            run_id,
            body.content,
        )
        return {"runId": run_id, "accepted": accepted}

    @app.post("/api/v2/runs/{run_id}/cancel")
    async def cancel_runtime_v2_run(run_id: str) -> dict[str, object]:
        accepted = await container.runtime_v2_gateway.cancel(run_id)
        return {"runId": run_id, "accepted": accepted}

    @app.post("/api/v2/approvals/{approval_id}")
    async def resolve_runtime_v2_approval(
        approval_id: str,
        body: ResolveApprovalBody,
    ) -> dict[str, object]:
        decision = (
            ToolApprovalDecision.APPROVE
            if body.decision == "approve"
            else ToolApprovalDecision.DENY
        )
        resolved = await container.runtime_v2_gateway.resolve_approval(
            approval_id,
            decision,
        )
        return {"approvalId": approval_id, "resolved": resolved}

    @app.post("/artifact-proposals/{proposal_id}/resolve")
    async def resolve_artifact_proposal(
        proposal_id: str, body: ResolveArtifactProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.artifact_proposal_repository.reject_proposal(
                proposal_id
            )
            return {"proposal": artifact_proposal_json(proposal)}
        proposal, artifact = container.artifact_proposal_repository.accept_proposal(
            proposal_id
        )
        return {
            "proposal": artifact_proposal_json(proposal),
            "artifact": artifact_json(artifact),
        }

    @app.post("/conversations/{conversation_id}/turns", status_code=202)
    async def create_turn(
        conversation_id: str,
        body: CreateTurnBody,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> dict[str, object]:
        runtime_status = container.runtime_v2_selection_service.describe(
            conversation_id
        )
        if runtime_status.effective_runtime == "v2":
            raise ApiRequestError(
                "runtime_v2_selected",
                "该会话已选择 Runtime v2，请使用 v2 message API。",
                status_code=409,
            )
        _assert_v1_write_allowed(container, conversation_id)
        if len(body.content) > container.settings.max_message_characters:
            raise ApiRequestError(
                "message_too_large",
                "消息内容超过本地配置允许的长度。",
                status_code=413,
            )
        handle = await container.controller.submit(
            conversation_id=conversation_id,
            client_request_id=idempotency_key,
            content=body.content,
        )
        snapshot = container.chat_repository.get_turn(handle.turn_id)
        selected = response_variant_by_id(snapshot, handle.response_variant_id)
        return {
            "conversationId": handle.conversation_id,
            "turnId": handle.turn_id,
            "responseVariantId": handle.response_variant_id,
            "userMessageId": snapshot.user_message.id,
            "assistantMessageId": selected.assistant_message.id,
            "eventsUrl": f"/turns/{handle.turn_id}/events",
        }

    @app.get("/turns/{turn_id}")
    async def get_turn(turn_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_turn(turn_id)
        events = tuple(container.runtime_repository.list_events(turn_id))
        return compact_turn_snapshot_json(
            snapshot,
            events=events,
            pending_approval=container.runtime_repository.get_pending_approval(turn_id),
        )

    @app.get("/turns/{turn_id}/events")
    async def get_turn_events(
        turn_id: str,
        last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        container.chat_repository.get_turn(turn_id)
        after_sequence = _parse_last_event_id(turn_id, last_event_id)

        async def stream() -> AsyncIterator[str]:
            cursor = after_sequence
            async with container.broker.subscribe(turn_id) as queue:
                while True:
                    persisted = container.runtime_repository.list_events(
                        turn_id,
                        after_sequence=cursor,
                    )
                    for event in persisted:
                        if event.sequence <= cursor:
                            continue
                        cursor = event.sequence
                        yield _event_sse(event)

                    snapshot = container.chat_repository.get_turn(turn_id)
                    if snapshot.turn.status in TERMINAL_TURN_STATUSES:
                        return

                    try:
                        event = await asyncio.wait_for(
                            queue.get(),
                            timeout=container.settings.heartbeat_seconds,
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event.sequence > cursor:
                        cursor = event.sequence
                        yield _event_sse(event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/turns/{turn_id}/cancel")
    async def cancel_turn(turn_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_turn(turn_id)
        _assert_v1_write_allowed(container, snapshot.turn.conversation_id)
        await container.controller.cancel(turn_id=turn_id)
        events = tuple(container.runtime_repository.list_events(turn_id))
        return compact_turn_snapshot_json(
            snapshot,
            events=events,
            pending_approval=container.runtime_repository.get_pending_approval(turn_id),
        )

    @app.post("/approvals/{approval_id}")
    async def resolve_approval(
        approval_id: str,
        body: ResolveApprovalBody,
    ) -> dict[str, object]:
        turn_id = container.runtime_repository.get_approval_turn_id(approval_id)
        snapshot = container.chat_repository.get_turn(turn_id)
        _assert_v1_write_allowed(container, snapshot.turn.conversation_id)
        status = (
            ApprovalStatus.APPROVED
            if body.decision == "approve"
            else ApprovalStatus.DENIED
        )
        approval = await container.runtime.resolve_approval(
            approval_id=approval_id,
            status=status,
        )
        return approval_request_json(approval)

    async def create_variant(
        turn_id: str,
        idempotency_key: str,
        *,
        operation: str,
    ) -> dict[str, object]:
        snapshot = container.chat_repository.get_turn(turn_id)
        _assert_v1_write_allowed(container, snapshot.turn.conversation_id)
        if operation == "retry":
            handle = await container.controller.retry(
                turn_id=turn_id,
                command_request_id=idempotency_key,
            )
        else:
            handle = await container.controller.regenerate(
                turn_id=turn_id,
                command_request_id=idempotency_key,
            )
        snapshot = container.chat_repository.get_turn(handle.turn_id)
        selected = response_variant_by_id(snapshot, handle.response_variant_id)
        return {
            "conversationId": handle.conversation_id,
            "turnId": handle.turn_id,
            "responseVariantId": handle.response_variant_id,
            "assistantMessageId": selected.assistant_message.id,
            "eventsUrl": f"/turns/{handle.turn_id}/events",
        }

    @app.post("/turns/{turn_id}/retry", status_code=202)
    async def retry_turn(
        turn_id: str,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> dict[str, object]:
        return await create_variant(turn_id, idempotency_key, operation="retry")

    @app.post("/turns/{turn_id}/regenerate", status_code=202)
    async def regenerate_turn(
        turn_id: str,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> dict[str, object]:
        return await create_variant(turn_id, idempotency_key, operation="regenerate")

    @app.post("/turns/{turn_id}/response-variants/{variant_id}/select")
    async def select_variant(turn_id: str, variant_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_turn(turn_id)
        _assert_v1_write_allowed(container, snapshot.turn.conversation_id)
        snapshot = container.chat_repository.select_response_variant(
            turn_id=turn_id,
            variant_id=variant_id,
        )
        return turn_command_json(snapshot)

    return app
