from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Callable, Literal, Mapping, Optional

from fastapi import WebSocket, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from endless_task.domain.task_schedule import ReminderDue
from endless_task.domain.models import (
    ArtifactVersionOperation,
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
    FeedbackRating,
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
from endless_task.runtime_v2.audit_trail import AuditFact, build_audit_trail
from endless_task.runtime_v2.gateway import derive_approval_risk
from endless_task.runtime_v2.verification import VERIFIER_MODES
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
    UndoUnavailableError,
    WorkspaceUndoService,
    ReadSkillFileTool,
    DeleteWorkspaceFileTool,
    EffectLog,
    ListWorkspaceDirTool,
    EditWorkspaceFileTool,
    ManageWorkspacePathsTool,
    ReadWorkspaceFileTool,
    RunShellTool,
    WorkspaceResolver,
    WorkspaceSearchTool,
    WriteWorkspaceFileTool,
)
from endless_task.workspace_runtime.artifact_store import ArtifactFileStore
from endless_task.workspace_runtime.browse import browse_directory
from endless_task.workspace_runtime.skill_install_tool import (
    SkillInstallTool,
    SkillInstallTrustPolicy,
)
from endless_task.workspace_runtime.skill_search_tool import SkillSearchTool
from endless_task.workspace_runtime.system_terminal import open_system_terminal
from endless_task.workspace_runtime.terminal import (
    DEFAULT_COLS as TERMINAL_DEFAULT_COLS,
    DEFAULT_ROWS as TERMINAL_DEFAULT_ROWS,
    TerminalService,
)
from endless_task.workspace_runtime.terminal_tools import (
    TERMINAL_TOOL_NAMES,
    build_terminal_tools,
)
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
from endless_task.memory import (
    UserProfileService,
    MemoryConflictService,
    MemoryConsolidationService,
    MemoryForgettingService,
    MemoryProposalService,
    MemoryReflectionService,
    ReflectionTerminalObserver,
)
from endless_task.tasks import (
    TaskNotificationService,
    TaskProposalService,
    TaskRunReviewService,
    TaskScheduler,
    TaskWorker,
)
from endless_task.storage import (
    GENERAL_SCOPE_KEY,
    SqliteMemoryConsolidationRepository,
    SqliteUserProfileRepository,
    SqliteMemoryReflectionRepository,
    SqliteUndoJournalRepository,
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
    SqliteResponseFeedbackRepository,
    SqliteVecSearch,
    SqliteTaskRunRepository,
    SqliteWorkspaceRepository,
    SqliteHubEventRepository,
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
from endless_task.storage.sqlite_skill_provenance_repository import (
    SqliteSkillProvenanceRepository,
)
from endless_task.storage.sqlite_skill_usage_repository import (
    SqliteSkillUsageRepository,
)
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.storage.sqlite_knowledge_repository import scope_tier
from endless_task.skills import (
    INVOKE_OK,
    INVOKE_UNKNOWN,
    Skill,
    SkillScope,
    SkillService,
    build_available_skills_prompt,
    parse_skill_commands,
)
from .container import AppContainer
from .errors import ApiRequestError, correlation_id, error_response
from .audit_support import audit_fact_from_event
from .hub_support import (
    hub_append_memory_consolidated,
    hub_append_proposal_resolved,
    hub_event_sse,
)
from .knowledge_support import emit_knowledge_duplicates
from .v2_conversation_support import (
    resolve_runtime_v2_conversation,
    title_conversation_from_first_message,
)
from .runtime_v2_support import (
    runtime_v2_lane_json,
    runtime_v2_memory_json,
    runtime_v2_memory_promotion_json,
    runtime_v2_product_sse,
    runtime_v2_run_variant_json,
)
from .routes.v2_runs import register_v2_runs_routes
from .schemas.v2_runs import (
    ResendRuntimeV2RunBody,
    RuntimeV2RecoveryBody,
    RuntimeV2RunMemoryBody,
    RuntimeV2SteerBody,
)
from .routes.v2_lanes import register_v2_lanes_routes
from .schemas.v2_lanes import (
    RuntimeV2RenameLaneBody,
)
from .routes.v2_memory import register_v2_memory_routes
from .schemas.v2_memory import (
    RuntimeV2MemoryPromotionBody,
    RuntimeV2MemoryPromotionResolveBody,
)
from .routes.v2_misc import register_v2_misc_routes
from .schemas.v2_misc import (
    ResolveApprovalBody,
)
from .routes.knowledge import register_knowledge_routes
from .schemas.knowledge import (
    KnowledgeSourceBody,
    KnowledgeSourcePatch,
)
from .routes.artifacts import register_artifacts_routes
from .schemas.artifacts import (
    CreateArtifactVersionBody,
    RollbackArtifactBody,
)
from .routes.mcp import register_mcp_routes
from .schemas.mcp import (
    McpServerBody,
    McpServerPatchBody,
)
from .routes.retrieval import register_retrieval_routes
from .schemas.retrieval import (
    RetrievalEventBody,
)
from .routes.proposals import register_proposals_routes
from .routes.reminders import register_reminders_routes
from .routes.conversations import register_conversations_routes
from .schemas.conversations import (
    ConversationPatch,
    CreateBranchBody,
    CreateConversationBody,
)
from .routes.userprofile import register_userprofile_routes
from .schemas.userprofile import (
    UserProfileBody,
)
from .routes.notifications import register_notifications_routes
from .routes.filesystem import register_filesystem_routes
from .schemas.filesystem import (
    RevealPathBody,
)
from .routes.workspaces import register_workspaces_routes
from .schemas.workspaces import (
    TerminalCreateBody,
    WorkspaceBody,
    WorkspaceFileWriteBody,
    WorkspacePatchBody,
)
from .routes.providers import register_providers_routes
from .schemas.providers import (
    ProviderDefaultBody,
    ProviderDefaultModelBody,
    ProviderModelBody,
    ProviderModelPatchBody,
    ProviderProfileBody,
    ProviderProfilePatchBody,
)
from .routes.memories import register_memories_routes
from .schemas.memories import (
    UpdateMemoryBody,
)
from .routes.tasks import register_tasks_routes
from .routes.system import register_system_routes
from .workspace_support import (
    require_bound_workspace,
    resolve_workspace_reference,
)
from .schemas.system import SetPermissionBody
from .routes.skills import register_skill_routes
from .schemas.skills import (
    SkillCasesRunBody,
    SkillCreateBody,
    SkillImportBody,
    SkillInstallBody,
    SkillPatchBody,
    SkillSearchBody,
    SkillValidateBody,
)
from .skill_requests import resolve_skill_requests

from endless_task.tooling import (
    ApprovalStatus,
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolError,
    ToolRegistry,
)

from .serialization import (
    artifact_json,
    artifact_proposal_json,
    artifact_version_json,
    memory_proposal_json,
    knowledge_proposal_json,
    knowledge_source_json,
    memory_record_json,
    memory_reflection_json,
    undo_entry_json,
    workspace_json,
    conversation_json,
    conversation_snapshot_json,
    uploaded_text_file_json,
    task_json,
    task_proposal_json,
    notification_json,
    reminder_json,
    task_run_json,
    hub_event_json,
)


logger = logging.getLogger(__name__)
TERMINAL_TURN_STATUSES = {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED}
#: 默认系统提示词。A7：引导模型对"多项对比/指标/步骤"用带语言标记的围栏块输出，
#: 前端会渲染成图表/指标卡/步骤清单（无法结构化时照常写 Markdown）。
DEFAULT_SYSTEM_PROMPT = (
    "你是 Endless Task，一个可靠、简洁的个人助手。\n"
    "- 能直接回答的问题用自然语言直接回答，不要调用工具。\n"
    "- 信息不足时先向用户追问关键信息，不要猜测。\n"
    "- 只有问题确实需要会话附件内容时才调用文件工具。\n"
    "- 文件操作优先用类型化工具：改已有文件里的一处用 edit_workspace_file（默认要求"
    "唯一匹配，改完会返回 diff）；移动/重命名/复制/建目录用 manage_workspace_paths；"
    "整文件新建或覆盖用 write_workspace_file；只有需要跑程序、构建、测试、装依赖或"
    "文本变换时才用 run_shell。\n"
    "- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。\n"
    "- 用户要求产出文档/文件（如写 README、报告、脚本）时，读完所需材料后应立即调用"
    "写入工具或直接给出完整结果，不要无休止地继续收集资料；读完即动手。\n"
    "- 当回答包含多项数值对比、指标汇总或步骤清单时，用带语言标记的围栏代码块给出"
    "结构化结果（前端会渲染成图表/指标卡/步骤清单），JSON 必须合法：\n"
    "  chart: {\"title\": \"\", \"unit\": \"\", \"series\": [{\"label\": \"\", \"value\": 0}]}\n"
    "  metrics: {\"title\": \"\", \"items\": [{\"label\": \"\", \"value\": \"\", \"hint\": \"\"}]}\n"
    "  steps: {\"title\": \"\", \"steps\": [{\"title\": \"\", \"detail\": \"\", \"done\": false}]}\n"
    "  其余内容照常写 Markdown；结构化块只是补充，不要用它替代解释。"
)

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
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    system_prompt_version: str = "p1-v2"
    # 生产默认：128K 窗口 / 4K 输出。DeepSeek V4 系列实测窗口 1,048,576 tokens，
    # 但个人助手用 128K 已可容纳 40+ 轮工具调用；长任务可通过环境变量上调
    # （推荐 262144；极限 524288 需配合成本上限与上下文缓存）。
    context_window_tokens: int = 131_072
    max_output_tokens: int = 4_096
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
    # S5：默认技能目录预算（展开条目数 / 总字符）。见 05 文档 §3.2。
    skill_catalog_limit: int = 8
    skill_catalog_budget: int = 3_000
    # S8：生态安装权限（ask=逐次审批 / allow=用户已全局授权 / deny=禁止）。
    skill_install_mode: str = "ask"
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
    embedding_sqlite_vec: bool = False
    hybrid_literal_weight: float = 0.4
    # B1 近期性偏置：0 = 纯相关性（默认，行为不变）；τ 单位天。
    memory_recency_weight: float = 0.0
    memory_decay_tau_days: float = 30.0
    # B3 重要性加权遗忘：默认关闭（不自动遗忘用户记忆），打开后按间隔巡检。
    memory_forgetting_enabled: bool = False
    memory_forgetting_interval_hours: float = 24.0
    memory_forgetting_max_per_run: int = 10
    # B4 反思：终态运行（含失败）自动归纳洞见（走提案确认，默认开）。
    reflection_enabled: bool = True
    # B2 记忆巩固：相似度阈值与每次最多产出的巩固提案数。
    memory_consolidation_threshold: float = 0.5
    memory_consolidation_max_per_run: int = 3
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
    # G1 item 5: no-progress StopPolicy enforcement (05 §RS-2). Default off;
    # thresholds approved (remind=1/restrict=2/stop=3 consecutive signals).
    # "1" turns on the safety stop for runs that make no progress.
    stop_policy_enforcement: bool = False
    # C4: 上下文预算用掉多少比例就升级人工（无进展升级与阈值无关）。
    escalation_budget_ratio: float = 0.85
    # C1: 独立验证模式 —— 0 关（默认）/ 1 所有运行 / side_effects 仅有副作用的
    # 关键运行；verifier_model 为空时用主模型（上下文与 prompt 仍完全独立）。
    verifier_mode: str = "0"
    verifier_model: Optional[str] = None
    # C5: 单次运行的估算成本上限（USD，0 = 不限制）；超限走 C4 升级提示。
    cost_cap_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.config_version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}"
            )
        if self.provider_timeout_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("Timeout values must be positive")
        if self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("Context window must be larger than max output tokens")
        if self.cost_cap_usd < 0:
            raise ValueError("ENDLESS_TASK_COST_CAP_USD 不能为负数")
        if self.verifier_mode not in VERIFIER_MODES:
            raise ValueError(
                "ENDLESS_TASK_VERIFIER 只允许 0 | 1 | side_effects"
            )
        if not 0 < self.escalation_budget_ratio <= 1:
            raise ValueError("Escalation budget ratio must be within (0, 1]")
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
        if not 0 <= self.memory_recency_weight <= 1:
            raise ValueError("ENDLESS_TASK_MEMORY_RECENCY_WEIGHT must be within [0, 1]")
        if self.memory_decay_tau_days <= 0:
            raise ValueError("ENDLESS_TASK_MEMORY_DECAY_TAU must be positive")
        if self.memory_forgetting_interval_hours <= 0:
            raise ValueError("ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS must be positive")
        if not 0 < self.memory_consolidation_threshold <= 1:
            raise ValueError("ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD must be within (0, 1]")
        if self.memory_consolidation_max_per_run < 1:
            raise ValueError("ENDLESS_TASK_MEMORY_CONSOLIDATION_MAX_PER_RUN must be >= 1")
        if self.memory_forgetting_max_per_run < 1:
            raise ValueError("ENDLESS_TASK_MEMORY_FORGETTING_MAX_PER_RUN must be >= 1")
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
            skill_install_mode=_parse_skill_install_mode(
                env.get("ENDLESS_TASK_SKILL_INSTALL", "ask")
            ),
            skill_catalog_limit=int(
                os.environ.get("ENDLESS_TASK_SKILL_CATALOG_LIMIT") or 8
            ),
            skill_catalog_budget=int(
                os.environ.get("ENDLESS_TASK_SKILL_CATALOG_BUDGET") or 3_000
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
            stop_policy_enforcement=_parse_strict_flag(
                env.get("ENDLESS_TASK_STOP_POLICY_V2", "0"),
                name="ENDLESS_TASK_STOP_POLICY_V2",
            ),
            escalation_budget_ratio=_parse_ratio(
                env.get("ENDLESS_TASK_ESCALATION_BUDGET_RATIO", "0.85"),
                name="ENDLESS_TASK_ESCALATION_BUDGET_RATIO",
            ),
            verifier_mode=_parse_verifier_mode(
                env.get("ENDLESS_TASK_VERIFIER", "0"),
            ),
            verifier_model=(
                env.get("ENDLESS_TASK_VERIFIER_MODEL", "").strip() or None
            ),
            cost_cap_usd=_parse_non_negative_float(
                env.get("ENDLESS_TASK_COST_CAP_USD", "0"),
                name="ENDLESS_TASK_COST_CAP_USD",
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
            embedding_sqlite_vec=_parse_flag(env.get("ENDLESS_TASK_SQLITE_VEC", "0")),
            hybrid_literal_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[0],
            hybrid_semantic_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[1],
            memory_recency_weight=_parse_unit_ratio(
                env.get("ENDLESS_TASK_MEMORY_RECENCY_WEIGHT", "0"),
                name="ENDLESS_TASK_MEMORY_RECENCY_WEIGHT",
            ),
            memory_decay_tau_days=_parse_positive_float(
                env.get("ENDLESS_TASK_MEMORY_DECAY_TAU", "30"),
                name="ENDLESS_TASK_MEMORY_DECAY_TAU",
            ),
            memory_forgetting_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING", "0")
            ),
            reflection_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_REFLECTION", "1")
            ),
            memory_forgetting_interval_hours=_parse_positive_float(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS", "24"),
                name="ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS",
            ),
            memory_forgetting_max_per_run=int(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING_MAX_PER_RUN", "10")
            ),
            memory_consolidation_threshold=_parse_ratio(
                env.get("ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD", "0.5"),
                name="ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD",
            ),
            memory_consolidation_max_per_run=int(
                env.get("ENDLESS_TASK_MEMORY_CONSOLIDATION_MAX_PER_RUN", "3")
            ),
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
                DEFAULT_SYSTEM_PROMPT,
            ),
            system_prompt_version=env.get(
                "ENDLESS_TASK_SYSTEM_PROMPT_VERSION",
                "p1-v3",
            ),
            context_window_tokens=int(
                env.get("ENDLESS_TASK_CONTEXT_WINDOW_TOKENS", "131072")
            ),
            max_output_tokens=int(
                env.get("ENDLESS_TASK_MAX_OUTPUT_TOKENS", "4096")
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


class ResolveMemoryProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveKnowledgeProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
    workspaceId: Optional[str] = None


class ResolveArtifactProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveTaskProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class _CompositeTrustPolicy:
    """把多个信任策略合成一个（任一放行即放行）。"""

    def __init__(self, *policies: object) -> None:
        self._policies = policies

    def allows(self, tool_name: str, call: object) -> bool:
        for policy in self._policies:
            allows = getattr(policy, "allows", None)
            if not callable(allows):
                continue
            try:
                if allows(tool_name, call):
                    return True
            except Exception:  # noqa: BLE001 单个策略异常按"不放行"
                continue
        return False


def _parse_skill_install_mode(raw: str) -> str:
    """S8：`ENDLESS_TASK_SKILL_INSTALL=ask|allow|deny`；非法值启动即失败。"""
    value = (raw or "ask").strip().lower()
    if value not in ("ask", "allow", "deny"):
        raise ValueError(
            "ENDLESS_TASK_SKILL_INSTALL 只能是 ask / allow / deny。"
        )
    return value


def _skill_home_dir() -> Path:
    """共享技能根使用的 home；`ENDLESS_TASK_SKILL_HOME` 可覆盖（测试隔离用）。"""
    override = (os.environ.get("ENDLESS_TASK_SKILL_HOME") or "").strip()
    return Path(override).expanduser() if override else Path.home()


def _parse_skill_dirs(raw: Optional[str]) -> tuple[Path, ...]:
    """S4：`ENDLESS_TASK_SKILL_DIRS`（冒号分隔）→ 额外技能根。"""
    if not raw:
        return ()
    parts = [item.strip() for item in raw.split(":")]
    return tuple(Path(item).expanduser() for item in parts if item)












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


def _parse_positive_float(value: str, *, name: str) -> float:
    """Strict positive float parsing (B1 decay tau)."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是正数") from error
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正数")
    return parsed


def _parse_unit_ratio(value: str, *, name: str) -> float:
    """Strict [0, 1] ratio parsing (0 = feature off)."""
    parsed = _parse_non_negative_float(value, name=name)
    if parsed > 1:
        raise ValueError(f"{name} 必须在 [0, 1] 之间")
    return parsed


def _parse_non_negative_float(value: str, *, name: str) -> float:
    """Strict non-negative float parsing (0 disables the limit)."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是非负数字") from error
    if parsed < 0:
        raise ValueError(f"{name} 必须是非负数字")
    return parsed


