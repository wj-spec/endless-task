from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol, Sequence, TYPE_CHECKING

from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from endless_task.tooling import (
    RegisteredTool,
    ToolCall,
    ToolCallStatus,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
)
from endless_task.tooling.schema import ToolSchemaError, validate_tool_arguments

from .context import ContextProjection
from .domain import (
    ModelTurnRecord,
    ModelTurnStatus,
    RunRecord,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from .safety import (
    SafetyStopError,
    SafetyStopPolicy,
    SafetyStopReason,
    SafetyStopState,
)

if TYPE_CHECKING:
    from endless_task.storage.sqlite_runtime_v2_repository import (
        SqliteRuntimeV2Repository,
    )


logger = logging.getLogger(__name__)

_MODEL_TURN_WAITING_SLOT_STATUS = ModelTurnStatus.WAITING_PROVIDER_SLOT
_MODEL_TURN_STREAMING_STATUS = ModelTurnStatus.STREAMING
_MODEL_TURN_EXECUTING_STATUS = ModelTurnStatus.EXECUTING_TOOLS
_MODEL_TURN_COMPLETED_STATUS = ModelTurnStatus.COMPLETED
_MODEL_TURN_FAILED_STATUS = ModelTurnStatus.FAILED
_MODEL_TURN_CANCELLED_STATUS = ModelTurnStatus.CANCELLED
_TERMINAL_TOOL_STATUSES = frozenset(
    {
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.CANCELLED,
        ToolExecutionStatus.REJECTED,
        ToolExecutionStatus.EXPIRED,
    }
)


class ToolApprovalDecision(str, Enum):
    WAIT = "wait"
    APPROVE = "approve"
    DENY = "deny"


class ToolApprovalGate(Protocol):
    async def decide(
        self,
        execution: ToolExecutionRecord,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolApprovalDecision:
        ...


@dataclass(frozen=True)
class ContextCompactionResult:
    messages: tuple[ProviderMessage, ...]
    summary_entry_id: Optional[str] = None
    covered_entry_ids: tuple[str, ...] = ()


class ContextCompactionHook(Protocol):
    async def compact(
        self,
        run: RunRecord,
        messages: Sequence[ProviderMessage],
    ) -> Optional[ContextCompactionResult]:
        ...


class WaitingToolApprovalGate:
    async def decide(
        self,
        execution,
        tool,
        call,
        cancellation_token,
    ) -> ToolApprovalDecision:
        del execution, tool, call, cancellation_token
        return ToolApprovalDecision.WAIT


class StaticToolApprovalGate:
    def __init__(self, decision: ToolApprovalDecision) -> None:
        self._decision = decision

    async def decide(
        self,
        execution,
        tool,
        call,
        cancellation_token,
    ) -> ToolApprovalDecision:
        del execution, tool, call, cancellation_token
        return self._decision


@dataclass(frozen=True)
class ToolExecutionOutcome:
    messages: tuple[ProviderMessage, ...]
    records: tuple[ToolExecutionRecord, ...] = ()
    pending_approval_execution_ids: tuple[str, ...] = ()
    terminal_execution_ids: tuple[str, ...] = ()


@dataclass
class _ToolWorkItem:
    provider_call: ProviderToolCall
    record_id: str
    status: ToolExecutionStatus = ToolExecutionStatus.CREATED
    content: str = ""
    error_code: Optional[str] = None
    safe_message: Optional[str] = None
    succeeded: bool = False
    pending: bool = False


@dataclass(frozen=True)
class _PreparedToolExecution:
    item: _ToolWorkItem
    tool: RegisteredTool
    call: ToolCall
    needs_approval: bool


class ToolExecutionCoordinator:
    """Executes one model-turn tool batch while preserving source order."""

    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        tool_registry: ToolRegistry,
        approval_gate: Optional[ToolApprovalGate] = None,
    ) -> None:
        self._repository = repository
        self._tool_registry = tool_registry
        self._approval_gate = approval_gate or WaitingToolApprovalGate()

    def definitions(self) -> tuple[ProviderToolDefinition, ...]:
        return tuple(
            ProviderToolDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )
            for definition in self._tool_registry.definitions()
        )

    async def execute(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        provider_calls: Sequence[ProviderToolCall],
        assistant_content: str,
        cancellation_token: CancellationToken,
        safety_policy: SafetyStopPolicy,
        safety_state: SafetyStopState,
    ) -> ToolExecutionOutcome:
        items: list[_ToolWorkItem] = []
        for provider_call in provider_calls:
            record, _entry = self._repository.record_tool_call(
                model_turn_id=model_turn.id,
                call_id=provider_call.id,
                tool_name=provider_call.name,
                arguments=provider_call.arguments,
            )
            safety_policy.register_tool_signature(
                safety_state,
                tool_name=provider_call.name,
                arguments=provider_call.arguments,
            )
            items.append(
                _ToolWorkItem(
                    provider_call=provider_call,
                    record_id=record.id,
                )
            )

        prepared_items: list[_PreparedToolExecution] = []
        for item in items:
            self._repository.transition_tool_execution_status(
                item.record_id,
                ToolExecutionStatus.VALIDATING,
                event_type="tool_execution_status_changed",
                payload={
                    "toolExecutionId": item.record_id,
                    "status": ToolExecutionStatus.VALIDATING.value,
                },
            )
            cancellation_token.raise_if_cancelled()
            try:
                tool = self._resolve_tool(item.provider_call.name)
                call = self._tool_call(item, run, model_turn)
                self._validate_arguments(tool, item.provider_call.arguments)
                needs_approval = (
                    tool.definition.approval_mode.value == "required"
                    or tool.requires_explicit_confirmation(call)
                )
            except ToolValidationError as error:
                item.status = ToolExecutionStatus.FAILED
                item.error_code = error.code
                item.safe_message = "模型请求了当前不可用的工具。"
                item.content = (
                    f"工具调用无效（{error.code}）：{item.safe_message} "
                    "不要原样重复同一调用；可以调整参数后重试。"
                )
                continue
            prepared_items.append(
                _PreparedToolExecution(
                    item=item,
                    tool=tool,
                    call=call,
                    needs_approval=needs_approval,
                )
            )

        work_tasks = [
            asyncio.create_task(
                self._execute_prepared_item(
                    run=run,
                    model_turn=model_turn,
                    prepared=prepared_item,
                    cancellation_token=cancellation_token,
                )
            )
            for prepared_item in prepared_items
        ]
        try:
            await asyncio.gather(*work_tasks)
        except RuntimeCancelled:
            for task in work_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*work_tasks, return_exceptions=True)
            for item in items:
                if item.pending:
                    continue
                if item.status not in _TERMINAL_TOOL_STATUSES:
                    item.status = ToolExecutionStatus.CANCELLED
                    item.error_code = "cancelled"
                    item.safe_message = "工具执行已被取消。"
                    item.content = "工具执行已被用户取消。"
                self._repository.record_tool_result(
                    item.record_id,
                    status=item.status,
                    content=item.content,
                    event_type=self._result_event(item.status),
                    error_code=item.error_code,
                    safe_message=item.safe_message,
                )
            raise
        except BaseException:
            for task in work_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*work_tasks, return_exceptions=True)
            raise

        terminal_ids: list[str] = []
        pending_ids: list[str] = []
        result_messages: list[ProviderMessage] = [] if not provider_calls else [
            ProviderMessage(
                role="assistant",
                content=assistant_content,
                tool_calls=tuple(provider_calls),
            )
        ]

        for item in items:
            if item.pending:
                pending_ids.append(item.record_id)
                continue
            event_type = self._result_event(item.status)
            record, _result_entry = self._repository.record_tool_result(
                item.record_id,
                status=item.status,
                content=item.content,
                event_type=event_type,
                error_code=item.error_code,
                safe_message=item.safe_message,
            )
            terminal_ids.append(record.id)
            safety_policy.register_tool_result(
                safety_state,
                succeeded=item.succeeded,
            )
            result_messages.append(
                ProviderMessage(
                    role="tool",
                    content=item.content,
                    tool_call_id=item.provider_call.id,
                    name=item.provider_call.name,
                )
            )

        return ToolExecutionOutcome(
            messages=tuple(result_messages),
            records=tuple(
                self._repository.get_tool_execution(item.record_id)
                for item in items
            ),
            pending_approval_execution_ids=tuple(pending_ids),
            terminal_execution_ids=tuple(terminal_ids),
        )

    async def _execute_prepared_item(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        prepared: _PreparedToolExecution,
        cancellation_token: CancellationToken,
    ) -> None:
        del run, model_turn
        item = prepared.item
        tool = prepared.tool
        call = prepared.call
        if prepared.needs_approval:
            decision = await self._approval_gate.decide(
                self._repository.get_tool_execution(item.record_id),
                tool,
                call,
                cancellation_token,
            )
            if decision is ToolApprovalDecision.WAIT:
                self._repository.transition_tool_execution_status(
                    item.record_id,
                    ToolExecutionStatus.WAITING_APPROVAL,
                    event_type="tool_execution_status_changed",
                    payload={
                        "toolExecutionId": item.record_id,
                        "status": ToolExecutionStatus.WAITING_APPROVAL.value,
                    },
                )
                item.status = ToolExecutionStatus.WAITING_APPROVAL
                item.pending = True
                return
            if decision is ToolApprovalDecision.DENY:
                item.status = ToolExecutionStatus.REJECTED
                item.error_code = "approval_denied"
                item.safe_message = "用户未授权这项操作。"
                item.content = (
                    "用户未授权这项操作。不要重复请求相同操作；"
                    "请说明未执行，或在无需该操作的情况下继续。"
                )
                return

        self._repository.transition_tool_execution_status(
            item.record_id,
            ToolExecutionStatus.RUNNING,
            event_type="tool_execution_started",
            payload={"toolExecutionId": item.record_id},
        )
        item.status = ToolExecutionStatus.RUNNING
        try:
            result = await self._execute_tool(
                tool,
                call,
                cancellation_token,
                timeout_seconds=tool.definition.timeout_seconds,
            )
        except RuntimeCancelled:
            item.status = ToolExecutionStatus.CANCELLED
            item.error_code = "cancelled"
            item.safe_message = "工具执行已被取消。"
            item.content = "工具执行已被用户取消。"
            raise
        except ToolError as error:
            item.status = ToolExecutionStatus.FAILED
            item.error_code = error.code
            item.safe_message = error.safe_message
            item.content = (
                f"工具执行失败（{error.code}）：{error.safe_message} "
                "不要原样重复同一调用；可以调整参数后重试。"
            )
            return
        except Exception:
            logger.exception(
                "Tool execution raised an unexpected error",
                extra={
                    "tool_name": item.provider_call.name,
                    "tool_execution_id": item.record_id,
                },
            )
            item.status = ToolExecutionStatus.FAILED
            item.error_code = "tool_execution_failed"
            item.safe_message = "工具执行出现内部错误。"
            item.content = (
                "工具执行出现内部错误。不要原样重复同一调用；"
                "可以调整参数后重试。"
            )
            return

        bounded_result = self._bounded_result(
            result,
            max_characters=tool.definition.max_output_characters,
        )
        item.status = ToolExecutionStatus.COMPLETED
        item.content = bounded_result.content
        item.succeeded = True

    def _resolve_tool(self, name: str) -> RegisteredTool:
        return self._tool_registry.resolve(name)

    @staticmethod
    def _tool_call(
        item: _ToolWorkItem,
        run: RunRecord,
        model_turn: ModelTurnRecord,
    ) -> ToolCall:
        return ToolCall(
            id=item.provider_call.id,
            conversation_id=run.conversation_id,
            turn_id=model_turn.id,
            response_variant_id=model_turn.run_id,
            tool_name=item.provider_call.name,
            arguments=item.provider_call.arguments,
            status=ToolCallStatus.RUNNING,
            created_at=model_turn.created_at,
        )

    @staticmethod
    def _validate_arguments(tool: RegisteredTool, arguments) -> None:
        try:
            validate_tool_arguments(tool.definition.input_schema, arguments)
        except ToolSchemaError as error:
            raise ToolValidationError(
                "invalid_tool_arguments",
                "模型提供的工具参数不符合要求。",
            ) from error

    @staticmethod
    async def _execute_tool(
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        execution = asyncio.create_task(tool.execute(call, cancellation_token))
        cancellation = asyncio.create_task(cancellation_token.wait())
        try:
            done, _pending = await asyncio.wait(
                (execution, cancellation),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                raise RuntimeCancelled()
            if execution not in done:
                raise ToolError(
                    "tool_timeout",
                    "工具执行超时，可以重试。",
                    retryable=True,
                )
            result = execution.result()
            if not isinstance(result, ToolResult) or result.tool_call_id != call.id:
                raise ToolError(
                    "invalid_tool_result",
                    "工具返回了无效结果。",
                    retryable=False,
                )
            return result
        finally:
            for task in (execution, cancellation):
                if not task.done():
                    task.cancel()
            await asyncio.gather(execution, cancellation, return_exceptions=True)

    @staticmethod
    def _bounded_result(result: ToolResult, *, max_characters: int) -> ToolResult:
        if len(result.content) <= max_characters:
            return result
        return ToolResult(
            tool_call_id=result.tool_call_id,
            content=result.content[:max_characters],
            structured_content=result.structured_content,
            is_truncated=True,
        )

    @staticmethod
    def _result_event(status: ToolExecutionStatus) -> str:
        if status is ToolExecutionStatus.COMPLETED:
            return "tool_execution_completed"
        if status is ToolExecutionStatus.FAILED:
            return "tool_execution_failed"
        if status is ToolExecutionStatus.CANCELLED:
            return "tool_execution_cancelled"
        if status is ToolExecutionStatus.REJECTED:
            return "tool_execution_rejected"
        if status is ToolExecutionStatus.EXPIRED:
            return "tool_execution_expired"
        return "tool_execution_status_changed"


@dataclass(frozen=True)
class ModelTurnOutcome:
    model_turn_id: str
    content: str
    finish_reason: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    messages: tuple[ProviderMessage, ...]
    tool_calls: tuple[ProviderToolCall, ...]
    pending_approval_execution_ids: tuple[str, ...]


class ModelTurnRunner:
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_coordinator: ToolExecutionCoordinator,
        model: str,
        max_output_tokens: int,
        temperature: Optional[float] = None,
        provider_slot: Optional[asyncio.Semaphore] = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._tool_coordinator = tool_coordinator
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._provider_slot = provider_slot

    async def run(
        self,
        *,
        run: RunRecord,
        messages: Sequence[ProviderMessage],
        cancellation_token: CancellationToken,
        safety_policy: SafetyStopPolicy,
        safety_state: SafetyStopState,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> ModelTurnOutcome:
        model_turn = self._repository.start_model_turn(
            run.id,
            provider=self._provider.name,
            model=self._model,
        )
        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_WAITING_SLOT_STATUS,
            event_type="model_turn_status_changed",
            payload={"status": _MODEL_TURN_WAITING_SLOT_STATUS.value},
        )

        provider_calls: list[ProviderToolCall] = []
        completion: Optional[ProviderCompleted] = None
        content_parts: list[str] = []
        request_id = f"{run.id}:{model_turn.turn_index}"
        request = ProviderRequest(
            request_id=request_id,
            model=self._model,
            messages=tuple(messages),
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            tools=self._tool_coordinator.definitions(),
        )

        try:
            if self._provider_slot is not None:
                async with self._provider_slot:
                    completion = await self._consume_provider(
                        run=run,
                        model_turn=model_turn,
                        request=request,
                        cancellation_token=cancellation_token,
                        provider_calls=provider_calls,
                        content_parts=content_parts,
                        on_text_delta=on_text_delta,
                    )
            else:
                completion = await self._consume_provider(
                    run=run,
                    model_turn=model_turn,
                    request=request,
                    cancellation_token=cancellation_token,
                    provider_calls=provider_calls,
                    content_parts=content_parts,
                    on_text_delta=on_text_delta,
                )
        except RuntimeCancelled:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_CANCELLED_STATUS,
                event_type="model_turn_cancelled",
            )
            raise
        except BaseException as error:
            error_code = "provider_failed"
            safe_message = "模型执行失败。"
            if isinstance(error, ProviderError):
                error_code = error.code
                safe_message = error.safe_message
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code=error_code,
                safe_message=safe_message,
            )
            raise

        content = "".join(content_parts)

        if completion is None:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code="invalid_provider_response",
                safe_message="模型未返回完成事件。",
            )
            raise ProviderError(
                "invalid_provider_response",
                "模型未返回完成事件。",
                retryable=False,
            )
        if completion.finish_reason == "tool_calls" and not provider_calls:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_FAILED_STATUS,
                event_type="model_turn_failed",
                error_code="invalid_provider_response",
                safe_message="模型声明工具调用但未返回调用内容。",
            )
            raise ProviderError(
                "invalid_provider_response",
                "模型声明工具调用但未返回调用内容。",
                retryable=False,
            )

        if provider_calls:
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_EXECUTING_STATUS,
                event_type="model_turn_status_changed",
                payload={"status": _MODEL_TURN_EXECUTING_STATUS.value},
            )
            try:
                tool_outcome = await self._tool_coordinator.execute(
                    run=run,
                    model_turn=model_turn,
                    provider_calls=tuple(provider_calls),
                    assistant_content=content,
                    cancellation_token=cancellation_token,
                    safety_policy=safety_policy,
                    safety_state=safety_state,
                )
            except RuntimeCancelled:
                self._repository.transition_model_turn_status(
                    model_turn.id,
                    _MODEL_TURN_CANCELLED_STATUS,
                    event_type="model_turn_cancelled",
                )
                raise
            except BaseException:
                self._repository.transition_model_turn_status(
                    model_turn.id,
                    _MODEL_TURN_FAILED_STATUS,
                    event_type="model_turn_failed",
                    error_code="tool_execution_failed",
                    safe_message="工具执行未能继续。",
                )
                raise
            if tool_outcome.pending_approval_execution_ids:
                self._repository.transition_run_status(
                    run.id,
                    RunStatus.WAITING_APPROVAL,
                    event_type="run_status_changed",
                    payload={"status": RunStatus.WAITING_APPROVAL.value},
                )
                return ModelTurnOutcome(
                    model_turn_id=model_turn.id,
                    content=content,
                    finish_reason=completion.finish_reason,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    messages=(),
                    tool_calls=tuple(provider_calls),
                    pending_approval_execution_ids=(
                        tool_outcome.pending_approval_execution_ids
                    ),
                )
            self._repository.transition_model_turn_status(
                model_turn.id,
                _MODEL_TURN_COMPLETED_STATUS,
                event_type="model_turn_completed",
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            return ModelTurnOutcome(
                model_turn_id=model_turn.id,
                content=content,
                finish_reason=completion.finish_reason,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                messages=tool_outcome.messages,
                tool_calls=tuple(provider_calls),
                pending_approval_execution_ids=(),
            )

        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_COMPLETED_STATUS,
            event_type="model_turn_completed",
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
        )
        return ModelTurnOutcome(
            model_turn_id=model_turn.id,
            content=content,
            finish_reason=completion.finish_reason,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            messages=(),
            tool_calls=(),
            pending_approval_execution_ids=(),
        )

    async def _consume_provider(
        self,
        *,
        run: RunRecord,
        model_turn: ModelTurnRecord,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
        provider_calls: list[ProviderToolCall],
        content_parts: list[str],
        on_text_delta: Optional[Callable[[str], Awaitable[None]]],
    ) -> Optional[ProviderCompleted]:
        self._repository.transition_model_turn_status(
            model_turn.id,
            _MODEL_TURN_STREAMING_STATUS,
            event_type="model_turn_started",
        )
        async for provider_event in self._provider.stream(
            request,
            cancellation_token,
        ):
            cancellation_token.raise_if_cancelled()
            if isinstance(provider_event, ProviderTextDelta):
                if not provider_event.text:
                    continue
                content_parts.append(provider_event.text)
                self._repository.append_runtime_event(
                    run_id=run.id,
                    model_turn_id=model_turn.id,
                    event_type="model_text_delta",
                    payload={"delta": provider_event.text},
                )
                if on_text_delta is not None:
                    await on_text_delta(provider_event.text)
                continue
            if isinstance(provider_event, ProviderToolCall):
                provider_calls.append(provider_event)
                continue
            if isinstance(provider_event, ProviderCompleted):
                return provider_event
        return None


