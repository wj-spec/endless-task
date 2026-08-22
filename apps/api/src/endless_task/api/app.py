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

from endless_task.domain.models import ConversationStatus, TurnStatus
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
from endless_task.security import configure_safe_logging
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteContextRepository,
    SqliteTextFileRepository,
    SqliteRuntimeRepository,
)
from endless_task.tooling import ApprovalStatus, ToolRegistry

from .serialization import (
    approval_request_json,
    compact_turn_snapshot_json,
    conversation_json,
    conversation_snapshot_json,
    response_variant_by_id,
    runtime_event_json,
    turn_command_json,
    uploaded_text_file_json,
)


logger = logging.getLogger(__name__)
TERMINAL_TURN_STATUSES = {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.CANCELLED}
CONFIG_VERSION = 1


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
    max_agent_iterations: int = 4
    max_tool_calls_per_turn: int = 8
    agent_timeout_seconds: float = 120.0
    approval_timeout_seconds: float = 1800.0
    max_message_characters: int = 100_000
    max_file_bytes: int = 1_000_000
    max_files_per_conversation: int = 10
    heartbeat_seconds: float = 15.0

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
        configured = os.environ.get("ENDLESS_TASK_DB_PATH")
        provider_name = os.environ.get("ENDLESS_TASK_PROVIDER", "fake").strip().lower()
        defaults = {
            "fake": ("fake-model", None),
            "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
            "openai": ("gpt-4.1-mini", "https://api.openai.com/v1"),
            "openai-compatible": ("", None),
        }
        default_model, default_base_url = defaults.get(provider_name, ("", None))
        api_key = os.environ.get("ENDLESS_TASK_API_KEY")
        if api_key is None and provider_name == "deepseek":
            api_key = os.environ.get("DEEPSEEK_API_KEY")
        if api_key is None and provider_name == "openai":
            api_key = os.environ.get("OPENAI_API_KEY")
        database_path = (
            Path(configured).expanduser().resolve()
            if configured
            else default_data_directory() / "endless-task.db"
        )
        return cls(
            database_path=database_path,
            config_version=int(os.environ.get("ENDLESS_TASK_CONFIG_VERSION", "1")),
            provider_name=provider_name,
            model=os.environ.get("ENDLESS_TASK_MODEL", default_model),
            base_url=os.environ.get("ENDLESS_TASK_BASE_URL", default_base_url),
            api_key=api_key,
            provider_timeout_seconds=float(
                os.environ.get("ENDLESS_TASK_PROVIDER_TIMEOUT_SECONDS", "60")
            ),
            system_prompt=os.environ.get(
                "ENDLESS_TASK_SYSTEM_PROMPT",
                "你是 Endless Task，一个可靠、简洁的个人助手。\n- 能直接回答的问题用自然语言直接回答，不要调用工具。\n- 信息不足时先向用户追问关键信息，不要猜测。\n- 只有问题确实需要会话附件内容时才调用文件工具。\n- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。",
            ),
            system_prompt_version=os.environ.get(
                "ENDLESS_TASK_SYSTEM_PROMPT_VERSION",
                "p1-v1",
            ),
            context_window_tokens=int(
                os.environ.get("ENDLESS_TASK_CONTEXT_WINDOW_TOKENS", "32768")
            ),
            max_output_tokens=int(
                os.environ.get("ENDLESS_TASK_MAX_OUTPUT_TOKENS", "2048")
            ),
            summary_token_limit=int(
                os.environ.get("ENDLESS_TASK_SUMMARY_TOKEN_LIMIT", "1024")
            ),
            max_concurrent_model_calls=int(
                os.environ.get("ENDLESS_TASK_MAX_CONCURRENT_MODEL_CALLS", "2")
            ),
            max_agent_iterations=int(
                os.environ.get("ENDLESS_TASK_MAX_AGENT_ITERATIONS", "4")
            ),
            max_tool_calls_per_turn=int(
                os.environ.get("ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN", "8")
            ),
            agent_timeout_seconds=float(
                os.environ.get("ENDLESS_TASK_AGENT_TIMEOUT_SECONDS", "120")
            ),
            approval_timeout_seconds=float(
                os.environ.get("ENDLESS_TASK_APPROVAL_TIMEOUT_SECONDS", "1800")
            ),
            max_message_characters=int(
                os.environ.get("ENDLESS_TASK_MAX_MESSAGE_CHARACTERS", "100000")
            ),
            max_file_bytes=int(
                os.environ.get("ENDLESS_TASK_MAX_FILE_BYTES", "1000000")
            ),
            max_files_per_conversation=int(
                os.environ.get("ENDLESS_TASK_MAX_FILES_PER_CONVERSATION", "10")
            ),
        )


@dataclass(frozen=True)
class AppContainer:
    settings: AppSettings
    database: Database
    chat_repository: SqliteChatRepository
    runtime_repository: SqliteRuntimeRepository
    file_repository: SqliteTextFileRepository
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


class ResolveApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]


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
    broker = RuntimeEventBroker()
    selected_provider = provider or _provider_from_settings(settings)
    selected_tool_registry = tool_registry or ToolRegistry()
    if tool_registry is None:
        selected_tool_registry.register(ReadTextFileTool(file_repository))
    runtime = AssistantRuntime(
        chat_repository=chat_repository,
        runtime_repository=runtime_repository,
        context_builder=P0ContextBuilder(
            chat_repository,
            system_prompt=settings.system_prompt,
            system_prompt_version=settings.system_prompt_version,
            max_context_tokens=settings.context_window_tokens,
            summary_token_limit=settings.summary_token_limit,
            context_repository=context_repository,
            file_repository=file_repository,
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
    )
    controller = TurnController(chat_repository=chat_repository, runtime=runtime)
    return AppContainer(
        settings=settings,
        database=database,
        chat_repository=chat_repository,
        runtime_repository=runtime_repository,
        file_repository=file_repository,
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
        container.chat_repository.delete_conversation(conversation_id)
        return Response(status_code=204)

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