def _parse_verifier_mode(value: str) -> str:
    """Strict verifier mode parsing (C1): 0 | 1 | side_effects."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "all"}:
        return "1"
    if normalized in {"side_effects", "side-effects"}:
        return "side_effects"
    raise ValueError("ENDLESS_TASK_VERIFIER 只允许 0 | 1 | side_effects")


def _parse_ratio(value: str, *, name: str) -> float:
    """Strict (0, 1] ratio parsing: illegal env values fail startup."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是 (0, 1] 之间的小数") from error
    if not 0 < parsed <= 1:
        raise ValueError(f"{name} 必须是 (0, 1] 之间的小数")
    return parsed


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
    memory_reflection_repository = SqliteMemoryReflectionRepository(database)
    user_profile_repository = SqliteUserProfileRepository(database)
    undo_journal_repository = SqliteUndoJournalRepository(database)
    memory_consolidation_repository = SqliteMemoryConsolidationRepository(database)
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
    hub_event_repository = SqliteHubEventRepository(database)
    reminder_repository = SqliteReminderRepository(database)
    knowledge_proposal_repository = SqliteKnowledgeProposalRepository(database)
    retrieval_event_repository = SqliteRetrievalEventRepository(database)
    response_feedback_repository = SqliteResponseFeedbackRepository(database)
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
        recency_weight=settings.memory_recency_weight,
        decay_tau_days=settings.memory_decay_tau_days,
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
                vec_search=(
                    SqliteVecSearch(database)
                    if settings.embedding_sqlite_vec
                    else None
                ),
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
    user_profile_service = UserProfileService(
        profile_repository=user_profile_repository,
        memory_repository=runtime_v2_memory_repository,
    )
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
    def _user_profile_provider(conversation_id: str):
        """B5：运行开始时读一次已存画像（只读，不重算，保证前缀稳定）。"""
        try:
            workspace_id = chat_repository.get_conversation(
                conversation_id
            ).workspace_id
        except Exception:  # noqa: BLE001
            workspace_id = None
        scope_key = workspace_id or GENERAL_SCOPE_KEY
        # 运行前对齐：内容未变不写库；被取代/过期的记忆会立刻从画像里消失。
        return user_profile_service.ensure_current(scope_key)

    def _refresh_user_profile(conversation_id: str) -> None:
        """B5：会话结束时更新画像（内部有节流；内容没变则完全不写库）。"""
        try:
            workspace_id = chat_repository.get_conversation(
                conversation_id
            ).workspace_id
        except Exception:  # noqa: BLE001
            workspace_id = None
        user_profile_service.refresh(workspace_id or GENERAL_SCOPE_KEY)

    # B4 反思：任何终态运行（含失败）都走 fanout 触发一次反思（fail-open）。
    memory_reflection_service = MemoryReflectionService(
        runtime_repository=runtime_v2_repository,
        memory_repository=memory_repository,
        proposal_repository=proposal_repository,
        reflection_repository=memory_reflection_repository,
        hub_event_sink=lambda conversation_id, count: hub_event_repository.append(
            "proposal.pending",
            conversation_id=conversation_id,
            data={
                "kind": "memory",
                "conversationId": conversation_id,
                "count": count,
            },
        ),
    )
    if settings.reflection_enabled:
        trace_fanout_members.append(
            ReflectionTerminalObserver(memory_reflection_service)
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
            EnforcementUnsupportedTool,
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

    # S8 只读信任策略：默认关闭（全部命令逐条确认，行为不变）。
    from endless_task.workspace_runtime.shell_trust import (
        ShellTrustPolicy,
        trust_enabled_from_env,
    )

    shell_trust_policy = ShellTrustPolicy(
        enabled=trust_enabled_from_env(
            os.environ.get("ENDLESS_TASK_SHELL_TRUST_READONLY")
        ),
        extra_prefixes=tuple(
            part.strip()
            for part in os.environ.get(
                "ENDLESS_TASK_SHELL_TRUSTED_PREFIXES", ""
            ).split(",")
            if part.strip()
        ),
    )
    # S9 shell 沙箱：off（默认，本机直跑）/ seatbelt / container。
    # 显式开启但沙箱不可用时**拒绝启动**（无静默降级）。
    # S4/S5：终端服务实例（面板与模型工具共用同一份会话注册表）。
    terminal_service = TerminalService()
    shell_sandbox_backend = None
    shell_sandbox_network_mode = None
    _shell_sandbox = (os.environ.get("ENDLESS_TASK_SHELL_SANDBOX") or "off").strip().lower()
    if _shell_sandbox not in {"", "off"}:
        from endless_task.execution_env import NetworkMode

        _network = (
            NetworkMode.ALLOW_ALL
            if (os.environ.get("ENDLESS_TASK_SHELL_SANDBOX_NETWORK") or "deny").strip().lower()
            == "allow"
            else NetworkMode.DENY
        )
        if _shell_sandbox == "seatbelt":
            import shutil as _shutil

            from endless_task.execution_env import SeatbeltBackend, probe_seatbelt

            _sandbox_exec = _shutil.which("sandbox-exec")
            if not _sandbox_exec or not probe_seatbelt(_sandbox_exec):
                raise ValueError(
                    "ENDLESS_TASK_SHELL_SANDBOX=seatbelt 但 sandbox-exec 无法应用沙箱；"
                    "已拒绝启动（不降级为本机执行）。"
                )
            shell_sandbox_backend = SeatbeltBackend(sandbox_exec=_sandbox_exec)
            shell_sandbox_network_mode = _network
        elif _shell_sandbox == "container":
            from endless_task.execution_env import ContainerExecutionBackend

            _image = (os.environ.get("ENDLESS_TASK_SHELL_SANDBOX_IMAGE") or "").strip()
            if not _image:
                raise ValueError(
                    "ENDLESS_TASK_SHELL_SANDBOX=container 需要同时设置 "
                    "ENDLESS_TASK_SHELL_SANDBOX_IMAGE。"
                )
            shell_sandbox_backend = ContainerExecutionBackend(image=_image)
            shell_sandbox_network_mode = _network
        else:
            raise ValueError(
                f"不支持的 ENDLESS_TASK_SHELL_SANDBOX 取值：{_shell_sandbox}"
                "（可选 off/seatbelt/container）。"
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
        tool_trust_policy=_CompositeTrustPolicy(
            shell_trust_policy,
            # S8：allow 模式下用户已全局授权生态安装（每次仍需协议层 REQUIRED，
            # 由信任策略放行并记录证据）。
            SkillInstallTrustPolicy(mode=settings.skill_install_mode),
        ),
        no_progress_enforcement_enabled=settings.stop_policy_enforcement,
        escalation_budget_ratio=settings.escalation_budget_ratio,
        verifier_mode=settings.verifier_mode,
        verifier_model=settings.verifier_model,
        user_profile_provider=_user_profile_provider,
        cost_cap_usd=settings.cost_cap_usd,
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
        no_progress_enforcement_enabled=settings.stop_policy_enforcement,
        escalation_budget_ratio=settings.escalation_budget_ratio,
        verifier_mode=settings.verifier_mode,
        verifier_model=settings.verifier_model,
        user_profile_provider=_user_profile_provider,
        cost_cap_usd=settings.cost_cap_usd,
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
                no_progress_enforcement_enabled=settings.stop_policy_enforcement,
                escalation_budget_ratio=settings.escalation_budget_ratio,
                verifier_mode=settings.verifier_mode,
                verifier_model=settings.verifier_model,
                user_profile_provider=_user_profile_provider,
                cost_cap_usd=settings.cost_cap_usd,
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
    skill_usage_repository = SqliteSkillUsageRepository(database)
    skill_provenance_repository = SqliteSkillProvenanceRepository(database)
    skill_service = SkillService(
        user_dir=settings.database_path.parent / "skills",
        extra_skill_dirs=_parse_skill_dirs(os.environ.get("ENDLESS_TASK_SKILL_DIRS")),
        home_dir=_skill_home_dir(),
        database_path=settings.database_path,
        override_repository=skill_override_repository,
        usage_repository=skill_usage_repository,
    )

    def _record_skill_usage(
        skill: Skill, kind: str, *, workspace_id: str = ""
    ) -> None:
        """S2：使用统计写入（失败不影响主流程）。"""
        del workspace_id
        if not skill.digest:
            return
        try:
            skill_usage_repository.record(
                scope=skill.scope.value,
                name=skill.name,
                digest=skill.digest,
                kind=kind,
            )
        except Exception:  # noqa: BLE001 统计失败不阻断
            logger.debug("skill usage record failed", exc_info=True)

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
        # S5：默认目录只放精选技能（工作区 + pin + 最近常用），其余折叠成
        # 一行"能力感知"，由 skill_search 兜底。
        selected, folded = skill_service.catalog_skills(
            root,
            workspace_id=workspace_id or "",
            limit=settings.skill_catalog_limit,
        )
        for skill in selected:
            _record_skill_usage(skill, "surfaced", workspace_id=workspace_id or "")
        return build_available_skills_prompt(
            selected,
            folded=folded,
            budget=settings.skill_catalog_budget,
            locator_mode=settings.skill_packages_enabled,
        )

    def skill_name_resolver(conversation_id: str):
        """S3：按技能名解析到 SKILL.md（目录不再暴露路径）。

        只返回"可见"技能（合法 + 未禁用）；SKILL.md 的根包含性仍由工具侧
        ``resolve_external_read_path`` 兜底。
        """

        def resolve(name: str) -> Optional[Path]:
            binding = workspace_resolver.resolve_binding(conversation_id)
            root = binding.root if binding is not None else None
            workspace_id = binding.workspace_id if binding is not None else ""
            for skill in skill_service.visible_skills(
                root, workspace_id=workspace_id
            ):
                if skill.name == name:
                    return skill.file_path
            return None

        return resolve

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
    undo_service = WorkspaceUndoService(
        repository=undo_journal_repository,
        effect_log=effect_log,
    )
    def _mcp_workspace_root(conversation_id: str):
        """M2：MCP 图片落盘需要工作区根（未绑定则不给）。"""
        try:
            binding = workspace_resolver.resolve_binding(conversation_id)
        except Exception:  # noqa: BLE001 未绑定/已删除都按"没有工作区"处理
            return None
        return binding.root if binding is not None else None

    mcp_manager = McpManager(
        repository=mcp_server_repository,
        tool_registry=selected_tool_registry,
        effect_log=effect_log,
        workspace_root_provider=_mcp_workspace_root,
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
                undo_service=undo_service,
            )
        )
        selected_tool_registry.register(
            EditWorkspaceFileTool(
                workspace_resolver,
                effect_log,
                max_write_bytes=settings.workspace_max_write_bytes,
                checkpoint_coordinator=run_checkpoint_coordinator,
                undo_service=undo_service,
            )
        )
        selected_tool_registry.register(
            ManageWorkspacePathsTool(
                workspace_resolver,
                effect_log,
                max_write_bytes=settings.workspace_max_write_bytes,
                checkpoint_coordinator=run_checkpoint_coordinator,
                undo_service=undo_service,
            )
        )
        selected_tool_registry.register(ListWorkspaceDirTool(workspace_resolver))
        selected_tool_registry.register(
            WorkspaceSearchTool(
                workspace_resolver,
                max_file_bytes=settings.max_file_bytes,
            )
        )
        selected_tool_registry.register(
            ReadSkillFileTool(
                skill_roots_for_conversation,
                locator_resolver_provider=skill_locator_resolver,
                name_resolver_provider=skill_name_resolver,
                max_file_bytes=settings.max_file_bytes,
            )
        )
        # S6：目录只列精选技能，其余靠检索发现（Level 1）。
        selected_tool_registry.register(
            SkillSearchTool(workspace_resolver, skill_service)
        )
        # S8：生态安装（Level 2）。deny 模式不注册（模型侧看不到）。
        if settings.skill_install_mode != "deny":
            selected_tool_registry.register(
                SkillInstallTool(
                    skill_service,
                    skill_provenance_repository,
                    mode=settings.skill_install_mode,
                    # 只装进应用自己的可写技能目录；共享目录（~/.claude/skills 等）只读。
                    target_root=settings.database_path.parent / "skills",
                )
            )
        selected_tool_registry.register(
            DeleteWorkspaceFileTool(
                workspace_resolver,
                effect_log,
                checkpoint_coordinator=run_checkpoint_coordinator,
                undo_service=undo_service,
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
                undo_service=undo_service,
                execution_backend=shell_sandbox_backend,
                sandbox_network_mode=shell_sandbox_network_mode,
            )
        )
        if (os.environ.get("ENDLESS_TASK_TERMINAL_TOOLS") or "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            # S5 模型侧终端工具：默认关闭，避免给不用的会话平添工具面开销。
            for terminal_tool in build_terminal_tools(
                workspace_resolver, terminal_service
            ):
                selected_tool_registry.register(terminal_tool)
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
                        undo_service=undo_service,
                    ),
                    "delete_workspace_file": DeleteWorkspaceFileTool(
                        workspace_resolver,
                        effect_log,
                        checkpoint_coordinator=run_checkpoint_coordinator,
                        execution_backend=enforcement_backend,
                        undo_service=undo_service,
                    ),
                    # edit / path 工具尚未 backend 化：enforcement 激活时
                    # fail closed（拒绝执行），绝不允许绕过隔离直接写宿主。
                    "edit_workspace_file": EnforcementUnsupportedTool(
                        EditWorkspaceFileTool(
                            workspace_resolver,
                            effect_log,
                            max_write_bytes=settings.workspace_max_write_bytes,
                            checkpoint_coordinator=run_checkpoint_coordinator,
                            undo_service=undo_service,
                        )
                    ),
                    "manage_workspace_paths": EnforcementUnsupportedTool(
                        ManageWorkspacePathsTool(
                            workspace_resolver,
                            effect_log,
                            max_write_bytes=settings.workspace_max_write_bytes,
                            checkpoint_coordinator=run_checkpoint_coordinator,
                            undo_service=undo_service,
                        )
                    ),
                }
            )
    if settings.delegation_mode == "isolated_write" and tool_registry is None:
        # M4B P3: explicit apply of an isolated child's scratch output.
        import hashlib

        from endless_task.delegation.apply_tool import (
            ApplyChildPatchesTool,
            ChildPatchFile,
            ChildPatchSource,
        )
        _MAX_PATCH_TOTAL_BYTES = 4 * 1024 * 1024

        def _delegation_child_patches(child_run_id: str) -> ChildPatchSource:
            workspace_id = delegation_handler.child_workspace_id_for(child_run_id)
            workspace = workspace_repository.get_workspace(workspace_id)
            root = Path(workspace.root_path).expanduser().resolve()
            files: list[ChildPatchFile] = []
            total = 0
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                data = path.read_bytes()
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ToolError(
                        "patch_binary_unsupported",
                        f"子代理产出 {relative} 非 UTF-8 文本，暂不支持应用。",
                        retryable=False,
                    ) from error
                total += len(data)
                if total > _MAX_PATCH_TOTAL_BYTES:
                    raise ToolError(
                        "patch_too_large",
                        "子代理产出超过应用上限（4 MiB）。",
                        retryable=False,
                    )
                files.append(
                    ChildPatchFile(
                        relative=relative,
                        content=data,
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
            return ChildPatchSource(files=tuple(files), total_bytes=total)

        apply_write_tool = WriteWorkspaceFileTool(
            workspace_resolver,
            effect_log,
            max_write_bytes=settings.workspace_max_write_bytes,
            checkpoint_coordinator=run_checkpoint_coordinator,
            undo_service=undo_service,
        )

        async def _delegation_apply_writer(
            call, relative: str, content: bytes
        ) -> str:
            from endless_task.runtime.cancellation import CancellationToken
            from endless_task.workspace_runtime.path_safety import (
                resolve_workspace_path,
            )

            binding = workspace_resolver.require_binding(call.conversation_id)
            target = resolve_workspace_path(binding.root, relative).canonical
            if target.exists():
                if target.read_bytes() == content:
                    return "equal"
                raise ToolError(
                    "path_exists_conflict",
                    f"主工作区已有 {relative}（内容不同），不覆盖；请处理后再试。",
                    retryable=False,
                )
            text = content.decode("utf-8")
            write_call = ToolCall(
                id=f"apply:{relative}",
                conversation_id=call.conversation_id,
                turn_id=call.turn_id,
                response_variant_id=call.response_variant_id,
                tool_name="write_workspace_file",
                arguments={"path": relative, "content": text},
                status=ToolCallStatus.RUNNING,
                created_at="2026-09-05T00:00:00.000Z",
            )
            await apply_write_tool.execute(write_call, CancellationToken())
            return "applied"

        selected_tool_registry.register(
            ApplyChildPatchesTool(
                patches_provider=_delegation_child_patches,
                writer=_delegation_apply_writer,
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
        base = context_builder.build_system_messages(
            conversation_id,
            user_content,
            turn_id=run_id,
            workspace_id=conversation.workspace_id,
            # v2 记忆统一由 <runtime-memory> 注入，S3 层不再叠加 v1 记忆，
            # 避免 v1/v2 记忆双重注入（v1 存量已由 migration 049 迁入 v2）。
            include_v1_memory=False,
        )
        # S1：用户显式调用技能（/技能名）→ 以 user-role 注入正文，
        # 让技能内容不获得 system 级权威；名称不存在时静默忽略。
        skill_messages, notices = resolve_skill_requests(
            skill_service,
            workspace_resolver,
            conversation_id,
            user_content,
            include_bodies=True,
            usage_recorder=_record_skill_usage,
        )
        if skill_messages or notices:
            runtime_v2_repository.append_runtime_event(
                run_id=run_id,
                event_type="skill_requested",
                payload={"notices": notices},
            )
        return (*base, *skill_messages)

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
    memory_forgetting_service = MemoryForgettingService(
        memory_repository=memory_repository,
        tau_days=settings.memory_decay_tau_days,
        max_per_run=settings.memory_forgetting_max_per_run,
    )
    memory_consolidation_service = MemoryConsolidationService(
        memory_repository=memory_repository,
        proposal_repository=proposal_repository,
        consolidation_repository=memory_consolidation_repository,
        threshold=settings.memory_consolidation_threshold,
        max_clusters_per_run=settings.memory_consolidation_max_per_run,
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

                    created_memories = (
                        await memory_proposal_service.generate_for_turn(
                            conversation_id=run.conversation_id,
                            turn_id=run.id,
                            user_message=user_message,
                            assistant_message=assistant_message,
                            auto_write_target=_write_v2_user_global_memory,
                        )
                    )
                    if created_memories:
                        hub_event_repository.append(
                            "proposal.pending",
                            conversation_id=run.conversation_id,
                            data={
                                "kind": "memory",
                                "conversationId": run.conversation_id,
                                "count": len(created_memories),
                            },
                        )
            if knowledge_proposal_service is not None and not ephemeral:
                if proposal_budget is None or proposal_budget.allow(
                    run.conversation_id
                ):
                    created = (
                        await knowledge_proposal_service.generate_for_turn(
                            conversation_id=run.conversation_id,
                            turn_id=run.id,
                            user_message=user_message,
                            assistant_message=assistant_message,
                        )
                    )
                    if created:
                        hub_event_repository.append(
                            "proposal.pending",
                            conversation_id=run.conversation_id,
                            data={
                                "kind": "knowledge",
                                "conversationId": run.conversation_id,
                                "count": len(created),
                            },
                        )
            if artifact_proposal_service is not None:
                created = await artifact_proposal_service.generate_for_turn(
                    conversation_id=run.conversation_id,
                    turn_id=run.id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
                if created:
                    hub_event_repository.append(
                        "proposal.pending",
                        conversation_id=run.conversation_id,
                        data={
                            "kind": "artifact",
                            "conversationId": run.conversation_id,
                            "count": len(created),
                        },
                    )
            if task_proposal_service is not None:
                created = await task_proposal_service.generate_for_turn(
                    conversation_id=run.conversation_id,
                    turn_id=run.id,
                    user_message=user_message,
                    assistant_message=assistant_message,
                )
                if created:
                    hub_event_repository.append(
                        "proposal.pending",
                        conversation_id=run.conversation_id,
                        data={
                            "kind": "task",
                            "conversationId": run.conversation_id,
                            "count": len(created),
                        },
                    )

            # B5：会话结束更新用户画像（节流 + 签名比对，通常不写库）。
            _refresh_user_profile(run.conversation_id)

        runtime_v2_gateway.set_run_completion_callback(on_v2_run_completed)

    task_notification_service = None
    if settings.notifications_enabled:
        task_notification_service = TaskNotificationService(
            notification_repository=notification_repository,
            hub_events=hub_event_repository,
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
        hub_event_repository=hub_event_repository,
        reminder_repository=reminder_repository,
        knowledge_proposal_repository=knowledge_proposal_repository,
        knowledge_proposal_service=knowledge_proposal_service,
        knowledge_repository=knowledge_repository,
        retrieval_event_repository=retrieval_event_repository,
        response_feedback_repository=response_feedback_repository,
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
        memory_forgetting_service=memory_forgetting_service,
        memory_consolidation_service=memory_consolidation_service,
        undo_service=undo_service,
        memory_reflection_service=memory_reflection_service,
        user_profile_service=user_profile_service,
        undo_journal_repository=undo_journal_repository,
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
        terminal_service=terminal_service,
        skill_usage_repository=skill_usage_repository,
        skill_provenance_repository=skill_provenance_repository,
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


class ResponseFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: str
    reason: Optional[str] = None
    note: Optional[str] = None
    variantId: Optional[str] = None
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


async def _run_memory_forgetting_loop(container: "AppContainer") -> None:
    """B3：周期性遗忘巡检。默认关闭；重要记忆只会进入待确认，不会静默删除。"""
    interval_seconds = (
        max(container.settings.memory_forgetting_interval_hours, 0.25) * 3600.0
    )
    await asyncio.sleep(min(300.0, interval_seconds))
    while True:
        try:
            service = container.memory_forgetting_service
            if service is not None:
                service.run()
        except Exception:  # noqa: BLE001 周期巡检失败不影响服务
            logger.debug("Memory forgetting run failed", exc_info=True)
        await asyncio.sleep(interval_seconds)


# ---- 以下 helper 曾随 SetPermissionBody 被误搬进 schemas/system.py，已放回 ----
def _repository_error_status(error: RepositoryError) -> int:
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, (ConflictError, InvalidStateError)):
        return 409
    if isinstance(error, ValidationError):
        return 400
    return 500












#: 只有终态工具事件才进轨迹（同一次调用会产生多条状态事件）。






















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
            container.settings.memory_forgetting_enabled
            and container.memory_forgetting_service is not None
        ):
            lifespan_tasks.spawn(
                _run_memory_forgetting_loop(container),
                name="memory-forgetting",
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
            if container.terminal_service is not None:
                await container.terminal_service.close_all()
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
        return error_response(
            status_code=_repository_error_status(error),
            code=error.code,
            message=str(error),
        )

    @app.exception_handler(ApiRequestError)
    async def handle_api_error(_: Request, error: ApiRequestError) -> JSONResponse:
        return error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            details=error.details,
        )

    @app.exception_handler(FileError)
    async def handle_file_error(_: Request, error: FileError) -> JSONResponse:
        return error_response(
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
        return error_response(
            status_code=400,
            code="invalid_request",
            message="请求参数格式不正确。",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_: Request, error: Exception) -> JSONResponse:
        correlation = correlation_id()
        logger.exception("Unhandled local API error", extra={"correlation_id": correlation})
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "本地服务发生错误。",
                    "retryable": True,
                    "correlationId": correlation,
                }
            },
        )

    # 系统域路由搬到 api/routes/system.py（搬家不改行为）
    register_system_routes(app, container)

    # conversations 域路由搬到 api/routes/conversations.py（搬家不改行为）
    register_conversations_routes(app, container)











    # memories 域路由搬到 api/routes/memories.py（搬家不改行为）
    register_memories_routes(app, container)








    # ---------- P5 知识源与联合检索 ----------

    # knowledge 域路由搬到 api/routes/knowledge.py（搬家不改行为）
    register_knowledge_routes(app, container)


    # providers 域路由搬到 api/routes/providers.py（搬家不改行为）
    register_providers_routes(app, container)














    # mcp 域路由搬到 api/routes/mcp.py（搬家不改行为）
    register_mcp_routes(app, container)








    # v2 零散路由搬到 api/routes/v2_misc.py（搬家不改行为）
    register_v2_misc_routes(app, container)









    # 技能域路由搬到 api/routes/skills.py（搬家不改行为，见 05-code-health 方案）
    register_skill_routes(app, container)

    # workspaces 域路由搬到 api/routes/workspaces.py（搬家不改行为）
    register_workspaces_routes(app, container)












    # ---------- P1 文件编辑保存（工作区根内，乐观并发） ----------






    # filesystem 域路由搬到 api/routes/filesystem.py（搬家不改行为）
    register_filesystem_routes(app, container)












    # retrieval 域路由搬到 api/routes/retrieval.py（搬家不改行为）
    register_retrieval_routes(app, container)


    @app.get("/retrieval-stats")
    async def get_retrieval_stats() -> dict[str, object]:
        return {"stats": container.retrieval_event_repository.summarize()}

    @app.post("/turns/{turn_id}/feedback")
    async def record_response_feedback(
        turn_id: str,
        body: ResponseFeedbackBody,
    ) -> dict[str, object]:
        try:
            rating = FeedbackRating(body.rating)
        except ValueError:
            raise ApiRequestError(
                "invalid_rating",
                "rating 只能是 up 或 down。",
                status_code=422,
            )
        record = container.response_feedback_repository.upsert(
            conversation_id=body.conversationId or "",
            turn_id=turn_id,
            variant_id=body.variantId,
            rating=rating,
            reason=body.reason,
            note=body.note,
        )
        return {
            "id": record.id,
            "conversationId": record.conversation_id,
            "turnId": record.turn_id,
            "variantId": record.variant_id,
            "rating": record.rating.value,
            "reason": record.reason,
            "note": record.note,
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
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
                workspace_filter = resolve_workspace_reference(container, text)
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
            if container.memory_consolidation_service is not None:
                container.memory_consolidation_service.reject(proposal_id)
            if container.memory_reflection_service is not None:
                container.memory_reflection_service.reject(proposal_id)
            hub_append_proposal_resolved(
                container, kind="memory", proposal=proposal, decision="reject"
            )
            return {"proposal": memory_proposal_json(proposal)}
        proposal, memory = container.proposal_repository.accept_proposal(
            proposal_id
        )
        consolidated_ids: list[str] = []
        if container.memory_consolidation_service is not None:
            merged = container.memory_consolidation_service.finalize(
                proposal_id, memory
            )
            if merged:
                consolidated_ids = list(merged)
                hub_append_memory_consolidated(
                    container,
                    proposal=proposal,
                    memory=memory,
                    source_memory_ids=merged,
                )
        if container.memory_reflection_service is not None:
            container.memory_reflection_service.finalize(proposal_id, memory)
        hub_append_proposal_resolved(
            container, kind="memory", proposal=proposal, decision="accept"
        )
        if container.memory_conflict_service is not None:
            await container.memory_conflict_service.resolve_conflicts_for(memory)
        _mirror_confirmed_memory_to_v2(container, memory)
        return {
            "proposal": memory_proposal_json(proposal),
            "memory": memory_record_json(memory),
            "consolidatedMemoryIds": consolidated_ids,
        }

    # userprofile 域路由搬到 api/routes/userprofile.py（搬家不改行为）
    register_userprofile_routes(app, container)




    @app.get("/reflections")
    async def list_all_memory_reflections(
        include_resolved: bool = True,
        limit: int = 50,
    ) -> dict[str, object]:
        """B4：跨会话的反思记录（记忆面板用）。"""
        service = container.memory_reflection_service
        if service is None:
            return {"items": []}
        return {
            "items": [
                memory_reflection_json(record)
                for record in service.list_records(
                    include_resolved=include_resolved, limit=limit
                )
            ]
        }



    @app.post("/undo-journal/{entry_id}/undo")
    async def undo_journal_entry(entry_id: str) -> dict[str, object]:
        """A5：撤销一次可逆的文件操作（幂等）。"""
        service = container.undo_service
        if service is None:
            raise NotFoundError("Undo service is not available.")
        try:
            entry, performed = service.undo(entry_id)
        except UndoUnavailableError as error:
            raise ValidationError(error.message) from error
        if performed:
            container.hub_event_repository.append(
                "effect.undone",
                conversation_id=entry.conversation_id,
                data={
                    "entryId": entry.id,
                    "conversationId": entry.conversation_id,
                    "kind": entry.kind,
                    "target": entry.target,
                    "description": entry.description,
                },
            )
        return {
            "entry": undo_entry_json(entry),
            "performed": performed,
            "alreadyUndone": not performed,
        }



    @app.post("/knowledge-proposals/{proposal_id}/resolve")
    async def resolve_knowledge_proposal(
        proposal_id: str, body: ResolveKnowledgeProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.knowledge_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="knowledge", proposal=proposal, decision="reject"
            )
            return {"proposal": knowledge_proposal_json(proposal)}
        workspace_override = container.knowledge_proposal_repository._UNSET_WORKSPACE
        if body.workspaceId is not None:
            workspace_override = resolve_workspace_reference(container, body.workspaceId)
        proposal, source = container.knowledge_proposal_repository.accept_proposal(
            proposal_id, workspace_override=workspace_override
        )
        hub_append_proposal_resolved(
            container, kind="knowledge", proposal=proposal, decision="accept"
        )
        if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
            emit_knowledge_duplicates(container, source)
        return {
            "proposal": knowledge_proposal_json(proposal),
            "source": knowledge_source_json(source),
        }

    # tasks 域路由搬到 api/routes/tasks.py（搬家不改行为）
    register_tasks_routes(app, container)








    # notifications 域路由搬到 api/routes/notifications.py（搬家不改行为）
    register_notifications_routes(app, container)




    # proposals 域路由搬到 api/routes/proposals.py（搬家不改行为）
    register_proposals_routes(app, container)


    # reminders 域路由搬到 api/routes/reminders.py（搬家不改行为）
    register_reminders_routes(app, container)




    @app.post("/task-proposals/{proposal_id}/resolve")
    async def resolve_task_proposal(
        proposal_id: str, body: ResolveTaskProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.task_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="task", proposal=proposal, decision="reject"
            )
            return {"proposal": task_proposal_json(proposal)}
        peek = container.task_proposal_repository.get_proposal(proposal_id)
        if isinstance(peek.schedule, ReminderDue):
            proposal, reminder = (
                container.task_proposal_repository.accept_proposal_as_reminder(
                    proposal_id
                )
            )
            hub_append_proposal_resolved(
                container, kind="task", proposal=proposal, decision="accept"
            )
            return {
                "proposal": task_proposal_json(proposal),
                "reminder": reminder_json(reminder),
            }
        proposal, task = container.task_proposal_repository.accept_proposal(
            proposal_id
        )
        hub_append_proposal_resolved(
            container, kind="task", proposal=proposal, decision="accept"
        )
        return {
            "proposal": task_proposal_json(proposal),
            "task": task_json(task),
        }

    # artifacts 域路由搬到 api/routes/artifacts.py（搬家不改行为）
    register_artifacts_routes(app, container)









    @app.get("/api/v2/conversations/{conversation_id}/lanes")
    async def list_runtime_v2_lanes(
        conversation_id: str,
        includeArchived: bool = False,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
                runtime_v2_lane_json(lane, active_lane_id=main_lane_id)
                for lane in lanes
            ),
        }

    @app.post("/api/v2/conversations/{conversation_id}/lanes", status_code=201)
    async def create_runtime_v2_lane(
        conversation_id: str,
        body: RuntimeV2CreateLaneBody,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
            "lane": runtime_v2_lane_json(result.lane),
            "sourceLane": runtime_v2_lane_json(result.source_lane),
            "baseEntryId": result.base_entry_id,
            "eventsUrl": f"/api/v2/conversations/{conversation_id}/events",
        }

    # v2 lane 路由搬到 api/routes/v2_lanes.py（搬家不改行为）
    register_v2_lanes_routes(app, container)





    @app.post(
        "/api/v2/conversations/{conversation_id}/temporary-conversations",
        status_code=201,
    )
    async def create_runtime_v2_temporary_conversation(
        conversation_id: str,
        body: RuntimeV2CreateTemporaryConversationBody,
    ) -> dict[str, object]:
        source_conversation_id = resolve_runtime_v2_conversation(
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
            "lane": runtime_v2_lane_json(lane, active_lane_id=lane.id),
        }



    # v2_runs 域路由搬到 api/routes/v2_runs.py（搬家不改行为）
    register_v2_runs_routes(app, container)








    @app.get("/api/v2/conversations/{conversation_id}/memories")
    async def list_runtime_v2_memories(
        conversation_id: str,
        lane_id: str = Query(...),
        run_id: Optional[str] = Query(None),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
            "items": tuple(runtime_v2_memory_json(memory) for memory in memories),
        }

    @app.post("/api/v2/conversations/{conversation_id}/memories", status_code=201)
    async def create_runtime_v2_memory(
        conversation_id: str,
        body: RuntimeV2MemoryBody,
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
        return {"memory": runtime_v2_memory_json(memory)}


    # v2 记忆路由搬到 api/routes/v2_memory.py（搬家不改行为）
    register_v2_memory_routes(app, container)


    @app.get("/api/v2/conversations/{conversation_id}/memory-promotions")
    async def list_runtime_v2_memory_promotions(
        conversation_id: str,
        include_resolved: bool = Query(False),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
                runtime_v2_memory_promotion_json(promotion)
                for promotion in promotions
            ),
        }


    @app.post("/api/v2/conversations/{conversation_id}/messages", status_code=202)
    async def create_runtime_v2_message(
        conversation_id: str,
        body: RuntimeV2MessageBody,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
        _skill_messages, skill_notices = resolve_skill_requests(
            container.skill_service,
            container.workspace_resolver,
            target_conversation_id,
            body.content,
            include_bodies=False,
        )
        title_conversation_from_first_message(
            container, target_conversation_id, body.content
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
            # S1：显式技能调用结果（ok / disabled / not_user_invocable / …）
            "requestedSkills": skill_notices,
        }


    @app.get("/api/v2/conversations/{conversation_id}/snapshot")
    async def get_runtime_v2_snapshot(
        conversation_id: str,
        lane_id: Optional[str] = Query(default=None),
    ) -> dict[str, object]:
        target_conversation_id = resolve_runtime_v2_conversation(
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
        target_conversation_id = resolve_runtime_v2_conversation(
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


    @app.get("/api/v2/conversations/{conversation_id}/events")
    async def get_runtime_v2_events(
        conversation_id: str,
        after_seq: int = Query(0, ge=0),
        lane_id: Optional[str] = Query(default=None),
    ) -> StreamingResponse:
        target_conversation_id = resolve_runtime_v2_conversation(
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
                    yield runtime_v2_product_sse(event)
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
                        yield runtime_v2_product_sse(event)
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





    @app.post("/artifact-proposals/{proposal_id}/resolve")
    async def resolve_artifact_proposal(
        proposal_id: str, body: ResolveArtifactProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.artifact_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="artifact", proposal=proposal, decision="reject"
            )
            return {"proposal": artifact_proposal_json(proposal)}
        proposal, artifact = container.artifact_proposal_repository.accept_proposal(
            proposal_id
        )
        hub_append_proposal_resolved(
            container, kind="artifact", proposal=proposal, decision="accept"
        )
        return {
            "proposal": artifact_proposal_json(proposal),
            "artifact": artifact_json(artifact),
        }


    return app
