from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol, Sequence, Tuple

from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolError,
    RegisteredTool,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
)
from endless_task.tooling.schema import ToolSchemaError, validate_tool_arguments

from .cancellation import CancellationToken, RuntimeCancelled
from .provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentLoopResult:
    content: str
    finish_reason: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    iterations: int
    tool_calls: int


class ToolExecutionObserver(Protocol):
    async def prepare(
        self,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> Optional[ToolCall]:
        ...

    async def completed(self, call: ToolCall, result: ToolResult) -> None:
        ...

    async def failed(self, call: ToolCall, error_code: str) -> None:
        ...

    async def cancelled(self, call: ToolCall) -> None:
        ...


class NullToolExecutionObserver:
    async def prepare(self, tool, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        if tool.definition.approval_mode is ToolApprovalMode.REQUIRED:
            raise ToolError(
                "approval_required",
                "这项操作需要用户确认，当前运行时未配置确认流程。",
                retryable=False,
            )
        return call

    async def completed(self, call, result) -> None:
        del call, result

    async def failed(self, call, error_code) -> None:
        del call, error_code

    async def cancelled(self, call) -> None:
        del call


class AgentLoop:
    """Runs a bounded provider/tool cycle within one Assistant Turn."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        model: str,
        max_output_tokens: int,
        temperature: Optional[float],
        max_iterations: int,
        max_tool_calls: int,
        tool_observer: Optional[ToolExecutionObserver] = None,
        active_timeout_seconds: Optional[float] = None,
    ) -> None:
        self._provider = provider
        self._tool_registry = tool_registry
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._tool_observer = tool_observer or NullToolExecutionObserver()
        self._active_timeout_seconds = active_timeout_seconds

    async def run(
        self,
        *,
        request_id: str,
        conversation_id: str,
        turn_id: str,
        response_variant_id: str,
        messages: Sequence[ProviderMessage],
        cancellation_token: CancellationToken,
        on_text_delta: Callable[[str], Awaitable[None]],
    ) -> AgentLoopResult:
        provider_messages = list(messages)
        definitions = self._tool_registry.definitions()
        provider_tools = tuple(
            ProviderToolDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )
            for definition in definitions
        )
        accumulated_content = ""
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        total_tool_calls = 0
        seen_call_ids: set[str] = set()
        seen_signatures: set[Tuple[str, str]] = set()
        active_seconds = 0.0

        for iteration in range(1, self._max_iterations + 1):
            cancellation_token.raise_if_cancelled()
            provider_calls: list[ProviderToolCall] = []
            completion: Optional[ProviderCompleted] = None
            step_content = ""
            current_request_id = request_id if iteration == 1 else f"{request_id}:{iteration}"
            request = ProviderRequest(
                request_id=current_request_id,
                model=self._model,
                messages=tuple(provider_messages),
                max_output_tokens=self._max_output_tokens,
                temperature=self._temperature,
                tools=provider_tools,
            )

            remaining = self._remaining_active_seconds(active_seconds)
            provider_started = asyncio.get_running_loop().time()

            async def consume_provider_events() -> None:
                nonlocal completion, step_content, accumulated_content
                async for provider_event in self._provider.stream(
                    request,
                    cancellation_token,
                ):
                    cancellation_token.raise_if_cancelled()
                    if isinstance(provider_event, ProviderTextDelta):
                        if provider_event.text:
                            step_content += provider_event.text
                            accumulated_content += provider_event.text
                            await on_text_delta(provider_event.text)
                        continue
                    if isinstance(provider_event, ProviderToolCall):
                        provider_calls.append(provider_event)
                        continue
                    if isinstance(provider_event, ProviderCompleted):
                        if completion is not None:
                            raise ProviderError(
                                "invalid_provider_response",
                                "模型返回了重复的完成事件。",
                                retryable=False,
                            )
                        completion = provider_event
                        continue
                    raise ProviderError(
                        "unsupported_provider_event",
                        "模型返回了当前版本无法处理的响应。",
                        retryable=False,
                    )

            try:
                await asyncio.wait_for(consume_provider_events(), timeout=remaining)
            except asyncio.TimeoutError as error:
                raise ProviderError(
                    "agent_timeout",
                    "本次请求执行超时，可以重试。",
                    retryable=True,
                ) from error
            finally:
                active_seconds += asyncio.get_running_loop().time() - provider_started

            if completion is None:
                raise ProviderError(
                    "provider_error",
                    "模型响应意外结束，可以重试。",
                    retryable=True,
                )
            input_tokens = self._sum_usage(input_tokens, completion.input_tokens)
            output_tokens = self._sum_usage(output_tokens, completion.output_tokens)

            if not provider_calls:
                if completion.finish_reason == "tool_calls":
                    raise ProviderError(
                        "invalid_provider_response",
                        "模型结束了工具调用，但没有提供有效工具。",
                        retryable=False,
                    )
                return AgentLoopResult(
                    content=accumulated_content,
                    finish_reason=completion.finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    iterations=iteration,
                    tool_calls=total_tool_calls,
                )

            if iteration == self._max_iterations:
                raise ToolError(
                    "tool_loop_limit",
                    "工具调用次数达到上限，请缩小请求范围后重试。",
                    retryable=False,
                )
            if total_tool_calls + len(provider_calls) > self._max_tool_calls:
                raise ToolError(
                    "tool_call_limit",
                    "当前请求需要的工具调用过多，请缩小请求范围后重试。",
                    retryable=False,
                )

            provider_messages.append(
                ProviderMessage(
                    role="assistant",
                    content=step_content,
                    tool_calls=tuple(provider_calls),
                )
            )
            for provider_call in provider_calls:
                if provider_call.id in seen_call_ids:
                    raise ToolError(
                        "duplicate_tool_call",
                        "模型重复提交了同一个工具调用，已安全停止。",
                        retryable=False,
                    )
                tool = self._resolve_tool(provider_call.name)
                initial_status = (
                    ToolCallStatus.WAITING_APPROVAL
                    if tool.definition.approval_mode is ToolApprovalMode.REQUIRED
                    else ToolCallStatus.CREATED
                )
                try:
                    call = ToolCall(
                        id=provider_call.id,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        response_variant_id=response_variant_id,
                        tool_name=provider_call.name,
                        arguments=provider_call.arguments,
                        status=initial_status,
                        created_at=self._utc_now(),
                    )
                except ToolValidationError as error:
                    raise ToolError(
                        "invalid_tool_call",
                        "模型返回了无效的工具调用。",
                        retryable=False,
                    ) from error
                signature = (call.tool_name, call.canonical_arguments_json)
                if signature in seen_signatures:
                    raise ToolError(
                        "duplicate_tool_call",
                        "模型重复请求了相同操作，已安全停止。",
                        retryable=False,
                    )
                seen_call_ids.add(call.id)
                seen_signatures.add(signature)
                total_tool_calls += 1
                self._validate_arguments(tool.definition.input_schema, call)
                prepared_call = await self._tool_observer.prepare(
                    tool,
                    call,
                    cancellation_token,
                )
                if prepared_call is None:
                    provider_messages.append(
                        ProviderMessage(
                            role="tool",
                            content=(
                                "用户未授权这项操作。不要重复请求相同操作；"
                                "请说明未执行，或在无需该操作的情况下继续。"
                            ),
                            tool_call_id=call.id,
                            name=call.tool_name,
                        )
                    )
                    continue
                call = replace(prepared_call, status=ToolCallStatus.RUNNING)
                execution_started = asyncio.get_running_loop().time()
                failure_feedback: Optional[str] = None
                try:
                    remaining = self._remaining_active_seconds(active_seconds)
                    result = await self._execute_tool(
                        tool,
                        call,
                        cancellation_token,
                        timeout_seconds=min(tool.definition.timeout_seconds, remaining),
                    )
                except RuntimeCancelled:
                    await self._tool_observer.cancelled(call)
                    raise
                except ToolError as error:
                    await self._tool_observer.failed(call, error.code)
                    failure_feedback = (
                        f"工具执行失败（{error.code}）：{error.safe_message} "
                        "不要原样重复同一调用；可以调整参数后重试，或直接向用户说明情况。"
                    )
                except Exception:
                    logger.exception(
                        "Tool execution raised an unexpected error",
                        extra={
                            "tool_name": call.tool_name,
                            "tool_call_id": call.id,
                            "turn_id": turn_id,
                        },
                    )
                    await self._tool_observer.failed(call, "tool_execution_failed")
                    failure_feedback = (
                        "工具执行出现内部错误。不要原样重复同一调用；"
                        "可以调整参数后重试，或直接向用户说明情况。"
                    )
                finally:
                    active_seconds += (
                        asyncio.get_running_loop().time() - execution_started
                    )
                if failure_feedback is not None:
                    provider_messages.append(
                        ProviderMessage(
                            role="tool",
                            content=failure_feedback,
                            tool_call_id=call.id,
                            name=call.tool_name,
                        )
                    )
                    continue
                result = self._bounded_result(
                    result,
                    max_characters=tool.definition.max_output_characters,
                )
                await self._tool_observer.completed(call, result)
                provider_messages.append(
                    ProviderMessage(
                        role="tool",
                        content=result.content,
                        tool_call_id=call.id,
                        name=call.tool_name,
                    )
                )

        raise AssertionError("Agent loop ended without a terminal result")

    def _resolve_tool(self, name: str):
        try:
            return self._tool_registry.resolve(name)
        except ToolValidationError as error:
            raise ToolError(
                error.code,
                "模型请求了当前不可用的工具。",
                retryable=False,
            ) from error

    @staticmethod
    def _validate_arguments(schema, call: ToolCall) -> None:
        try:
            validate_tool_arguments(schema, call.arguments)
        except ToolSchemaError as error:
            raise ToolError(
                "invalid_tool_arguments",
                "模型提供的工具参数不符合要求。",
                retryable=False,
            ) from error

    @staticmethod
    async def _execute_tool(tool, call, token, *, timeout_seconds: float) -> ToolResult:
        execution = asyncio.create_task(tool.execute(call, token))
        cancellation = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
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
    def _sum_usage(current: Optional[int], value: Optional[int]) -> Optional[int]:
        if value is None:
            return current
        return (current or 0) + value

    def _remaining_active_seconds(self, elapsed: float) -> float:
        if self._active_timeout_seconds is None:
            return 24 * 60 * 60
        remaining = self._active_timeout_seconds - elapsed
        if remaining <= 0:
            raise ProviderError(
                "agent_timeout",
                "本次请求执行超时，可以重试。",
                retryable=True,
            )
        return remaining

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )
