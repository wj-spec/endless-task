from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from endless_task.domain.models import (
    ConversationStatus,
    PermissionMode,
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
    OpenAICompatibleProvider,
    P0ContextBuilder,
    RuntimeConfiguration,
    RuntimeEvent,
    RuntimeEventBroker,
    TurnController,
    UnconfiguredProvider,
)
from endless_task.runtime.provider import ModelProvider
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
from endless_task.security import configure_safe_logging
from endless_task.memory import MemoryConflictService, MemoryProposalService
from endless_task.tasks import TaskProposalService, TaskWorker
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
    SqliteChatRepository,
    SqliteContextRepository,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
    SqlitePreferencesRepository,
    SqliteTextFileRepository,
    SqliteRuntimeRepository,
    SqliteTaskRepository,
    SqliteTaskProposalRepository,
    SqliteTaskRunRepository,
)
from endless_task.tooling import ApprovalStatus, ToolRegistry

from .serialization import (
    artifact_json,
    artifact_proposal_json,
    artifact_version_json,
    memory_proposal_json,
    memory_record_json,
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

TASK_AWARENESS_PROMPT_VERSION = "p4.2-v1"
TASK_AWARENESS_CLAUSE = (
    "\n- 用户要求周期性做某事时（每天、每周、每月等），回答必须明确复述完整承诺"
    "（周期、时间、做什么），并说明该安排在用户确认后才生效；不得声称已经安排。"
    "\n- 用户提出一次性定时事项时，如实说明一次性定时安排能力尚未就绪，"
    "可以建议届时再提出，或把事项内容记入记忆；不得假装已安排。"
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
    artifact_proposals_enabled: bool = True
    task_proposals_enabled: bool = True

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
        if self.max_agent_iterations <= 0 or self.max_tool_calls_per_turn <= 0:
            raise ValueError("Agent loop limits must be positive")
        if self.agent_timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        if self.approval_timeout_seconds <= 0:
            raise ValueError("Approval timeout must be positive")
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
            artifact_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_ARTIFACT_PROPOSALS", "1")
            ),
            task_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_TASK_PROPOSALS", "1")
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
    task_worker: TaskWorker
    reference_resolver: SourceReferenceResolver
    memory_proposal_service: Optional[MemoryProposalService]
    memory_conflict_service: Optional[MemoryConflictService]
    broker: RuntimeEventBroker
    provider: ModelProvider
    runtime: AssistantRuntime
    tool_registry: ToolRegistry
    controller: TurnController


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    status: Optional[ConversationStatus] = None


class CreateTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class UpdateMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str


class ResolveMemoryProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class RollbackArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetOrdinal: int
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
    artifact_proposal_repository = SqliteArtifactProposalRepository(database)
    task_repository = SqliteTaskRepository(database)
    task_proposal_repository = SqliteTaskProposalRepository(database)
    task_run_repository = SqliteTaskRunRepository(database)
    reference_resolver = SourceReferenceResolver(
        file_repository=file_repository,
        memory_repository=memory_repository,
    )
    memory_proposal_service: Optional[MemoryProposalService] = None
    broker = RuntimeEventBroker()
    selected_provider = provider or _provider_from_settings(settings)
    selected_tool_registry = tool_registry or ToolRegistry()
    if tool_registry is None:
        selected_tool_registry.register(ReadTextFileTool(file_repository))
        selected_tool_registry.register(ReadArtifactTool(artifact_repository))
    system_prompt = settings.system_prompt
    system_prompt_version = settings.system_prompt_version
    if settings.artifact_proposals_enabled:
        system_prompt = system_prompt + ARTIFACT_AWARENESS_CLAUSE
        system_prompt_version = ARTIFACT_AWARENESS_PROMPT_VERSION
    if settings.task_proposals_enabled:
        system_prompt = system_prompt + TASK_AWARENESS_CLAUSE
        system_prompt_version = TASK_AWARENESS_PROMPT_VERSION
    runtime = AssistantRuntime(
        chat_repository=chat_repository,
        runtime_repository=runtime_repository,
        context_builder=P0ContextBuilder(
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
        ),
        provider=selected_provider,
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
        )

    if (
        settings.memory_proposals_enabled
        or settings.artifact_proposals_enabled
        or settings.task_proposals_enabled
    ):

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
            if memory_proposal_service is not None:
                await memory_proposal_service.generate_for_turn(
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
    task_worker = TaskWorker(
        task_repository=task_repository,
        run_repository=task_run_repository,
        controller=controller,
        max_concurrent_task_runs=settings.max_concurrent_task_runs,
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
        task_worker=task_worker,
        artifact_proposal_service=artifact_proposal_service,
        reference_resolver=reference_resolver,
        memory_proposal_service=memory_proposal_service,
        memory_conflict_service=memory_conflict_service,
        broker=broker,
        provider=selected_provider,
        runtime=runtime,
        tool_registry=selected_tool_registry,
        controller=controller,
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
        await container.runtime.recover_interrupted()
        try:
            yield
        finally:
            await container.task_worker.drain()
            await container.controller.shutdown()
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
        return {
            "status": "ok",
            "provider": container.provider.name,
            "model": container.settings.model,
            "providerConfigured": not isinstance(
                container.provider,
                UnconfiguredProvider,
            ),
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
    async def create_conversation() -> dict[str, object]:
        return conversation_json(
            container.chat_repository.create_or_reuse_empty_conversation()
        )

    @app.get("/conversations")
    async def list_conversations(
        status: ConversationStatus = Query(ConversationStatus.ACTIVE),
        query: Optional[str] = Query(None),
    ) -> dict[str, object]:
        conversations = container.chat_repository.list_conversations(
            status=status,
            title_query=query,
        )
        return {"items": [conversation_json(item) for item in conversations]}

    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_conversation_snapshot(conversation_id)
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
        if body.title is None and body.status is None:
            raise ApiRequestError("invalid_request", "至少需要提供 title 或 status。")
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
        return conversation_json(conversation)

    @app.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str) -> Response:
        container.task_repository.cancel_tasks_for_conversation(
            conversation_id
        )
        container.task_proposal_repository.cancel_proposals_for_conversation(
            conversation_id
        )
        container.proposal_repository.cancel_proposals_for_conversation(
            conversation_id
        )
        container.chat_repository.delete_conversation(conversation_id)
        return Response(status_code=204)

    @app.get("/memories")
    async def list_memories(include_deleted: bool = False) -> dict[str, object]:
        memories = container.memory_repository.list_memories(
            include_deleted=include_deleted
        )
        return {"items": [memory_record_json(item) for item in memories]}

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

    @app.get("/tasks/{task_id}/runs")
    async def list_task_runs(task_id: str) -> dict[str, object]:
        runs = container.task_run_repository.list_runs(task_id=task_id)
        return {"items": [task_run_json(item) for item in runs]}

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
        await container.controller.cancel(turn_id=turn_id)
        snapshot = container.chat_repository.get_turn(turn_id)
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
        snapshot = container.chat_repository.select_response_variant(
            turn_id=turn_id,
            variant_id=variant_id,
        )
        return turn_command_json(snapshot)

    return app
