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
    MemoryKind,
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
from endless_task.knowledge.embeddings import Embedder
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
from .app_settings import AppSettings
from .wiring import _build_container, _provider_from_settings
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
from .routes.proposals_resolve import register_proposals_resolve_routes
from .schemas.proposals_resolve import (
    ResolveArtifactProposalBody,
    ResolveKnowledgeProposalBody,
    ResolveMemoryProposalBody,
    ResolveTaskProposalBody,
    ResponseFeedbackBody,
    SearchBody,
)
from .routes.v2_conversations import register_v2_conversations_routes
from .schemas.v2_conversations import (
    RuntimeV2CreateLaneBody,
    RuntimeV2CreateTemporaryConversationBody,
    RuntimeV2MemoryBody,
    RuntimeV2MessageBody,
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


    # proposals_resolve 域路由搬到 api/routes/proposals_resolve.py（搬家不改行为）
    register_proposals_resolve_routes(app, container)








    # userprofile 域路由搬到 api/routes/userprofile.py（搬家不改行为）
    register_userprofile_routes(app, container)











    # tasks 域路由搬到 api/routes/tasks.py（搬家不改行为）
    register_tasks_routes(app, container)








    # notifications 域路由搬到 api/routes/notifications.py（搬家不改行为）
    register_notifications_routes(app, container)




    # proposals 域路由搬到 api/routes/proposals.py（搬家不改行为）
    register_proposals_routes(app, container)


    # reminders 域路由搬到 api/routes/reminders.py（搬家不改行为）
    register_reminders_routes(app, container)





    # artifacts 域路由搬到 api/routes/artifacts.py（搬家不改行为）
    register_artifacts_routes(app, container)









    # v2_conversations 域路由搬到 api/routes/v2_conversations.py（搬家不改行为）
    register_v2_conversations_routes(app, container)



    # v2 lane 路由搬到 api/routes/v2_lanes.py（搬家不改行为）
    register_v2_lanes_routes(app, container)








    # v2_runs 域路由搬到 api/routes/v2_runs.py（搬家不改行为）
    register_v2_runs_routes(app, container)











    # v2 记忆路由搬到 api/routes/v2_memory.py（搬家不改行为）
    register_v2_memory_routes(app, container)
















    return app
