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
from typing import TYPE_CHECKING, AsyncIterator, Callable, Literal, Optional

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
    BackgroundTaskSupervisor,
    FakeProvider,
    KnowledgeQueryRewriter,
    KnowledgeReranker,
    OpenAICompatibleProvider,
    P0ContextBuilder,
    RuntimeEvent,
    RuntimeEventBroker,
    UnconfiguredProvider,
)
from endless_task.runtime.provider import ModelProvider, ProviderError, ProviderMessage
from endless_task.runtime.provider_manager import ProviderManager
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    LaneKind,
    LaneRecord,
    MemoryScope,
    ProductRuntimeEventRecord,
    RunRecord,
    RunStatus,
    RuntimeV2MemoryPromotion,
    RuntimeV2MemoryRecord,
    RuntimeV2SessionGateway,
    ToolApprovalDecision,
    ToolExecutionLimits,
    UnattendedToolApprovalGate,
    product_event_json,
)
from endless_task.runtime_v2.compaction import RuntimeV2ContextCompactionService
from endless_task.runtime_v2.memory_quality import RuntimeV2MemoryQualityService
from endless_task.runtime_v2.metrics import RuntimeV2MetricsCollector
from endless_task.runtime_v2.trace_observer import (
    LedgerTraceObserver,
    default_run_reader,
    default_usage_exists,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.runtime_v2.run_trajectory import (
    FanoutTraceObserver,
    RunTrajectoryExporter,
    default_journal_reader,
)
# runtime_ledger implementation modules are imported by path (not through
# the package __init__, which stays protocol-only to avoid init cycles).
from endless_task.runtime_ledger.pricing import make_default_catalog
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.runtime_v2.plan_tool import UpdatePlanTool
from endless_task.runtime_v2.agent_kernel_adapter import RuntimeV2AgentKernel
from endless_task.delegation import (
    CoordinatorDelegationHandler,
    SpawnAgentLegacyTool,
    QueryAgentLegacyTool,
    CancelAgentLegacyTool,
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
from endless_task.workspace_runtime.visibility import workspace_tool_filter
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
    SqliteMemoryProposalRepository,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
    SqliteMemoryRepository,
    SqlitePreferencesRepository,
    SqliteTextFileRepository,
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
    ProviderModel,
    ProviderProfileDraft,
    SqliteProviderProfileRepository,
)
from endless_task.storage.provider_secret_store import ProviderSecretStore
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.storage.sqlite_knowledge_repository import scope_tier
from endless_task.skills import Skill, SkillService, build_available_skills_prompt
from endless_task.tooling import ApprovalStatus, ToolApprovalMode, ToolRegistry

from .serialization import (
    artifact_json,
    artifact_proposal_json,
    artifact_version_json,
    memory_proposal_json,
    knowledge_proposal_json,
    knowledge_source_json,
    memory_record_json,
    workspace_json,
    conversation_json,
    conversation_snapshot_json,
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
    # v2 是唯一 runtime（14-runtime-single-mode）：旧 v1 引擎已删，无运行时切换。
    provider_name: str = "fake"
    model: str = "fake-model"
    base_url: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    provider_timeout_seconds: float = 60.0
    system_prompt: str = "你是 Endless Task，一个可靠、简洁的个人助手。\n- 能直接回答的问题用自然语言直接回答，不要调用工具。\n- 信息不足时先向用户追问关键信息，不要猜测。\n- 只有问题确实需要会话附件内容时才调用文件工具。\n- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。\n- 用户要求产出文档/文件（如写 README、报告、脚本）时，读完所需材料后应立即调用写入工具或直接给出完整结果，不要无休止地继续收集资料；读完即动手。"
    system_prompt_version: str = "p1-v1"
    context_window_tokens: int = 32_768
    max_output_tokens: int = 2_048
    summary_token_limit: int = 1_024
    max_concurrent_model_calls: int = 2
    max_concurrent_task_runs: int = 1
    max_tool_calls_per_turn: int = 8
    max_concurrent_tool_calls: int = 4
    max_tool_argument_bytes: int = 64 * 1024
    max_total_tool_argument_bytes: int = 256 * 1024
    max_tool_argument_depth: int = 32
    max_tool_argument_nodes: int = 10_000
    agent_timeout_seconds: float = 120.0
    approval_timeout_seconds: float = 1800.0
    max_message_characters: int = 100_000
    max_file_bytes: int = 1_000_000
    max_files_per_conversation: int = 10
    heartbeat_seconds: float = 15.0
    memory_proposals_enabled: bool = True
    memory_marker_gate_enabled: bool = True
    memory_auto_fact: bool = False
    context_compaction_enabled: bool = True
    # Agent Platform v2 tool path; default on after AP-107 flip. Illegal env values fail startup.
    tool_platform_v2_enabled: bool = True
    # M2 context engine v2 cutover; default on after validation (initial
    # context pruning under budget pressure; under-budget behavior is
    # byte-identical to legacy).
    context_engine_v2_enabled: bool = True
    # M4A read-only delegation mode; default "readonly" after real-provider
    # validation (2026-09-05 deepseek E2E: child run + concurrent batch +
    # delegate gate verified, 06 §27-29). "0" disables delegation tools.
    # "isolated_write" (M4B) is not implemented yet and fails startup
    # rather than silently degrading to read-only (no silent downgrade).
    delegation_mode: str = "readonly"
    # M3A RS-1 (G1-2): provider retry mode (05 §RS-1). "0" (default) =
    # shadow only: evaluator observes and records retry decisions, never
    # retries (max_attempts=0); "1" enables bounded auto-retry of
    # pre-emission retryable provider errors. Illegal env values fail
    # startup.
    provider_retry_mode: str = "0"
    # M5 SK-1b: enable skill:// locator resolution for read_skill_file and
    # locator-based prompt exposure. Default off keeps the legacy absolute
    # path behavior unchanged. Illegal env values fail startup.
    skill_packages_enabled: bool = False
    # M6 W6-2/W6-8: runtime trace mode (08 §OE-1). Default "all" after
    # real-provider validation (2026-09-05 deepseek E2E: usage rows +
    # run/model-turn spans + trajectory export all verified). "0" disables
    # all trace wiring; errors|sampled|all enable it. Illegal env values
    # fail startup.
    runtime_trace_mode: str = "all"
    # M6 W6-4: OTLP export mode (08 §OE-5). "0" (default) = exporter not
    # assembled; otlp-http exports terminal-run journal events to the
    # endpoint below. Illegal env values fail startup.
    otel_export_mode: str = "0"
    otel_export_endpoint: str = "http://127.0.0.1:4318/v1/traces"
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
    # M3B slice B (方案 A 兑现): a v2 run that reaches terminal FAILED
    # auto-restores its workspace checkpoints (guards keep user edits).
    # Default on after run-level checkpoint landed (slice A) + guarded
    # restore E2E; "0" disables so ops keep partial state on failure.
    run_auto_restore_enabled: bool = True
    # M3B slice F/F2 enforcement: execution backend for unattended executors'
    # workspace fs mutations. "local" (default since F2) routes unattended
    # write/delete through the ExecutionEnvironment seam (containment
    # re-check + run-scoped ledger + single execution boundary); "container"
    # is the same seam for file mutations (container's isolation difference
    # is process-bound, which unattended cannot reach today); "" disables
    # enforcement (current direct tool execution). Illegal env values fail
    # startup.
    execution_backend_mode: str = "local"  # values: "" | local | container

    def __post_init__(self) -> None:
        if self.config_version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}"
            )
        if self.provider_timeout_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("Timeout values must be positive")
        if self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("Context window must be larger than max output tokens")
        if self.summary_token_limit < 0:
            raise ValueError("Summary token limit cannot be negative")
        if self.max_concurrent_model_calls <= 0:
            raise ValueError("Maximum concurrent model calls must be positive")
        tool_limit_values = (
            self.max_tool_calls_per_turn,
            self.max_concurrent_tool_calls,
            self.max_tool_argument_bytes,
            self.max_total_tool_argument_bytes,
            self.max_tool_argument_depth,
            self.max_tool_argument_nodes,
        )
        if any(value <= 0 for value in tool_limit_values):
            raise ValueError("Tool limits must be positive")
        if self.max_tool_argument_bytes > self.max_total_tool_argument_bytes:
            raise ValueError(
                "Per-call tool argument limit cannot exceed the per-turn limit"
            )
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
            memory_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_PROPOSALS", "1")
            ),
            memory_marker_gate_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_MARKER_GATE", "1")
            ),
            memory_auto_fact=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_AUTO_FACT", "0")
            ),
            context_compaction_enabled=_parse_flag(
                env.get("ENDLESS_TASK_CONTEXT_COMPACTION", "1")
            ),
            tool_platform_v2_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_TOOL_PLATFORM_V2", "1"),
                name="ENDLESS_TASK_TOOL_PLATFORM_V2",
            ),
            context_engine_v2_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_CONTEXT_ENGINE_V2", "1"),
                name="ENDLESS_TASK_CONTEXT_ENGINE_V2",
            ),
            delegation_mode=_parse_delegation_mode(
                env.get("ENDLESS_TASK_DELEGATION", "readonly"),
            ),
            provider_retry_mode=_parse_strict_mode(
                env.get("ENDLESS_TASK_PROVIDER_RETRY_V2", "0"),
                name="ENDLESS_TASK_PROVIDER_RETRY_V2",
            ),
            skill_packages_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_SKILL_PACKAGES", "0"),
                name="ENDLESS_TASK_SKILL_PACKAGES",
            ),
            runtime_trace_mode=_parse_runtime_trace_mode(
                env.get("ENDLESS_TASK_RUNTIME_TRACE", "all"),
            ),
            otel_export_mode=_parse_otel_export_mode(
                env.get("ENDLESS_TASK_OTEL_EXPORT", "0"),
            ),
            run_auto_restore_enabled=_parse_flag(
                env.get("ENDLESS_TASK_RUN_AUTO_RESTORE", "1")
            ),
            execution_backend_mode=_parse_execution_backend_mode(
                env.get("ENDLESS_TASK_EXECUTION_BACKEND", "local")
            ),
            otel_export_endpoint=env.get(
                "ENDLESS_TASK_OTEL_ENDPOINT",
                "http://127.0.0.1:4318/v1/traces",
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
                "你是 Endless Task，一个可靠、简洁的个人助手。\n- 能直接回答的问题用自然语言直接回答，不要调用工具。\n- 信息不足时先向用户追问关键信息，不要猜测。\n- 只有问题确实需要会话附件内容时才调用文件工具。\n- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。\n- 用户要求产出文档/文件（如写 README、报告、脚本）时，读完所需材料后应立即调用写入工具或直接给出完整结果，不要无休止地继续收集资料；读完即动手。",
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
            max_tool_calls_per_turn=int(
                env.get("ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN", "8")
            ),
            max_concurrent_tool_calls=int(
                env.get("ENDLESS_TASK_MAX_CONCURRENT_TOOL_CALLS", "4")
            ),
            max_tool_argument_bytes=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_BYTES", "65536")
            ),
            max_total_tool_argument_bytes=int(
                env.get("ENDLESS_TASK_MAX_TOTAL_TOOL_ARGUMENT_BYTES", "262144")
            ),
            max_tool_argument_depth=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_DEPTH", "32")
            ),
            max_tool_argument_nodes=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_NODES", "10000")
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
    provider_secret_store: ProviderSecretStore
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
    tool_registry: ToolRegistry
    runtime_v2_repository: SqliteRuntimeV2Repository
    runtime_v2_memory_repository: SqliteRuntimeV2MemoryRepository
    runtime_v2_memory_quality_service: RuntimeV2MemoryQualityService
    runtime_v2_gateway: RuntimeV2SessionGateway
    delegation_handler: Optional[CoordinatorDelegationHandler] = None
    runtime_v2_trace_observer: Optional[object] = None
    runtime_v2_trajectory_exporter: Optional[RunTrajectoryExporter] = None
    runtime_v2_span_recorder: Optional[SqliteRuntimeLedger] = None
    run_checkpoint_coordinator: Optional[RunCheckpointCoordinator] = None
    # M3B slice F enforcement (for ops/tests): registry view used by the
    # unattended executors + configured mode.
    unattended_tool_registry: Optional[ToolRegistry] = None
    execution_backend_mode: str = ""


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    status: Optional[ConversationStatus] = None
    providerProfileId: Optional[str] = None
    modelOverride: Optional[str] = None
    workspaceId: Optional[str] = None


class CreateBranchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forkTurnId: Optional[str] = None


class RuntimeV2MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    laneId: Optional[str] = None


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

    # Kept optional for request compatibility; the server always snapshots the
    # current main lane and its complete leaf path.
    sourceLaneId: Optional[str] = None
    sourceLeafEntryId: Optional[str] = None
    title: Optional[str] = None


class RuntimeV2MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    laneId: Optional[str] = None
    sourceEntryId: Optional[str] = None
    expiresAt: Optional[str] = None


class RuntimeV2RunMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    sourceEntryId: Optional[str] = None
    expiresAt: Optional[str] = None


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
    defaultModel: str = ""
    baseUrl: str = ""
    apiKey: Optional[str] = None
    apiKeyRef: str = ""
    timeoutSeconds: float = 60.0
    enabled: bool = True


class ProviderProfilePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    defaultModel: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    apiKeyRef: Optional[str] = None
    timeoutSeconds: Optional[float] = None
    enabled: Optional[bool] = None


class ProviderDefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profileId: str


class ProviderModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modelId: str
    displayName: str = ""


class ProviderModelPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ProviderDefaultModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modelId: str


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


def _tool_platform_v2_profile_name() -> str:
    """Name of the calibrated provider profile used by the v2 tool path."""
    from endless_task.tool_platform import create_openai_compatible_profile

    return create_openai_compatible_profile().name


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


def _resolve_runtime_v2_conversation(
    container: AppContainer,
    conversation_id: str,
    *,
    write: bool,
) -> str:
    del write
    # 14 B2: v2 sole runtime — conversation id is its own tree (migration
    # mapping tables removed with the migration machinery).
    return conversation_id


def _parse_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_strict_flag(value: str, *, name: str) -> bool:
    """Strict boolean env parsing: any value outside 0/1 fails startup."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 只允许 0 或 1")


def _parse_delegation_mode(value: str) -> str:
    """Strict delegation mode parsing (06 §9): 0 | readonly | isolated_write.

    ``isolated_write`` (M4B) is accepted from P2a on; startup additionally
    requires the execution-backend enforcement seam to be enabled (checked in
    the composition root) — never silently degrades to read-only.
    """
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "readonly"}:
        return "readonly"
    if normalized == "isolated_write":
        return "isolated_write"
    raise ValueError("ENDLESS_TASK_DELEGATION 只允许 0 | readonly | isolated_write")


def _parse_execution_backend_mode(value: str) -> str:
    """Strict execution-backend mode parsing (M3B slice F): 0|local|container.

    "" = enforcement off (current direct tool behavior). ``local``/``container``
    both route unattended fs mutations through the ExecutionEnvironment (the
    container backend executes file mutations locally too — its isolation
    difference applies to process execution, which unattended runs cannot
    reach), so either value is an explicit opt-in to the enforcement seam;
    illegal values fail startup.
    """
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", ""}:
        return ""
    if normalized in {"1", "true", "yes", "on", "local"}:
        return "local"
    if normalized == "container":
        return "container"
    raise ValueError("ENDLESS_TASK_EXECUTION_BACKEND 只允许 0|local|container")


def _parse_strict_mode(value: str, *, name: str) -> str:
    """Strict 0|1 mode parsing: any value outside 0/1 fails startup."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return "1"
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    raise ValueError(f"{name} 只允许 0 或 1")