@dataclass(frozen=True)
class RunExecutionResult:
    status: RunStatus
    run: RunRecord
    content: str
    input_tokens: int
    output_tokens: int
    model_turn_ids: tuple[str, ...]
    pending_approval_execution_ids: tuple[str, ...]
    follow_up_messages: tuple[str, ...] = ()
    assistant_entry_id: Optional[str] = None


class AgentRunExecutor:
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        model: str,
        max_output_tokens: int = 8192,
        max_model_turns: int = 24,
        temperature: Optional[float] = None,
        approval_gate: Optional[ToolApprovalGate] = None,
        safety_policy: Optional[SafetyStopPolicy] = None,
        provider_slot: Optional[asyncio.Semaphore] = None,
        compaction_hook: Optional[ContextCompactionHook] = None,
        context_prefix_messages: Sequence[ProviderMessage] = (),
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._tool_registry = tool_registry
        self._model = model
        self._max_output_tokens = max_output_tokens
        if max_model_turns <= 0:
            raise ValueError("max_model_turns must be positive")
        self._max_model_turns = max_model_turns
        self._temperature = temperature
        self._safety_policy = safety_policy or SafetyStopPolicy()
        self._provider_slot = provider_slot
        self._compaction_hook = compaction_hook
        self._context_prefix_messages = tuple(context_prefix_messages)
        self._tool_coordinator = ToolExecutionCoordinator(
            repository=repository,
            tool_registry=tool_registry,
            approval_gate=approval_gate,
        )
        self._model_turn_runner = ModelTurnRunner(
            repository=repository,
            provider=provider,
            tool_coordinator=self._tool_coordinator,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            provider_slot=provider_slot,
        )
        self._context_projection = ContextProjection()
        self._steering_messages: list[str] = []
        self._follow_up_messages: list[str] = []
        self._queue_lock = asyncio.Lock()
        self._active_run_id: Optional[str] = None
        self._cancellation_token: Optional[CancellationToken] = None

    async def execute(
        self,
        run_id: str,
        *,
        cancellation_token: CancellationToken,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> RunExecutionResult:
        run = self._repository.get_run(run_id)
        self._active_run_id = run.id
        self._cancellation_token = cancellation_token
        entries = self._repository.list_entry_context_entries(run.trigger_entry_id)
        projection = self._context_projection.project(entries)
        provider_messages: list[ProviderMessage] = list(
            self._context_prefix_messages
        )
        provider_messages.extend(projection.messages)
        safety_state = SafetyStopState()
        run = self._repository.start_run(run.id)
        cancellation_token.raise_if_cancelled()

        content_parts: list[str] = []
        model_turn_ids: list[str] = []
        input_tokens = 0
        output_tokens = 0
        pending_approval_ids: tuple[str, ...] = ()

        try:
            while True:
                if len(model_turn_ids) >= self._max_model_turns:
                    raise SafetyStopError(
                        SafetyStopReason.MAX_MODEL_TURNS,
                        "Run 达到本地模型轮次上限，已安全停止。",
                    )
                provider_messages.extend(await self._drain_steering_messages(run))
                await self._maybe_compact_context(run, provider_messages)
                outcome = await self._model_turn_runner.run(
                    run=run,
                    messages=provider_messages,
                    cancellation_token=cancellation_token,
                    safety_policy=self._safety_policy,
                    safety_state=safety_state,
                    on_text_delta=on_text_delta,
                )
                model_turn_ids.append(outcome.model_turn_id)
                content_parts.append(outcome.content)
                input_tokens += outcome.input_tokens or 0
                output_tokens += outcome.output_tokens or 0
                pending_approval_ids = outcome.pending_approval_execution_ids
                if pending_approval_ids:
                    return self._result(
                        RunStatus.WAITING_APPROVAL,
                        run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        pending_approval_ids,
                    )

                provider_messages.extend(outcome.messages)
                self._safety_policy.register_model_turn(
                    safety_state,
                    content=outcome.content,
                    tool_call_count=len(outcome.tool_calls),
                )
                if not outcome.tool_calls and outcome.content.strip():
                    final_run, assistant_entry = self._repository.finalize_run(
                        run.id,
                        content="".join(content_parts),
                        finish_reason=outcome.finish_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return self._result(
                        RunStatus.COMPLETED,
                        final_run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        (),
                        assistant_entry_id=assistant_entry.id,
                    )
        except RuntimeCancelled:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.CANCELLED,
                event_type="run_cancelled",
                cancelled_by="user",
            )
            return self._result(
                RunStatus.CANCELLED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except SafetyStopError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="safety_stop",
                payload={
                    "reason": error.reason.value,
                    "errorCode": error.code,
                    "safeMessage": error.safe_message,
                },
            )
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except ProviderError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except Exception:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            logger.exception("Runtime v2 run failed unexpectedly", extra={"run_id": run.id})
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={
                    "errorCode": "runtime_internal_error",
                    "safeMessage": "执行过程出现内部错误。",
                },
                error_code="runtime_internal_error",
                safe_message="执行过程出现内部错误。",
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        finally:
            self._active_run_id = None
            self._cancellation_token = None

    async def enqueue_user_message(self, content: str) -> bool:
        async with self._queue_lock:
            if self._active_run_id is None:
                self._follow_up_messages.append(content)
                return False
            self._steering_messages.append(content)
            return True

    async def drain_follow_up_messages(self) -> tuple[str, ...]:
        async with self._queue_lock:
            messages = tuple(self._follow_up_messages)
            self._follow_up_messages.clear()
            return messages

    async def request_cancel(self, *, cancelled_by: str = "user") -> None:
        if self._cancellation_token is not None:
            self._cancellation_token.cancel()
        if self._active_run_id is None:
            return
        run = self._repository.get_run(self._active_run_id)
        if run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            return
        self._repository.transition_run_status(
            run.id,
            RunStatus.CANCELLING,
            event_type="run_cancel_requested",
            cancelled_by=cancelled_by,
        )

    async def _drain_steering_messages(self, run: RunRecord) -> list[ProviderMessage]:
        async with self._queue_lock:
            messages = self._steering_messages
            self._steering_messages = []
        projected: list[ProviderMessage] = []
        for message in messages:
            projected.append(ProviderMessage(role="user", content=message))
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="steer_injected",
                payload={"content": message},
            )
        return projected

    async def _maybe_compact_context(
        self,
        run: RunRecord,
        provider_messages: list[ProviderMessage],
    ) -> None:
        if self._compaction_hook is None:
            return
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_started",
            payload={},
        )
        result = await self._compaction_hook.compact(
            run,
            tuple(provider_messages),
        )
        if result is None:
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="compaction_completed",
                payload={"changed": False},
            )
            return
        provider_messages[:] = list(result.messages)
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_completed",
            payload={
                "changed": True,
                "summaryEntryId": result.summary_entry_id,
                "coveredEntryIds": list(result.covered_entry_ids),
            },
        )

    def _append_current_model_turn_ids(
        self,
        run_id: str,
        model_turn_ids: list[str],
    ) -> None:
        existing = set(model_turn_ids)
        model_turn_ids.extend(
            turn.id
            for turn in self._repository.list_model_turns(run_id)
            if turn.id not in existing
        )

    def _result(
        self,
        status: RunStatus,
        run: RunRecord,
        content: str,
        input_tokens: int,
        output_tokens: int,
        model_turn_ids: tuple[str, ...],
        pending_approval_ids: tuple[str, ...],
        *,
        assistant_entry_id: Optional[str] = None,
    ) -> RunExecutionResult:
        return RunExecutionResult(
            status=status,
            run=run,
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_turn_ids=model_turn_ids,
            pending_approval_execution_ids=pending_approval_ids,
            follow_up_messages=tuple(self._follow_up_messages),
            assistant_entry_id=assistant_entry_id,
        )
