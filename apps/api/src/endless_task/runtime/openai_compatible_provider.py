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


class OpenAICompatibleProvider:
    """Adapts an OpenAI-compatible Chat Completions stream to Runtime events."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        base_url: Optional[str],
        timeout_seconds: float = 60.0,
        client: Any = None,
    ) -> None:
        if not name.strip():
            raise ValueError("Provider name cannot be empty")
        if not api_key.strip():
            raise ValueError("Provider API key cannot be empty")
        if timeout_seconds <= 0:
            raise ValueError("Provider timeout must be positive")

        self.name = name.strip()
        if client is None:
            from openai import AsyncOpenAI

            options: dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout_seconds,
                "max_retries": 0,
            }
            if base_url:
                options["base_url"] = base_url
            client = AsyncOpenAI(**options)
        self._client = client

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

        stream: Any = None
        try:
            stream = await self._await_or_cancel(
                self._client.chat.completions.create(**arguments),
                cancellation_token,
            )
            iterator = stream.__aiter__()
            finish_reason: Optional[str] = None
            input_tokens: Optional[int] = None
            output_tokens: Optional[int] = None
            tool_call_fragments: dict[int, dict[str, str]] = {}

            while True:
                try:
                    chunk = await self._next_or_cancel(iterator, cancellation_token)
                except StopAsyncIteration:
                    break

                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", input_tokens)
                    output_tokens = getattr(usage, "completion_tokens", output_tokens)

                choices = getattr(chunk, "choices", None) or ()
                for choice in choices:
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None) if delta is not None else None
                    if isinstance(content, str) and content:
                        yield ProviderTextDelta(content)
                    raw_tool_calls = (
                        getattr(delta, "tool_calls", None) if delta is not None else None
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
            if finish_reason not in {"stop", "length", "content_filter", "tool_calls"}:
                raise ProviderError(
                    "unsupported_provider_event",
                    "模型返回了当前版本无法处理的结束状态。",
                    retryable=False,
                )
            for provider_tool_call in self._parse_tool_calls(tool_call_fragments):
                yield provider_tool_call
            yield ProviderCompleted(
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except (RuntimeCancelled, asyncio.CancelledError, ProviderError):
            raise
        except Exception as error:
            raise self._normalize_error(error) from error
        finally:
            if stream is not None:
                await self._close_stream(stream)

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
                raise ProviderError(
                    "invalid_tool_arguments",
                    "模型返回的工具参数不是有效 JSON。",
                    retryable=False,
                ) from error
            if not isinstance(arguments, dict):
                raise ProviderError(
                    "invalid_tool_arguments",
                    "模型返回的工具参数必须是 JSON 对象。",
                    retryable=False,
                )
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
        return ProviderError(
            "provider_error",
            "模型服务返回错误，可以重试。",
            retryable=False,
            provider_request_id=provider_request_id,
        )

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