def _parse_runtime_trace_mode(value: str) -> str:
    """Strict runtime trace mode parsing (08 §OE-1): 0|errors|sampled|all."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"errors"}:
        return "errors"
    if normalized in {"sampled"}:
        return "sampled"
    if normalized in {"1", "true", "yes", "on", "all"}:
        return "all"
    raise ValueError("ENDLESS_TASK_RUNTIME_TRACE 只允许 0|errors|sampled|all")


def _trace_runtime_version() -> str:
    from endless_task import __version__

    return __version__ or "0.0.0"


def _parse_otel_export_mode(value: str) -> str:
    """Strict OTLP export mode parsing (08 §OE-5): 0|otlp-http."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", ""}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "otlp", "otlp-http"}:
        return "otlp-http"
    raise ValueError("ENDLESS_TASK_OTEL_EXPORT 只允许 0|otlp-http")


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
    provider_secret_store = ProviderSecretStore(
        settings.database_path.parent / "provider-secrets.json"
    )
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
    embedder: Optional[Embedder] = None
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
    runtime_v2_memory_quality_service = RuntimeV2MemoryQualityService(
        memory_repository=runtime_v2_memory_repository,
        embedder=embedder,
    )
    runtime_v2_metrics_collector = RuntimeV2MetricsCollector()
    # M3A RS-1 (G1-2): provider retry wiring. Default mode "0" = shadow
    # only: the evaluator observes provider failures and the metrics
    # collector records every decision; auto-retry is off (max_attempts=0
    # per 05 §RS-1). Mode "1" enables bounded auto-retry of pre-emission
    # retryable errors.
    from endless_task.reliability.retry_runtime import (
        ProviderRetryConfig,
        ProviderRetryEvaluator,
    )

    runtime_v2_provider_retry_evaluator: Optional[ProviderRetryEvaluator] = None
    if settings.provider_retry_mode != "0":
        runtime_v2_provider_retry_evaluator = ProviderRetryEvaluator(
            ProviderRetryConfig(
                max_attempts=(
                    1 if settings.provider_retry_mode == "1" else 0
                ),
                max_total_retry_delay=30.0,
                deadline_seconds=min(30.0, settings.agent_timeout_seconds or 30.0),
            )
        )
    # M3B run-level (方案 A): shared per-run workspace checkpoint
    # coordinator. Every fs/shell write snapshots the workspace before its
    # first side effect; the FileMutationLedger records agent changes so a
    # later restore never overwrites user edits. Store + ledger live under
    # the data directory. Constructed before the run observers/executors so
    # M3B slice B's failure auto-restore can share the same instance.
    checkpoint_store = settings.database_path.parent / "checkpoints"
    mutation_ledger = FileMutationLedger(
        settings.database_path.parent / "file-mutations.jsonl"
    )
    run_checkpoint_coordinator = RunCheckpointCoordinator(
        store_root=checkpoint_store,
        ledger=mutation_ledger,
    )
    # M6 W6-2/W6-3: runtime trace wiring (flag ENDLESS_TASK_RUNTIME_TRACE,
    # default "0"). When enabled, terminal-run usage is mirrored into the
    # SQLite trace ledger (08 §17/§24) and failed runs auto-export a
    # trajectory bundle (08 §OE-3/§25). "errors"|"sampled"|"all" all
    # enable both (usage rows are ordinary observability; the sampling
    # distinction applies to span events in later slices).
    runtime_v2_trace_observer: Optional[LedgerTraceObserver] = None
    runtime_v2_trajectory_exporter: Optional[RunTrajectoryExporter] = None
    trace_fanout_members: list[object] = []
    runtime_v2_span_recorder: Optional[SqliteRuntimeLedger] = None
    if settings.runtime_trace_mode != "0":
        trace_ledger = SqliteRuntimeLedger(database)
        runtime_v2_span_recorder = trace_ledger
        runtime_v2_trace_observer = LedgerTraceObserver(
            trace_ledger,
            catalog=make_default_catalog(),
            run_reader=default_run_reader(runtime_v2_repository),
            usage_exists=default_usage_exists(trace_ledger),
        )
        trace_fanout_members.append(runtime_v2_trace_observer)
        runtime_v2_trajectory_exporter = RunTrajectoryExporter(
            export_root=settings.database_path.parent / "v2_trajectory_exports",
            journal_reader=default_journal_reader(runtime_v2_repository),
            runtime_version=_trace_runtime_version(),
            provider=settings.provider_name,
            model=settings.model,
            config_fingerprint=settings.system_prompt_version,
            # usage_ledger: cost rows land in usage.jsonl so offline eval
            # can enforce 08 §10.3 approved cost budgets on the bundle.
            usage_ledger=trace_ledger,
        )
        trace_fanout_members.append(runtime_v2_trajectory_exporter)
    # M6 W6-4: optional OTLP export (08 §OE-5). Default off; when enabled
    # a journal bridge exports allowlisted terminal-run events through the
    # OtelExporter. Export failure never affects the run.
    if settings.otel_export_mode == "otlp-http":
        from endless_task.runtime_ledger.otel_exporter import (
            OtelExportMode,
            OtelExporter,
            OtelExporterConfig,
        )
        from endless_task.runtime_v2.run_trajectory import JournalOtelBridge

        otel_exporter = OtelExporter(
            OtelExporterConfig(
                mode=OtelExportMode.OTLP_HTTP,
                endpoint=settings.otel_export_endpoint,
            )
        )
        trace_fanout_members.append(
            JournalOtelBridge(
                exporter=otel_exporter,
                journal_reader=default_journal_reader(runtime_v2_repository),
            )
        )
    # M3B slice B (方案 A 兑现): failed-run auto-restore rides the same
    # terminal fanout but assembles independently of trace mode, so
    # runtime_trace_mode="0" keeps failure rollback active. Rule set in
    # run_restore.py: only terminal FAILED restores; CANCELLED/COMPLETED and
    # runs without checkpoints are no-ops; the observer is fail-open under
    # the executor's hard timeout. Gate: ENDLESS_TASK_RUN_AUTO_RESTORE.
    if settings.run_auto_restore_enabled:
        from endless_task.runtime_v2.run_restore import (
            RunFailureAutoRestoreObserver,
            default_run_status_reader,
        )

        trace_fanout_members.append(
            RunFailureAutoRestoreObserver(
                run_reader=default_run_status_reader(runtime_v2_repository),
                coordinator=run_checkpoint_coordinator,
                event_sink=(
                    lambda run_id, event_type, payload: runtime_v2_repository.append_runtime_event(  # noqa: E501
                        run_id=run_id,
                        event_type=event_type,
                        payload=payload,
                    )
                ),
            )
        )
    if trace_fanout_members:
        runtime_v2_trace_observer = FanoutTraceObserver(trace_fanout_members)
    provider_manager = ProviderManager(
        repository=provider_profile_repository,
        fallback_provider=selected_provider,
        secret_resolver=provider_secret_store.resolve,
    )
    selected_tool_registry = tool_registry or ToolRegistry()
    workspace_resolver = WorkspaceResolver(chat_repository, workspace_repository)
    # M3B slice F: unattended-executor tool enforcement (late-bound holder).
    # Built before the executors; replacements are configured after the
    # built-in tools are registered (registration order in this function).
    tool_enforcement = None
    unattended_tool_registry = None
    if settings.execution_backend_mode and tool_registry is None:
        # F2: enforcement applies to the built-in composition. A caller-supplied
        # custom registry (dev/test only — the product path always uses the
        # built-in tools) keeps its own tools untouched; no enforcement and no
        # silent swap, which is fine because that surface is internal.
        from endless_task.workspace_runtime.enforcement import (
            EnforcingToolRegistry,
            ToolEnforcement,
        )

        tool_enforcement = ToolEnforcement()
        unattended_tool_registry = EnforcingToolRegistry(
            selected_tool_registry, tool_enforcement
        )
    # M4B P2a gate: isolated_write requires the enforcement seam (no silent
    # degradation to read-only children / unrestricted local writes).
    if settings.delegation_mode == "isolated_write" and not settings.execution_backend_mode:
        raise ValueError(
            "ENDLESS_TASK_DELEGATION=isolated_write 需要执行后端 enforcement；"
            "请设置 ENDLESS_TASK_EXECUTION_BACKEND=local 或 container（或改回 0/readonly）。"
        )

    def _v2_workspace_tool_filter(
        conversation_id: str,
    ) -> Optional[Callable[[str], bool]]:
        """v2 按会话过滤工作区工具；与 v1 `_tool_filter_for_conversation` 一致。"""
        return workspace_tool_filter(workspace_resolver, conversation_id)

    def _v2_unattended_tool_filter(
        conversation_id: str,
    ) -> Callable[[str], bool]:
        workspace_filter = _v2_workspace_tool_filter(conversation_id)

        def allows(tool_name: str) -> bool:
            if workspace_filter is not None and not workspace_filter(tool_name):
                return False
            return (
                selected_tool_registry.resolve(tool_name).definition.approval_mode
                is ToolApprovalMode.AUTO
            )

        return allows

    def _v2_surface_definitions_provider(
        *,
        unattended: bool,
        exclude_tools: frozenset[str] = frozenset(),
    ):
        """Build the flag-gated v2 model surface for one conversation.

        Composes the nine built-in adapters plus any registered MCP bridges
        into a v2 catalog, applies the workspace-binding capability grant
        (legacy predicate parity, AP-105a) and projects the authorized
        surface with the calibrated provider profile. ``unattended`` also
        hides approval-required tools, matching the legacy unattended
        filter. ``exclude_tools`` hides named tools from the projected
        surface (delegation children never see the delegation tools
        themselves — depth = 1 gate). Execution still resolves tools by
        name through the legacy registry, so only the model surface
        switches.
        """

        def provider(conversation_id: str):
            from endless_task.runtime.provider import ProviderToolDefinition
            from endless_task.tool_platform import (
                ApprovalPolicy,
                CapabilityContext,
                CapabilityProfile,
                InMemoryToolCatalog,
                LegacyToolAdapter,
                ToolProvenance,
                ToolProvenanceKind,
                ToolScope,
                ToolSurfaceRequest,
                capability_grant_for_workspace_binding,
                create_openai_compatible_profile,
                project_tool_surface,
            )

            binding = workspace_resolver.resolve_binding(conversation_id)
            catalog = InMemoryToolCatalog()
            allowed: set[str] = set()
            approval_by_name: dict[str, ApprovalPolicy] = {}
            for definition in selected_tool_registry.definitions():
                tool = selected_tool_registry.resolve(definition.name)
                adapter = LegacyToolAdapter(tool)
                kind = (
                    ToolProvenanceKind.MCP
                    if definition.name.startswith("mcp__")
                    else ToolProvenanceKind.LEGACY_ADAPTER
                )
                catalog.register(
                    adapter,
                    scope=ToolScope.BUILTIN,
                    provenance=ToolProvenance(
                        kind=kind,
                        source_id=(
                            "builtin"
                            if kind is ToolProvenanceKind.LEGACY_ADAPTER
                            else "mcp"
                        ),
                    ),
                )
                allowed.update(adapter.definition.required_capabilities)
                approval_by_name[definition.name] = adapter.definition.approval

            granted = capability_grant_for_workspace_binding(
                binding is not None,
                frozenset(allowed),
            )
            surface = catalog.surface(
                ToolSurfaceRequest(
                    context=CapabilityContext(
                        profile=CapabilityProfile(
                            name="v2_surface",
                            allowed_capabilities=granted,
                        ),
                    ),
                )
            )
            report = project_tool_surface(surface, create_openai_compatible_profile())
            return tuple(
                ProviderToolDefinition(
                    name=projected.name,
                    description=projected.description,
                    input_schema=dict(projected.input_schema),
                )
                for projected in report.projected
                if projected.name not in exclude_tools
                and not (
                    unattended
                    and approval_by_name.get(projected.name)
                    is ApprovalPolicy.REQUIRED
                )
            )

        return provider

    runtime_v2_compaction_hook = (
        RuntimeV2ContextCompactionService(
            repository=runtime_v2_repository,
            max_context_tokens=settings.context_window_tokens,
            metrics=runtime_v2_metrics_collector,
        )
        if settings.context_compaction_enabled
        else None
    )

    tool_execution_limits = ToolExecutionLimits(
        max_calls_per_turn=settings.max_tool_calls_per_turn,
        max_concurrent_calls=settings.max_concurrent_tool_calls,
        max_argument_bytes=settings.max_tool_argument_bytes,
        max_total_argument_bytes=settings.max_total_tool_argument_bytes,
        max_argument_depth=settings.max_tool_argument_depth,
        max_argument_nodes=settings.max_tool_argument_nodes,
    )

    runtime_v2_gateway = RuntimeV2SessionGateway(
        chat_repository=chat_repository,
        repository=runtime_v2_repository,
        provider=selected_provider,
        provider_resolver=provider_manager.resolve,
        tool_registry=selected_tool_registry,
        model=settings.model,
        max_output_tokens=settings.max_output_tokens,
        temperature=None,
        provider_slot_limit=settings.max_concurrent_model_calls,
        memory_repository=runtime_v2_memory_repository,
        tool_filter_provider=_v2_workspace_tool_filter,
        tool_definitions_provider=(
            _v2_surface_definitions_provider(unattended=False)
            if settings.tool_platform_v2_enabled
            else None
        ),
        v2_pipeline_enabled=settings.tool_platform_v2_enabled,
        context_engine_v2_enabled=settings.context_engine_v2_enabled,
        context_window_tokens=settings.context_window_tokens,
        compaction_hook=runtime_v2_compaction_hook,
        metrics_collector=runtime_v2_metrics_collector,
        agent_timeout_seconds=settings.agent_timeout_seconds,
        approval_timeout_seconds=settings.approval_timeout_seconds,
        tool_execution_limits=tool_execution_limits,
        trace_observer=runtime_v2_trace_observer,
        span_recorder=runtime_v2_span_recorder,
        provider_retry_evaluator=runtime_v2_provider_retry_evaluator,
    )
    # Task/reminder runs have no interactive approval channel. Required tools
    # are hidden from the model and denied if a provider still emits one.
    task_executor = AgentRunExecutor(
        repository=runtime_v2_repository,
        provider=selected_provider,
        # M3B slice F: unattended task runs use the enforcement view when
        # ENDLESS_TASK_EXECUTION_BACKEND is set (else identical to the
        # shared registry).
        tool_registry=unattended_tool_registry or selected_tool_registry,
        model=settings.model,
        max_output_tokens=settings.max_output_tokens,
        temperature=None,
        approval_gate=UnattendedToolApprovalGate(),
        provider_slot=(
            asyncio.Semaphore(settings.max_concurrent_model_calls)
            if settings.max_concurrent_model_calls
            else None
        ),
        compaction_hook=runtime_v2_compaction_hook,
        tool_filter_provider=_v2_unattended_tool_filter,
        tool_definitions_provider=(
            _v2_surface_definitions_provider(unattended=True)
            if settings.tool_platform_v2_enabled
            else None
        ),
        v2_pipeline_enabled=settings.tool_platform_v2_enabled,
        context_engine_v2_enabled=settings.context_engine_v2_enabled,
        context_window_tokens=settings.context_window_tokens,
        metrics=runtime_v2_metrics_collector,
        agent_timeout_seconds=settings.agent_timeout_seconds,
        tool_execution_limits=tool_execution_limits,
        trace_observer=runtime_v2_trace_observer,
        span_recorder=runtime_v2_span_recorder,
        provider_retry_evaluator=runtime_v2_provider_retry_evaluator,
        provider_retry_observer=(
            # Task/reminder runs are one-shot; the process-level metrics
            # summary aggregates retry decisions (not per-run), so the
            # static label only marks the execution path.
            runtime_v2_metrics_collector.provider_retry_observer("task")
            if runtime_v2_provider_retry_evaluator is not None
            else None
        ),
    )
    if settings.delegation_mode != "0":
        # M4A read-only delegation (DR-2): register the three delegation
        # tools and wire their handler to the production kernel adapter.
        # Children execute through RuntimeV2AgentKernel against the same
        # repository; child conversations are delegation-tagged so product
        # enumeration hides them (06 2.4). Spawn idempotency keys on the
        # parent tool call id (06 8.6); parents must hold read capabilities.
        from endless_task.tool_platform import (
            capability_grant_for_workspace_binding as _delegation_grant,
        )

        delegation_tool_names = frozenset(
            {"spawn_agent", "query_agent", "cancel_agent"}
        )

        # One shared provider slot for ALL children of the process (same
        # global concurrency cap the gateway applies to parent runs); every
        # child executor receives it, so N parallel children cannot each
        # spin an unbounded provider fan-out.
        delegation_provider_slot = (
            asyncio.Semaphore(settings.max_concurrent_model_calls)
            if settings.max_concurrent_model_calls
            else None
        )

        def _delegation_kernel_provider(request):
            del request  # one kernel per parent run is created on demand
            return RuntimeV2AgentKernel(
                repository=runtime_v2_repository,
                conversation_factory=_delegation_conversation_factory,
                executor_builder=_delegation_executor_builder,
            )

        async def _delegation_conversation_factory(
            child_run_id: str, *, workspace_id=None
        ):
            # M4B P2b-ii: an isolated child conversation is bound to its
            # scratch workspace so its fs tools resolve into the scratch root.
            return chat_repository.create_delegation_conversation(
                child_run_id, workspace_id=workspace_id
            )

        def _delegation_executor_builder():
            return AgentRunExecutor(
                repository=runtime_v2_repository,
                provider=selected_provider,
                # M3B slice F: same enforcement view as task runs (children
                # are read-only today; write-enabled M4B children inherit it).
                tool_registry=unattended_tool_registry or selected_tool_registry,
                model=settings.model,
                max_output_tokens=settings.max_output_tokens,
                temperature=None,
                approval_gate=UnattendedToolApprovalGate(),
                provider_slot=delegation_provider_slot,
                compaction_hook=runtime_v2_compaction_hook,
                tool_filter_provider=_delegation_child_tool_filter,
                tool_definitions_provider=(
                    _v2_surface_definitions_provider(
                        unattended=True,
                        exclude_tools=delegation_tool_names,
                    )
                    if settings.tool_platform_v2_enabled
                    else None
                ),
                v2_pipeline_enabled=settings.tool_platform_v2_enabled,
                context_engine_v2_enabled=settings.context_engine_v2_enabled,
                context_window_tokens=settings.context_window_tokens,
                metrics=runtime_v2_metrics_collector,
                agent_timeout_seconds=settings.agent_timeout_seconds,
                tool_execution_limits=tool_execution_limits,
                trace_observer=runtime_v2_trace_observer,
                span_recorder=runtime_v2_span_recorder,
                provider_retry_evaluator=runtime_v2_provider_retry_evaluator,
                provider_retry_observer=(
                    # Child run ids are assigned by the coordinator after the
                    # executor is built; the metrics summary is aggregate,
                    # so the static label marks the delegation path.
                    runtime_v2_metrics_collector.provider_retry_observer(
                        "delegation_child"
                    )
                    if runtime_v2_provider_retry_evaluator is not None
                    else None
                ),
            )

        def _delegation_child_tool_filter(conversation_id: str):
            # Children must never surface the delegation tools themselves
            # (depth = 1 gate, 06 9 DR-2) nor approval-required tools.
            workspace_filter = _v2_workspace_tool_filter(conversation_id)

            def allows(tool_name: str) -> bool:
                if tool_name in delegation_tool_names:
                    return False
                if workspace_filter is not None and not workspace_filter(tool_name):
                    return False
                return (
                    selected_tool_registry.resolve(tool_name)
                    .definition.approval_mode
                    is ToolApprovalMode.AUTO
                )

            return allows

        def _delegation_capability_provider(request) -> frozenset[str]:
            # Capabilities are read from the v2 adapter definition (the v1
            # legacy ToolDefinition carries no required_capabilities field);
            # LegacyToolAdapter fills them from the audited policy table or
            # the conservative generic effect mapping (AP-105a parity).
            from endless_task.tool_platform import LegacyToolAdapter

            binding = workspace_resolver.resolve_binding(request.conversation_id)
            allowed: set[str] = set()
            for definition in selected_tool_registry.definitions():
                tool = selected_tool_registry.resolve(definition.name)
                allowed.update(LegacyToolAdapter(tool).definition.required_capabilities)
            return _delegation_grant(
                binding is not None,
                frozenset(allowed),
            )

        def _delegation_isolated_workspace_provider(
            conversation_id: str, child_run_id: str
        ) -> str:
            from endless_task.delegation.isolated_workspace import (
                prepare_isolated_child_workspace,
            )

            binding = workspace_resolver.require_binding(conversation_id)
            return prepare_isolated_child_workspace(
                workspace_repository=workspace_repository,
                parent_workspace_root=binding.root,
                child_run_id=child_run_id,
            )

        delegation_handler = CoordinatorDelegationHandler(
            kernel_provider=_delegation_kernel_provider,
            capability_provider=_delegation_capability_provider,
            isolated_write_enabled=(settings.delegation_mode == "isolated_write"),
            isolated_workspace_provider=_delegation_isolated_workspace_provider,
        )
        selected_tool_registry.register(SpawnAgentLegacyTool(delegation_handler))
        selected_tool_registry.register(QueryAgentLegacyTool(delegation_handler))
        selected_tool_registry.register(CancelAgentLegacyTool(delegation_handler))
        delegation_handler_ref = delegation_handler
    else:
        delegation_handler_ref = None
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
            skill_service.visible_skills(root, workspace_id=workspace_id or ""),
            locator_mode=settings.skill_packages_enabled,
        )

    def skill_locator_resolver(conversation_id: str):
        """Resolve ``skill://<scope>/<name>`` to a canonical SKILL.md path.

        SK-1b/SK-3: registry-backed locator resolution over the
        conversation's skill roots; returns None for unknown, quarantined,
        invalid or dependency-unsatisfied skills (fail closed) so the tool
        reports a stable error and the runtime never auto-widens
        capabilities to satisfy a skill. Malformed locators raise
        ValueError.
        """
        if not settings.skill_packages_enabled:
            return None
        from endless_task.skills import (
            InMemorySkillRegistry,
            SkillLocator,
            SkillRoot,
            SkillDependencyContext,
            check_skill_dependencies,
        )

        roots = skill_roots_for_conversation(conversation_id)
        root_objects: list[SkillRoot] = []
        for index, root in enumerate(roots):
            source = "workspace" if index > 0 else "user"
            rank = 1 if index > 0 else 2
            root_objects.append(SkillRoot(root, source, rank))
        registry = InMemorySkillRegistry()
        registry.discover(tuple(root_objects))
        dependency_context = _conversation_skill_dependency_context(
            conversation_id
        )

        def resolve(locator: str) -> Optional[Path]:
            parsed = SkillLocator.parse(locator)
            revision = registry.resolve(parsed)
            if revision is None:
                return None
            if not check_skill_dependencies(
                revision, dependency_context
            ).satisfied:
                return None
            return revision.manifest_path

        return resolve

    def _conversation_skill_dependency_context(conversation_id: str):
        """Tools + granted capabilities of one conversation (SK-3 context).

        Mirrors the v2 surface grant so skill dependencies are checked
        against the same surface the model can actually call.
        """
        from endless_task.skills import SkillDependencyContext
        from endless_task.tool_platform import (
            LegacyToolAdapter,
            capability_grant_for_workspace_binding as _grant,
        )

        binding = workspace_resolver.resolve_binding(conversation_id)
        tool_names: set[str] = set()
        allowed: set[str] = set()
        for definition in selected_tool_registry.definitions():
            tool_names.add(definition.name)
            try:
                tool = selected_tool_registry.resolve(definition.name)
            except Exception:
                continue
            allowed.update(LegacyToolAdapter(tool).definition.required_capabilities)
        return SkillDependencyContext(
            available_tools=frozenset(tool_names),
            granted_capabilities=_grant(
                binding is not None,
                frozenset(allowed),
            ),
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
                checkpoint_coordinator=run_checkpoint_coordinator,
            )
        )
        selected_tool_registry.register(ListWorkspaceDirTool(workspace_resolver))
        selected_tool_registry.register(
            ReadSkillFileTool(
                skill_roots_for_conversation,
                locator_resolver_provider=skill_locator_resolver,
                max_file_bytes=settings.max_file_bytes,
            )
        )
        selected_tool_registry.register(
            DeleteWorkspaceFileTool(
                workspace_resolver,
                effect_log,
                checkpoint_coordinator=run_checkpoint_coordinator,
            )
        )
        selected_tool_registry.register(
            RunShellTool(
                workspace_resolver,
                effect_log,
                timeout_seconds=settings.shell_timeout_seconds,
                no_change_timeout_seconds=settings.shell_no_change_timeout_seconds,
                max_output_bytes=settings.shell_max_output_bytes,
                checkpoint_coordinator=run_checkpoint_coordinator,
            )
        )
        selected_tool_registry.register(UpdatePlanTool(runtime_v2_repository))
        if tool_enforcement is not None:
            # Enforcement backend shares the run coordinator's mutation
            # ledger so run-scoped restore attribution stays continuous.
            from endless_task.execution_env import LocalExecutionBackend

            enforcement_backend = LocalExecutionBackend(ledger=mutation_ledger)
            tool_enforcement.configure(
                replacements={
                    "write_workspace_file": WriteWorkspaceFileTool(
                        workspace_resolver,
                        effect_log,
                        max_write_bytes=settings.workspace_max_write_bytes,
                        checkpoint_coordinator=run_checkpoint_coordinator,
                        execution_backend=enforcement_backend,
                    ),
                    "delete_workspace_file": DeleteWorkspaceFileTool(
                        workspace_resolver,
                        effect_log,
                        checkpoint_coordinator=run_checkpoint_coordinator,
                        execution_backend=enforcement_backend,
                    ),
                }
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
        file_repository=file_repository,
        memory_repository=memory_repository,
        artifact_proposal_repository=artifact_proposal_repository,
        artifact_repository=artifact_repository,
        task_repository=task_repository,
        knowledge_repository=knowledge_repository,
        retrieval_event_repository=retrieval_event_repository,
        skill_prompt_builder=skill_prompt_for_workspace,
    )

    async def build_runtime_v2_context_prefix(
        conversation_id: str,
        lane_id: str,
        run_id: str,
        user_content: str,
    ) -> tuple[ProviderMessage]:
        del lane_id
        conversation = chat_repository.get_conversation(conversation_id)
        return context_builder.build_system_messages(
            conversation_id,
            user_content,
            turn_id=run_id,
            workspace_id=conversation.workspace_id,
            # v2 记忆统一由 <runtime-memory> 注入，S3 层不再叠加 v1 记忆，
            # 避免 v1/v2 记忆双重注入（v1 存量已由 migration 049 迁入 v2）。
            include_v1_memory=False,
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
            runtime_v2_repository=runtime_v2_repository,
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
            auto_fact_enabled=settings.memory_auto_fact,
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

        def _write_v2_memory_auto(
            *,
            conversation_id: str,
            content: str,
            kind: str,
        ) -> None:
            """自动写入记忆:NOOP(重复)/UPDATE(同主题更强)/ADD(新增)。"""
            similar = runtime_v2_memory_quality_service.find_similar(
                conversation_id=conversation_id,
                content=content,
            )
            if similar is None:
                runtime_v2_memory_repository.create_memory(
                    scope=MemoryScope.USER_GLOBAL,
                    kind=kind,
                    content=content,
                    conversation_id=conversation_id,
                )
                return
            if (
                similar.similarity
                >= runtime_v2_memory_quality_service.similarity_threshold
            ):
                return  # NOOP:重复内容不新增
            created = runtime_v2_memory_repository.create_memory(
                scope=MemoryScope.USER_GLOBAL,
                kind=kind,
                content=content,
                conversation_id=conversation_id,
            )
            runtime_v2_memory_repository.supersede_memory(
                similar.record.id,
                superseded_by=created.id,
            )  # UPDATE:新记忆取代旧记忆(旧保留可追溯)

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

                    def _write_v2_user_global_memory(
                        kind, content, conversation_id, turn_id
                    ) -> None:
                        """v2 运行的自动提取写入 v2 user_global(读取端统一 v2 表)。"""
                        del turn_id
                        try:
                            _write_v2_memory_auto(
                                conversation_id=conversation_id,
                                content=content,
                                kind=(
                                    kind.value
                                    if isinstance(kind, MemoryKind)
                                    else str(kind)
                                ),
                            )
                        except Exception:  # noqa: BLE001 自动写入失败不阻断提取
                            logger.debug(
                                "Failed to write auto-fact memory to v2",
                                exc_info=True,
                            )

                    await memory_proposal_service.generate_for_turn(
                        conversation_id=run.conversation_id,
                        turn_id=run.id,
                        user_message=user_message,
                        assistant_message=assistant_message,
                        auto_write_target=_write_v2_user_global_memory,
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
        runtime_v2_repository=runtime_v2_repository,
        task_executor=task_executor,
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
        provider_secret_store=provider_secret_store,
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
        tool_registry=selected_tool_registry,
        runtime_v2_repository=runtime_v2_repository,
        runtime_v2_memory_repository=runtime_v2_memory_repository,
        runtime_v2_memory_quality_service=runtime_v2_memory_quality_service,
        runtime_v2_gateway=runtime_v2_gateway,
        delegation_handler=(
            delegation_handler_ref
            if settings.delegation_mode != "0"
            else None
        ),
        runtime_v2_trace_observer=runtime_v2_trace_observer,
        runtime_v2_trajectory_exporter=runtime_v2_trajectory_exporter,
        runtime_v2_span_recorder=runtime_v2_span_recorder,
        run_checkpoint_coordinator=run_checkpoint_coordinator,
        unattended_tool_registry=unattended_tool_registry,
        execution_backend_mode=settings.execution_backend_mode,
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


class ResendRuntimeV2RunBody(BaseModel):
    content: str


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
        await container.mcp_manager.start_all()
        lifespan_tasks = BackgroundTaskSupervisor(
            name="api-lifespan",
            logger=logger,
        )
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
        # M3B slice E: crash-window startup reconciliation. A run that FAILED
        # but whose terminal auto-restore never ran (process died in the
        # window) is restored here, once per run (guarded, per-run scoped,
        # marker run_auto_restored). Fail-open: never blocks API startup.
        if (
            container.settings.run_auto_restore_enabled
            and container.run_checkpoint_coordinator is not None
        ):
            try:
                from endless_task.runtime_v2.run_reconcile import (
                    reconcile_failed_runs,
                )

                reconcile_summary = await reconcile_failed_runs(
                    repository=container.runtime_v2_repository,
                    coordinator=container.run_checkpoint_coordinator,
                )
                if reconcile_summary.restored_runs:
                    logger.info(
                        "Startup reconcile restored failed runs",
                        extra={
                            "candidates": reconcile_summary.candidates,
                            "restoredRuns": reconcile_summary.restored_runs,
                            "restoredFiles": reconcile_summary.restored_files,
                        },
                    )
            except Exception:
                logger.exception("Failed-run startup reconciliation failed")
        if container.settings.scheduler_enabled:
            lifespan_tasks.spawn(
                container.task_scheduler.run(),
                name="task-scheduler",
            )
        if (
            container.settings.knowledge_decay_enabled
            and container.knowledge_lifecycle_service is not None
        ):
            lifespan_tasks.spawn(
                _run_knowledge_decay_loop(container),
                name="knowledge-decay",
            )
        try:
            yield
        finally:
            if container.embedding_indexer is not None:
                container.embedding_indexer.stop()
            await lifespan_tasks.shutdown(cancel=True)
            await container.mcp_manager.stop_all()
            await container.task_worker.drain()
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
            "toolPlatformV2": {
                "enabled": container.settings.tool_platform_v2_enabled,
                "profileName": (
                    _tool_platform_v2_profile_name()
                    if container.settings.tool_platform_v2_enabled
                    else None
                ),
            },
            "contextEngineV2": {
                "enabled": container.settings.context_engine_v2_enabled,
            },
            "delegation": {
                "enabled": container.settings.delegation_mode != "0",
                "mode": (
                    container.settings.delegation_mode
                    if container.settings.delegation_mode != "0"
                    else None
                ),
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
        # 必选绑定设定：新建会话必须归属到一个已绑定目录的工作区。
        workspace_id = _require_bound_workspace(
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
        supplied_fields = body.model_fields_set
        if not supplied_fields:
            raise ApiRequestError(
                "invalid_request",
                "至少需要提供 title、status、providerProfileId、modelOverride 或 workspaceId。",
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
        if "providerProfileId" in supplied_fields or "modelOverride" in supplied_fields:
            provider_profile_id = (
                body.providerProfileId
                if "providerProfileId" in supplied_fields
                else conversation.provider_profile_id
            )
            model_override = (
                body.modelOverride
                if "modelOverride" in supplied_fields
                else conversation.model_override
            )
            if provider_profile_id is not None or model_override is not None:
                profile_id = provider_profile_id
                if profile_id is None:
                    profile_id = (
                        container.provider_profile_repository.get_default_profile_id()
                    )
                if profile_id is None:
                    raise ApiRequestError(
                        "provider_not_configured",
                        "请先配置可用的模型服务。",
                        status_code=409,
                    )
                try:
                    profile = container.provider_profile_repository.get_profile(
                        profile_id
                    )
                except NotFoundError as error:
                    raise ApiRequestError(
                        "provider_not_available",
                        "所选模型服务已不存在，请重新选择。",
                        status_code=409,
                    ) from error
                if not profile.enabled:
                    raise ApiRequestError(
                        "provider_disabled",
                        "该模型服务已停用，请重新选择。",
                        status_code=409,
                    )
                selected_model_id = model_override or profile.default_model
                if not selected_model_id:
                    raise ApiRequestError(
                        "model_not_available",
                        "该模型服务没有可用的默认模型，请先完成模型配置。",
                        status_code=409,
                    )
                try:
                    model = container.provider_profile_repository.get_model(
                        profile_id,
                        selected_model_id,
                    )
                except NotFoundError as error:
                    raise ApiRequestError(
                        "model_not_available",
                        "所选模型不在该服务的可用模型中，请重新选择。",
                        status_code=409,
                    ) from error
                if not model.enabled:
                    raise ApiRequestError(
                        "model_disabled",
                        "该模型已停用，请重新选择。",
                        status_code=409,
                    )
            conversation = container.chat_repository.set_conversation_model(
                conversation_id,
                provider_profile_id=provider_profile_id,
                model_override=model_override,
            )
        if "workspaceId" in supplied_fields:
            if conversation.kind != ConversationKind.NORMAL or (
                conversation.parent_conversation_id is not None
            ):
                raise ApiRequestError(
                    "workspace_conversation_mismatch",
                    "临时/分支会话不允许跨工作区迁移。",
                    status_code=409,
                )
            workspace_id = _require_bound_workspace(body.workspaceId)
            conversation = container.chat_repository.set_conversation_workspace(
                conversation_id,
                workspace_id,
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
        conversation = container.chat_repository.promote_conversation(
            conversation_id
        )
        return {"conversation": conversation_json(conversation)}

    @app.get("/memories")
    async def list_memories(
        include_deleted: bool = False,
        write_origin: Optional[str] = Query(default=None),
    ) -> dict[str, object]:
        memories = container.memory_repository.list_memories(
            include_deleted=include_deleted
        )
        if write_origin is not None:
            memories = [
                item for item in memories if item.write_origin == write_origin
            ]
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

    def _require_bound_workspace(value: Optional[str]) -> str:
        """校验目标工作区存在且已绑定本地目录，返回其 id。

        会话必须归属到已绑定目录的工作区（必选绑定产品设定）。失败抛出
        ApiRequestError（409），不返回 None。
        """
        text = (value or "").strip()
        if not text or text == "general":
            raise ApiRequestError(
                "workspace_required",
                "会话必须归属到一个工作区，请先选择并绑定工作区目录。",
                status_code=409,
            )
        try:
            workspace = container.workspace_repository.get_workspace(text)
        except NotFoundError as error:
            raise ApiRequestError(
                "workspace_not_found",
                "所选工作区已不存在，请重新选择。",
                status_code=404,
            ) from error
        if not workspace.root_path:
            raise ApiRequestError(
                "workspace_not_bound",
                "该工作区尚未绑定本地目录，请先绑定目录后再创建/迁移会话。",
                status_code=409,
            )
        return text

    def _emit_knowledge_duplicates(source) -> None:
        service = container.knowledge_lifecycle_service
        if service is None:
            return
        try:
            service.detect_duplicates(source)
        except Exception:  # noqa: BLE001 去重检测不影响写入本身
            logger.debug("Knowledge duplicate detection failed", exc_info=True)

    def _provider_model_json(
        model: ProviderModel,
        *,
        default_model: str,
    ) -> dict[str, object]:
        return {
            "providerProfileId": model.provider_profile_id,
            "modelId": model.model_id,
            "displayName": model.display_name,
            "source": model.source,
            "enabled": model.enabled,
            "isDefault": model.model_id == default_model,
            "lastSeenAt": model.last_seen_at,
        }

    def _provider_profile_json(profile) -> dict[str, object]:
        configured = container.provider_manager.is_configured(profile)
        connection_state = profile.connection_state
        if profile.is_builtin:
            connection_state = "ready" if configured else "failed"
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
            "configured": configured,
            "apiKeyConfigured": container.provider_secret_store.has(
                profile.api_key_ref
            ),
            "connectionState": connection_state,
            "lastCheckedAt": profile.last_checked_at,
            "lastError": profile.last_error,
            "models": [
                _provider_model_json(model, default_model=profile.default_model)
                for model in container.provider_profile_repository.list_models(profile.id)
            ],
            "createdAt": profile.created_at,
            "updatedAt": profile.updated_at,
        }

    def _provider_draft(
        *,
        current=None,
        name: Optional[str] = None,
        default_model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key_ref: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> ProviderProfileDraft:
        return ProviderProfileDraft(
            name=name if name is not None else current.name,
            default_model=(
                default_model if default_model is not None else current.default_model
            ),
            base_url=base_url if base_url is not None else current.base_url,
            api_key_ref=(
                api_key_ref if api_key_ref is not None else current.api_key_ref
            ),
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else current.timeout_seconds
            ),
            enabled=enabled if enabled is not None else current.enabled,
        )

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
        if (
            body.apiKey
            and body.apiKey.strip()
            and body.apiKeyRef
            and body.apiKeyRef.strip()
        ):
            raise ApiRequestError(
                "invalid_provider_credentials",
                "API Key 和 API Key 引用不能同时填写。",
            )
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
        try:
            if body.apiKey and body.apiKey.strip():
                reference = container.provider_secret_store.put(
                    profile.id, body.apiKey
                )
                profile = container.provider_profile_repository.update_profile(
                    profile.id,
                    _provider_draft(current=profile, api_key_ref=reference),
                )
        except Exception:
            container.provider_secret_store.delete(profile.id)
            container.provider_profile_repository.delete_profile(profile.id)
            raise
        return {"profile": _provider_profile_json(profile)}

    @app.patch("/providers/{profile_id}")
    async def patch_provider_profile(
        profile_id: str, body: ProviderProfilePatchBody
    ) -> dict[str, object]:
        current = container.provider_profile_repository.get_profile(profile_id)
        if current.is_builtin and body.apiKey is not None:
            raise ApiRequestError(
                "builtin_provider_credentials",
                "环境配置的模型服务不能在此修改 API Key。",
            )
        if (
            body.apiKey
            and body.apiKey.strip()
            and body.apiKeyRef
            and body.apiKeyRef.strip()
        ):
            raise ApiRequestError(
                "invalid_provider_credentials",
                "API Key 和 API Key 引用不能同时填写。",
            )
        next_reference = body.apiKeyRef
        stored_key_changed = body.apiKey is not None and bool(body.apiKey.strip())
        previous_stored_secret = None
        if stored_key_changed:
            if current.api_key_ref == container.provider_secret_store.reference_for(
                profile_id
            ):
                previous_stored_secret = container.provider_secret_store.resolve(
                    current.api_key_ref
                )
            next_reference = container.provider_secret_store.put(
                profile_id, body.apiKey or ""
            )
        try:
            profile = container.provider_profile_repository.update_profile(
                profile_id,
                _provider_draft(
                    current=current,
                    name=body.name,
                    default_model=body.defaultModel,
                    base_url=body.baseUrl,
                    api_key_ref=next_reference,
                    timeout_seconds=body.timeoutSeconds,
                    enabled=body.enabled,
                ),
            )
        except Exception:
            if stored_key_changed:
                if previous_stored_secret:
                    container.provider_secret_store.put(
                        profile_id, previous_stored_secret
                    )
                else:
                    container.provider_secret_store.delete(profile_id)
            raise
        if body.baseUrl is not None or stored_key_changed or body.apiKeyRef is not None:
            profile = container.provider_profile_repository.set_connection_state(
                profile_id,
                state="untested",
            )
        stored_reference = container.provider_secret_store.reference_for(profile_id)
        if (
            body.apiKeyRef is not None
            and current.api_key_ref == stored_reference
            and profile.api_key_ref != stored_reference
        ):
            container.provider_secret_store.delete(profile_id)
        await container.provider_manager.invalidate(profile.id)
        return {"profile": _provider_profile_json(profile)}

    @app.delete("/providers/{profile_id}", status_code=204)
    async def delete_provider_profile(profile_id: str) -> None:
        container.provider_profile_repository.delete_profile(profile_id)
        container.provider_secret_store.delete(profile_id)
        await container.provider_manager.invalidate(profile_id)

    @app.post("/providers/default")
    async def set_default_provider(body: ProviderDefaultBody) -> dict[str, object]:
        container.provider_profile_repository.set_default_profile_id(body.profileId)
        profile = container.provider_profile_repository.get_profile(body.profileId)
        return {"profile": _provider_profile_json(profile)}

    @app.post("/providers/{profile_id}/refresh-models")
    async def refresh_provider_models(profile_id: str) -> dict[str, object]:
        try:
            discovered = await container.provider_manager.discover_models(profile_id)
        except ProviderError as error:
            container.provider_profile_repository.set_connection_state(
                profile_id,
                state="failed",
                error=error.safe_message,
            )
            raise ApiRequestError(
                error.code,
                error.safe_message,
                status_code=422,
            ) from error
        except ValueError as error:
            message = str(error) or "无法获取模型列表。"
            container.provider_profile_repository.set_connection_state(
                profile_id,
                state="failed",
                error=message,
            )
            raise ApiRequestError(
                "model_discovery_unavailable",
                message,
                status_code=422,
            ) from error
        models = container.provider_profile_repository.replace_discovered_models(
            profile_id,
            discovered,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        if not profile.default_model and models:
            profile = container.provider_profile_repository.set_default_model(
                profile_id,
                models[0].model_id,
            )
        profile = container.provider_profile_repository.set_connection_state(
            profile_id,
            state="ready",
        )
        return {"profile": _provider_profile_json(profile)}

    @app.post("/providers/{profile_id}/models", status_code=201)
    async def add_provider_model(
        profile_id: str,
        body: ProviderModelBody,
    ) -> dict[str, object]:
        model = container.provider_profile_repository.add_model(
            profile_id,
            model_id=body.modelId,
            display_name=body.displayName,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        if not profile.default_model:
            profile = container.provider_profile_repository.set_default_model(
                profile_id,
                model.model_id,
            )
        return {
            "model": _provider_model_json(
                model,
                default_model=profile.default_model,
            ),
            "profile": _provider_profile_json(profile),
        }

    @app.patch("/providers/{profile_id}/models/{model_id:path}")
    async def patch_provider_model(
        profile_id: str,
        model_id: str,
        body: ProviderModelPatchBody,
    ) -> dict[str, object]:
        model = container.provider_profile_repository.set_model_enabled(
            profile_id,
            model_id,
            enabled=body.enabled,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        return {
            "model": _provider_model_json(
                model,
                default_model=profile.default_model,
            )
        }

    @app.delete("/providers/{profile_id}/models/{model_id:path}", status_code=204)
    async def delete_provider_model(profile_id: str, model_id: str) -> None:
        container.provider_profile_repository.delete_model(profile_id, model_id)

    @app.post("/providers/{profile_id}/default-model")
    async def set_provider_default_model(
        profile_id: str,
        body: ProviderDefaultModelBody,
    ) -> dict[str, object]:
        profile = container.provider_profile_repository.set_default_model(
            profile_id,
            body.modelId,
        )
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

    @app.delete("/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(workspace_id: str) -> Response:
        # 级联删除：该工作区名下的会话（含后代分支）一并删除；先清理其相关的
        # 提醒/任务/提案，再删除会话，最后删除工作区注册。
        conversation_ids = container.chat_repository.list_workspace_conversation_ids(
            workspace_id
        )
        for target_id in conversation_ids:
            descendant_ids = container.chat_repository.list_descendant_ids(target_id)
            for delete_id in (target_id, *descendant_ids):
                container.reminder_repository.cancel_reminders_for_conversation(
                    delete_id
                )
                container.task_repository.cancel_tasks_for_conversation(delete_id)
                container.task_proposal_repository.cancel_proposals_for_conversation(
                    delete_id
                )
                container.proposal_repository.cancel_proposals_for_conversation(
                    delete_id
                )
        for target_id in conversation_ids:
            try:
                container.chat_repository.delete_conversation(target_id)
            except InvalidStateError as error:
                raise ApiRequestError(
                    "workspace_conversation_active",
                    f"工作区下的会话「{target_id}」仍在运行，请先停止后重试。",
                    status_code=409,
                ) from error
        removed = container.workspace_repository.delete_workspace(workspace_id)
        if not removed:
            raise ApiRequestError(
                "workspace_not_found",
                "所选工作区已不存在。",
                status_code=404,
            )
        return Response(status_code=204)

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
                            "SELECT conversation_id FROM v2_transcript_entries WHERE id = ?",
                            (hit.ref_id,),
                        ).fetchone()
                        if row is None:
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

    def _mirror_confirmed_memory_to_v2(container, memory) -> None:
        """v2 会话的确认记忆镜像到 v2 user_global。

        读取端（v2 链路）统一只读 v2_runtime_memories，因此 v2 会话确认的
        记忆必须同时进入 v2 表；v1 表保留（提案状态机完整），但不再被
        v2 链路读取，避免 v1/v2 记忆双重注入。
        """
        conversation_id = memory.source_conversation_id
        try:
            quality = container.runtime_v2_memory_quality_service
            repository = container.runtime_v2_memory_repository
            similar = quality.find_similar(
                conversation_id=conversation_id,
                content=memory.content,
            )
            if similar is None:
                repository.create_memory(
                    scope=MemoryScope.USER_GLOBAL,
                    kind=memory.kind.value,
                    content=memory.content,
                    conversation_id=conversation_id,
                )
                return
            if similar.similarity >= quality.similarity_threshold:
                return  # NOOP:重复内容不新增
            created = repository.create_memory(
                scope=MemoryScope.USER_GLOBAL,
                kind=memory.kind.value,
                content=memory.content,
                conversation_id=conversation_id,
            )
            repository.supersede_memory(
                similar.record.id,
                superseded_by=created.id,
            )  # UPDATE:新记忆取代旧记忆(旧保留可追溯)
        except Exception:  # noqa: BLE001 镜像失败不影响提案确认
            logger.debug(
                "Failed to mirror confirmed memory to v2",
                exc_info=True,
            )

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
        _mirror_confirmed_memory_to_v2(container, memory)
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
        main_lane_id = pointer.active_lane_id if pointer is not None else None
        running_runs = container.runtime_v2_repository.list_runs(
            conversation_id=target_conversation_id,
            statuses=(
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.COMPACTING,
                RunStatus.CANCELLING,
            ),
        )
        running_run = running_runs[-1] if running_runs else None
        return {
            "conversationId": target_conversation_id,
            "activeLaneId": main_lane_id,
            "mainLaneId": main_lane_id,
            "runningLaneId": running_run.lane_id if running_run is not None else None,
            "runningRunId": running_run.id if running_run is not None else None,
            "items": tuple(
                _runtime_v2_lane_json(lane, active_lane_id=main_lane_id)
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
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            source_conversation_id
        )
        if pointer is None:
            raise ConflictError("Conversation has no v2 main lane")
        main_lane = container.runtime_v2_repository.get_lane(pointer.active_lane_id)
        if main_lane.leaf_entry_id is None:
            raise ConflictError("Conversation main lane has no context to copy")
        temporary_conversation_id, lane = (
            await container.runtime_v2_gateway.create_temporary_conversation(
                source_conversation_id=source_conversation_id,
                source_lane_id=main_lane.id,
                source_leaf_entry_id=main_lane.leaf_entry_id,
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

    @app.post("/api/v2/runs/{run_id}/resend", status_code=202)
    async def resend_runtime_v2_run(
        run_id: str, body: ResendRuntimeV2RunBody
    ) -> dict[str, object]:
        result = await container.runtime_v2_gateway.resend_run(run_id, body.content)
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
            expires_at=body.expiresAt,
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
            expires_at=body.expiresAt,
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
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
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
            client_request_id=idempotency_key,
        )
        return {
            "conversationId": handle.conversation_id,
            "laneId": handle.lane_id,
            "runId": handle.run_id,
            "userMessageId": handle.user_message_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
        }

    @app.get("/api/v2/metrics")
    async def get_runtime_v2_metrics() -> dict[str, object]:
        return container.runtime_v2_gateway.metrics_summary()

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


    return app
