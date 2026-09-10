"""应用容器：`create_app` 装配出来的依赖集合（从 app.py 搬出）。

路由模块只依赖它（以及 `errors`），因此不再需要闭包共享 8 000 行里的局部变量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from endless_task.artifacts import ArtifactProposalService, SourceReferenceResolver
from endless_task.delegation import CoordinatorDelegationHandler
from endless_task.knowledge import (
    EmbeddingIndexer,
    KnowledgeLifecycleService,
    KnowledgeProposalService,
)
from endless_task.mcp_runtime import McpManager
from endless_task.memory import (
    MemoryConflictService,
    MemoryConsolidationService,
    MemoryForgettingService,
    MemoryProposalService,
    MemoryReflectionService,
    UserProfileService,
)
from endless_task.proposals.budget import ProposalBudget
from endless_task.runtime import RuntimeEventBroker
from endless_task.runtime.provider import ModelProvider
from endless_task.runtime.provider_manager import ProviderManager
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.runtime_v2 import RuntimeV2SessionGateway
from endless_task.runtime_v2.memory_quality import RuntimeV2MemoryQualityService
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.runtime_v2.run_trajectory import RunTrajectoryExporter
from endless_task.skills import SkillService
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
    SqliteChatRepository,
    SqliteHubEventRepository,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
    SqliteNotificationRepository,
    SqlitePreferencesRepository,
    SqliteReminderRepository,
    SqliteResponseFeedbackRepository,
    SqliteRetrievalEventRepository,
    SqliteRuntimeV2MemoryRepository,
    SqliteRuntimeV2Repository,
    SqliteTaskProposalRepository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
    SqliteTextFileRepository,
    SqliteUndoJournalRepository,
    SqliteWorkspaceRepository,
)
from endless_task.storage.provider_secret_store import ProviderSecretStore
from endless_task.storage.sqlite_mcp_server_repository import (
    SqliteMcpServerRepository,
)
from endless_task.storage.sqlite_provider_profile_repository import (
    SqliteProviderProfileRepository,
)
from endless_task.storage.sqlite_skill_provenance_repository import (
    SqliteSkillProvenanceRepository,
)
from endless_task.storage.sqlite_skill_usage_repository import (
    SqliteSkillUsageRepository,
)
from endless_task.tasks import (
    TaskNotificationService,
    TaskProposalService,
    TaskScheduler,
    TaskWorker,
)
from endless_task.tooling import ToolRegistry
from endless_task.workspace_runtime import (
    EffectLog,
    WorkspaceResolver,
    WorkspaceUndoService,
)
from endless_task.workspace_runtime.artifact_store import ArtifactFileStore

if TYPE_CHECKING:  # 仅供注解使用：AppSettings 在 api/app_settings.py（app.py 仍 re-export）
    from .app_settings import AppSettings


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
    hub_event_repository: SqliteHubEventRepository
    reminder_repository: SqliteReminderRepository
    knowledge_proposal_repository: SqliteKnowledgeProposalRepository
    knowledge_proposal_service: Optional[KnowledgeProposalService]
    knowledge_repository: SqliteKnowledgeRepository
    retrieval_event_repository: SqliteRetrievalEventRepository
    workspace_repository: SqliteWorkspaceRepository
    workspace_resolver: WorkspaceResolver
    skill_service: SkillService
    #: S2：技能使用统计（按 digest 分代持久化）。
    skill_usage_repository: SqliteSkillUsageRepository
    #: S8：生态安装来源记录。
    skill_provenance_repository: SqliteSkillProvenanceRepository
    provider_profile_repository: SqliteProviderProfileRepository
    provider_secret_store: ProviderSecretStore
    provider_manager: ProviderManager
    mcp_server_repository: SqliteMcpServerRepository
    mcp_manager: McpManager
    effect_log: EffectLog
    artifact_file_store: ArtifactFileStore
    knowledge_lifecycle_service: Optional[KnowledgeLifecycleService]
    memory_forgetting_service: Optional[MemoryForgettingService]
    memory_consolidation_service: Optional[MemoryConsolidationService]
    undo_service: Optional[WorkspaceUndoService]
    memory_reflection_service: Optional[MemoryReflectionService]
    user_profile_service: Optional[UserProfileService]
    undo_journal_repository: SqliteUndoJournalRepository
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
    response_feedback_repository: Optional[SqliteResponseFeedbackRepository] = None
    # S4 内嵌终端：按工作区管理 PTY 会话（默认开启；上限/回收见 terminal.py）。
    terminal_service: Optional["TerminalService"] = None
