from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, AsyncIterator, Optional

from .cancellation import CancellationToken, RuntimeCancelled
from .provider import (
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)


_DEFAULT_MAX_RETRIES = 2
_PRE_EVENT_RETRYABLE_ERROR_CODES = frozenset(
    {"network_error", "provider_unavailable", "rate_limited", "request_timeout"}
)
_BASE_STREAM_RETRY_DELAY_SECONDS = 0.05
_MAX_STREAM_RETRY_DELAY_SECONDS = 1.0


class OpenAICompatibleProvider:
    """Adapts an OpenAI-compatible Chat Completions stream to Runtime events."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: Optional[str],
        timeout_seconds: float = 60.0,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        client: Any = None,
    ) -> None:
        if not name.strip():
            raise ValueError("Provider name cannot be empty")
        if not api_key.strip():
            raise ValueError("Provider API key cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("Provider timeout must be positive")
        if max_retries < 0:
            raise ValueError("Provider retry count cannot be negative")

        self.name = name.strip()
        self._max_retries = max_retries
        if client is None:
            from openai import AsyncOpenAI

            options: dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout_seconds,
                "max_retries": max_retries,
            }
            if base_url:
                options["base_url"] = base_url
            client = AsyncOpenAI(**options)
        self._client = client

    async def list_models(self) -> tuple[tuple[str, str], ...]:
        try:
            response = await self._client.models.list()
            items = getattr(response, "data", response)
            models: list[tuple[str, str]] = []
            for item in items or ():
                model_id = getattr(item, "id", None)
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                display_name = getattr(item, "name", None)
                models.append(
                    (
                        model_id.strip(),
                        display_name.strip()
                        if isinstance(display_name, str) and display_name.strip()
                        else model_id.strip(),
                    )
                )
            return tuple(models)
        except ProviderError:
            raise
        except Exception as error:
            raise self._normalize_error(error) from error

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        cancellation_token.raise_if_cancelled()
        arguments: dict[str, Any] = {
            "model": request.model,
            "messages": [self._message_json(message) for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            arguments["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": dict(tool.input_schema),
                    },
                }
                for tool in request.tools
            ]
        if request.temperature is not None:
            arguments["temperature"] = request.temperature

        attempt = 0
        while True:
            stream: Any = None
            stream_created = False
            emitted_event = False
            retryable_stream_error = False
            provider_error: Optional[ProviderError] = None
            try:
                stream = await self._await_or_cancel(
                    self._client.chat.completions.create(**arguments),
                    cancellation_token,
                )
                stream_created = True
                iterator = stream.__aiter__()
                finish_reason: Optional[str] = None
                input_tokens: Optional[int] = None
                output_tokens: Optional[int] = None
                tool_call_fragments: dict[int, dict[str, str]] = {}

                while True:
                    try:
                        chunk = await self._next_or_cancel(
                            iterator,
                            cancellation_token,
                        )
                    except StopAsyncIteration:
                        break

                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        input_tokens = getattr(usage, "prompt_tokens", input_tokens)
                        output_tokens = getattr(
                            usage,
                            "completion_tokens",
                            output_tokens,
                        )

                    choices = getattr(chunk, "choices", None) or ()
                    for choice in choices:
                        delta = getattr(choice, "delta", None)
                        content = (
                            getattr(delta, "content", None)
                            if delta is not None
                            else None
                        )
                        if isinstance(content, str) and content:
                            emitted_event = True
                            yield ProviderTextDelta(content)
                        raw_tool_calls = (
                            getattr(delta, "tool_calls", None)
                            if delta is not None
                            else None
                        ) or ()
                        for position, raw_tool_call in enumerate(raw_tool_calls):
                            index = getattr(raw_tool_call, "index", position)
                            if not isinstance(index, int) or index < 0:
                                raise ProviderError(
                                    "invalid_provider_response",
                                    "模型返回了无效的工具调用。",
                                    retryable=False,
                                )
                            fragments = tool_call_fragments.setdefault(
                                index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            call_id = getattr(raw_tool_call, "id", None)
                            if isinstance(call_id, str):
                                fragments["id"] += call_id
                            function = getattr(raw_tool_call, "function", None)
                            name = getattr(function, "name", None)
                            if isinstance(name, str):
                                fragments["name"] += name
                            raw_arguments = getattr(function, "arguments", None)
                            if isinstance(raw_arguments, str):
                                fragments["arguments"] += raw_arguments
                        candidate = getattr(choice, "finish_reason", None)
                        if candidate is not None:
                            finish_reason = str(candidate)

                if finish_reason == "insufficient_system_resource":
                    raise ProviderError(
                        "provider_unavailable",
                        "模型服务暂时不可用，可以稍后重试。",
                        retryable=True,
                    )
                if finish_reason not in {
                    "stop",
                    "length",
                    "content_filter",
                    "tool_calls",
                }:
                    raise ProviderError(
                        "unsupported_provider_event",
                        "模型返回了当前版本无法处理的结束状态。",
                        retryable=False,
                    )
                for provider_tool_call in self._parse_tool_calls(
                    tool_call_fragments
                ):
                    emitted_event = True
                    yield provider_tool_call
                emitted_event = True
                yield ProviderCompleted(
                    finish_reason=finish_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                return
            except (RuntimeCancelled, asyncio.CancelledError, ProviderError):
                raise
            except Exception as error:
                provider_error = self._normalize_error(error)
                retryable_stream_error = stream_created
            finally:
                if stream is not None:
                    await self._close_stream(stream)

            if provider_error is None:
                # 流正常结束但没有 finish_reason：响应被截断（多为连接中断），
                # 这种情况重试是有意义的，不再伪装成"未识别的 provider 错误"。
                raise ProviderError(
                    "incomplete_stream",
                    "模型服务没有返回完整结果（连接可能被中断），可以重试。",
                    retryable=True,
                )
            if not (
                retryable_stream_error
                and not emitted_event
                and self._should_retry_pre_event_stream_error(provider_error, attempt)
            ):
                raise provider_error
            await self._sleep_before_retry(provider_error, attempt, cancellation_token)
            attempt += 1

    @staticmethod
    def _message_json(message) -> dict[str, Any]:
        if message.role == "assistant" and message.tool_calls:
            return {
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(
                                dict(call.arguments),
                                ensure_ascii=False,
                                allow_nan=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        },
                    }
                    for call in message.tool_calls
                ],
            }
        if message.role == "tool":
            return {
                "role": "tool",
                "content": message.content,
                "tool_call_id": message.tool_call_id,
            }
        return {"role": message.role, "content": message.content}

    @staticmethod
    def _parse_tool_calls(
        fragments_by_index: dict[int, dict[str, str]],
    ) -> tuple[ProviderToolCall, ...]:
        calls = []
        for index in sorted(fragments_by_index):
            fragments = fragments_by_index[index]
            if not fragments["id"] or not fragments["name"]:
                raise ProviderError(
                    "invalid_provider_response",
                    "模型返回了不完整的工具调用。",
                    retryable=False,
                )
            try:
                arguments = json.loads(fragments["arguments"] or "{}")
            except (TypeError, ValueError) as error:
                calls.append(
                    ProviderToolCall(
                        id=fragments["id"],
                        name=fragments["name"],
                        arguments={},
                        parse_error="模型返回的工具参数不是有效 JSON。",
                    )
                )
                continue
            if not isinstance(arguments, dict):
                calls.append(
                    ProviderToolCall(
                        id=fragments["id"],
                        name=fragments["name"],
                        arguments={},
                        parse_error="模型返回的工具参数必须是 JSON 对象。",
                    )
                )
                continue
            calls.append(
                ProviderToolCall(
                    id=fragments["id"],
                    name=fragments["name"],
                    arguments=arguments,
                )
            )
        return tuple(calls)

    @staticmethod
    async def _next_or_cancel(iterator: Any, token: CancellationToken) -> Any:
        return await OpenAICompatibleProvider._await_or_cancel(
            iterator.__anext__(),
            token,
        )

    @staticmethod
    async def _await_or_cancel(awaitable: Any, token: CancellationToken) -> Any:
        next_result = asyncio.ensure_future(awaitable)
        cancelled = asyncio.create_task(token.wait())
        done, pending = await asyncio.wait(
            (next_result, cancelled),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if cancelled in done:
            if next_result in done:
                await asyncio.gather(next_result, return_exceptions=True)
            raise RuntimeCancelled()
        return next_result.result()

    @staticmethod
    async def _close_stream(stream: Any) -> None:
        close = getattr(stream, "close", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    def _should_retry_pre_event_stream_error(
        self,
        error: ProviderError,
        attempt: int,
    ) -> bool:
        return (
            attempt < self._max_retries
            and error.retryable
            and error.code in _PRE_EVENT_RETRYABLE_ERROR_CODES
        )

    async def _sleep_before_retry(
        self,
        error: ProviderError,
        attempt: int,
        token: CancellationToken,
    ) -> None:
        delay = self._retry_delay_seconds(error, attempt)
        if delay <= 0:
            return
        await self._await_or_cancel(asyncio.sleep(delay), token)

    @staticmethod
    def _retry_delay_seconds(error: ProviderError, attempt: int) -> float:
        if error.retry_after_ms is not None:
            return min(
                max(error.retry_after_ms / 1000, 0),
                _MAX_STREAM_RETRY_DELAY_SECONDS,
            )
        return min(
            _BASE_STREAM_RETRY_DELAY_SECONDS * (2**attempt),
            _MAX_STREAM_RETRY_DELAY_SECONDS,
        )

    @classmethod
    def _normalize_error(cls, error: Exception) -> ProviderError:
        status_code = getattr(error, "status_code", None)
        provider_request_id = getattr(error, "request_id", None)
        retry_after_ms = cls._retry_after_ms(error)
        provider_code = cls._provider_error_code(error)
        class_name = type(error).__name__.lower()

        if status_code == 401:
            return ProviderError(
                "authentication_failed",
                "模型服务密钥无效，请检查本地配置。",
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if status_code == 403:
            return ProviderError(
                "permission_denied",
                "当前密钥没有访问该模型的权限。",
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if status_code == 409:
            return ProviderError(
                "provider_unavailable",
                "模型服务正忙，可以稍后重试。",
                retryable=True,
                retry_after_ms=retry_after_ms,
                provider_request_id=provider_request_id,
            )
        if status_code == 429:
            return ProviderError(
                "rate_limited",
                "模型服务请求过于频繁，请稍后重试。",
                retryable=True,
                retry_after_ms=retry_after_ms,
                provider_request_id=provider_request_id,
            )
        if status_code == 408 or "timeout" in class_name:
            return ProviderError(
                "request_timeout",
                "模型响应超时，可以重试。",
                retryable=True,
                provider_request_id=provider_request_id,
            )
        if provider_code in {"context_length_exceeded", "max_tokens_exceeded"}:
            return ProviderError(
                "context_too_large",
                "当前对话内容过长，请开始一个新对话。",
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if provider_code in {"content_filter", "content_policy_violation"}:
            return ProviderError(
                "content_filtered",
                "模型服务拒绝处理这段内容。",
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if status_code in (400, 422):
            detail = cls._provider_error_message(error)
            return ProviderError(
                "invalid_request",
                (
                    f"模型服务拒绝了本次请求：{detail}"
                    if detail
                    else "模型服务拒绝了本次请求（请求格式或参数不被支持）。"
                ),
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if status_code == 404:
            detail = cls._provider_error_message(error)
            return ProviderError(
                "model_not_found",
                (
                    f"模型不存在或未开通：{detail}"
                    if detail
                    else "模型不存在或未开通，请检查模型名称。"
                ),
                retryable=False,
                provider_request_id=provider_request_id,
            )
        if isinstance(status_code, int) and status_code >= 500:
            return ProviderError(
                "provider_unavailable",
                "模型服务暂时不可用，可以稍后重试。",
                retryable=True,
                retry_after_ms=retry_after_ms,
                provider_request_id=provider_request_id,
            )
        if "connection" in class_name:
            return ProviderError(
                "network_error",
                "无法连接模型服务，请检查网络后重试。",
                retryable=True,
                provider_request_id=provider_request_id,
            )
        detail = cls._provider_error_message(error)
        return ProviderError(
            "provider_error",
            (
                f"模型服务返回错误：{detail}"
                if detail
                else "模型服务返回错误（原因未识别），请检查模型配置。"
            ),
            retryable=False,
            provider_request_id=provider_request_id,
        )

    @staticmethod
    def _provider_error_message(error: Exception) -> Optional[str]:
        """取服务端返回的人类可读错误信息（用于告诉用户真正的原因）。"""
        body = getattr(error, "body", None)
        if isinstance(body, dict):
            nested = body.get("error")
            if isinstance(nested, dict):
                message = nested.get("message")
            else:
                message = body.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:400]
        message = getattr(error, "message", None)
        if isinstance(message, str) and message.strip():
            return message.strip()[:400]
        return None

    @staticmethod
    def _provider_error_code(error: Exception) -> Optional[str]:
        body = getattr(error, "body", None)
        if not isinstance(body, dict):
            return None
        nested = body.get("error")
        if isinstance(nested, dict):
            value = nested.get("code")
        else:
            value = body.get("code")
        return value if isinstance(value, str) else None

    @staticmethod
    def _retry_after_ms(error: Exception) -> Optional[int]:
        response = getattr(error, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return None
        raw_value = headers.get("retry-after")
        if raw_value is None:
            return None
        try:
            seconds = float(raw_value)
        except (TypeError, ValueError):
            return None
        return max(0, int(seconds * 1000))


class UnconfiguredProvider:
    """Keeps the local API available while surfacing a safe configuration error."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request
        cancellation_token.raise_if_cancelled()
        if False:
            yield ProviderTextDelta("")
        raise ProviderError(
            "provider_not_configured",
            "尚未配置模型服务密钥。",
            retryable=False,
        )
