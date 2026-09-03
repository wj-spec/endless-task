from __future__ import annotations

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from endless_task.runtime import (
    CancellationManager,
    OpenAICompatibleProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
    UnconfiguredProvider,
)
from endless_task.runtime.cancellation import RuntimeCancelled


class StubStream:
    def __init__(self, chunks) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


class FailingStream:
    def __init__(self, *, error: Exception, chunks=()) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise self._error

    async def close(self) -> None:
        self.closed = True


class BlockingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.started.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


class StubCompletions:
    def __init__(self, *, stream=None, error=None) -> None:
        self.stream = stream
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def create(self, **arguments):
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        return self.stream


class SequencedCompletions(StubCompletions):
    def __init__(self, streams) -> None:
        super().__init__()
        self._streams = list(streams)

    async def create(self, **arguments):
        self.calls.append(arguments)
        if not self._streams:
            raise AssertionError("No scripted stream remains")
        return self._streams.pop(0)


class BlockingCompletions(StubCompletions):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def create(self, **arguments):
        self.calls.append(arguments)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class StubModels:
    def __init__(self, *, items=(), error=None) -> None:
        self.items = items
        self.error = error

    async def list(self):
        if self.error is not None:
            raise self.error
        return SimpleNamespace(data=self.items)


class StubClient:
    def __init__(
        self,
        completions: StubCompletions,
        *,
        models: StubModels | None = None,
    ) -> None:
        self.chat = SimpleNamespace(completions=completions)
        if models is not None:
            self.models = models
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class StubApiError(Exception):
    def __init__(
        self,
        *,
        status_code=None,
        request_id=None,
        body=None,
        retry_after=None,
    ) -> None:
        super().__init__("sensitive upstream error")
        self.status_code = status_code
        self.request_id = request_id
        self.body = body
        headers = {} if retry_after is None else {"retry-after": retry_after}
        self.response = SimpleNamespace(headers=headers)


class ApiConnectionError(Exception):
    pass


def chunk(content=None, *, finish_reason=None, usage=None, tool_calls=None):
    choices = []
    if content is not None or finish_reason is not None or tool_calls is not None:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


class OpenAICompatibleProviderTest(unittest.IsolatedAsyncioTestCase):
    def _request(self) -> ProviderRequest:
        return ProviderRequest(
            request_id="variant-1",
            model="deepseek-chat",
            messages=(
                ProviderMessage(role="system", content="系统提示"),
                ProviderMessage(role="user", content="你好"),
            ),
            max_output_tokens=512,
            temperature=0.3,
        )

    async def _token(self):
        return await CancellationManager().acquire("turn-1", "variant-1")

    async def test_list_models_normalizes_ids_and_names(self) -> None:
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(
                StubCompletions(),
                models=StubModels(
                    items=(
                        SimpleNamespace(id=" model-b ", name="Model B"),
                        SimpleNamespace(id="model-a", name=None),
                        SimpleNamespace(id="", name="Ignored"),
                    )
                ),
            ),
        )

        self.assertEqual(
            (("model-b", "Model B"), ("model-a", "model-a")),
            await provider.list_models(),
        )

    async def test_list_models_normalizes_connection_errors(self) -> None:
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(
                StubCompletions(),
                models=StubModels(error=ApiConnectionError()),
            ),
        )

        with self.assertRaises(ProviderError) as raised:
            await provider.list_models()

        self.assertEqual("network_error", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    async def test_sdk_client_uses_bounded_retries_by_default(self) -> None:
        captured: dict[str, object] = {}

        class CapturingAsyncOpenAI:
            def __init__(self, **options) -> None:
                captured.update(options)

        with patch.dict(
            sys.modules,
            {"openai": SimpleNamespace(AsyncOpenAI=CapturingAsyncOpenAI)},
        ):
            OpenAICompatibleProvider(
                name="deepseek",
                api_key="secret",
                base_url="https://api.deepseek.com",
            )

        self.assertEqual(2, captured["max_retries"])
        self.assertEqual("secret", captured["api_key"])
        self.assertEqual("https://api.deepseek.com", captured["base_url"])

    async def test_stream_maps_chunks_finish_reason_and_usage(self) -> None:
        usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3)
        stream = StubStream(
            (
                chunk("你"),
                chunk("好"),
                chunk(finish_reason="stop"),
                chunk(usage=usage),
            )
        )
        completions = StubCompletions(stream=stream)
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret-never-logged",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
        )

        events = [event async for event in provider.stream(self._request(), await self._token())]

        self.assertEqual(["你", "好"], [e.text for e in events if isinstance(e, ProviderTextDelta)])
        completed = next(e for e in events if isinstance(e, ProviderCompleted))
        self.assertEqual("stop", completed.finish_reason)
        self.assertEqual(12, completed.input_tokens)
        self.assertEqual(3, completed.output_tokens)
        self.assertTrue(stream.closed)
        self.assertEqual(
            [
                {"role": "system", "content": "系统提示"},
                {"role": "user", "content": "你好"},
            ],
            completions.calls[0]["messages"],
        )
        self.assertEqual(512, completions.calls[0]["max_tokens"])
        self.assertEqual({"include_usage": True}, completions.calls[0]["stream_options"])
        self.assertEqual(0.3, completions.calls[0]["temperature"])

    async def test_stream_retries_retryable_body_failure_before_first_event(self) -> None:
        first_stream = FailingStream(error=StubApiError(status_code=500))
        second_stream = StubStream((chunk("恢复"), chunk(finish_reason="stop")))
        completions = SequencedCompletions((first_stream, second_stream))
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
            max_retries=1,
        )

        events = [event async for event in provider.stream(self._request(), await self._token())]

        self.assertEqual(2, len(completions.calls))
        self.assertTrue(first_stream.closed)
        self.assertTrue(second_stream.closed)
        self.assertEqual(
            ["恢复"],
            [event.text for event in events if isinstance(event, ProviderTextDelta)],
        )
        self.assertIsInstance(events[-1], ProviderCompleted)

    async def test_stream_does_not_retry_after_first_event(self) -> None:
        first_stream = FailingStream(
            chunks=(chunk("部分"),),
            error=StubApiError(status_code=500),
        )
        second_stream = StubStream((chunk("不应出现"), chunk(finish_reason="stop")))
        completions = SequencedCompletions((first_stream, second_stream))
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
            max_retries=1,
        )
        events: list[object] = []

        with self.assertRaises(ProviderError) as raised:
            async for event in provider.stream(self._request(), await self._token()):
                events.append(event)

        self.assertEqual("provider_unavailable", raised.exception.code)
        self.assertEqual(1, len(completions.calls))
        self.assertTrue(first_stream.closed)
        self.assertFalse(second_stream.closed)
        self.assertEqual(
            ["部分"],
            [event.text for event in events if isinstance(event, ProviderTextDelta)],
        )

    async def test_stream_maps_fragmented_tool_calls_and_followup_messages(self) -> None:
        first_fragment = SimpleNamespace(
            index=0,
            id="call_1",
            function=SimpleNamespace(name="read_", arguments='{"file_'),
        )
        second_fragment = SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name="file", arguments='id":"file_1"}'),
        )
        stream = StubStream(
            (
                chunk(tool_calls=(first_fragment,)),
                chunk(tool_calls=(second_fragment,)),
                chunk(finish_reason="tool_calls"),
            )
        )
        completions = StubCompletions(stream=stream)
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
        )
        tool_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"file_id": {"type": "string", "minLength": 1}},
            "type": "object",
            "properties": {"file_id": {"$ref": "#/$defs/file_id"}},
            "required": ["file_id"],
            "additionalProperties": False,
        }
        request = ProviderRequest(
            request_id="variant-1:2",
            model="deepseek-chat",
            messages=(
                ProviderMessage(role="user", content="读取文件"),
                ProviderMessage(
                    role="assistant",
                    content="",
                    tool_calls=(
                        ProviderToolCall(
                            id="prior_call",
                            name="read_file",
                            arguments={"file_id": "old"},
                        ),
                    ),
                ),
                ProviderMessage(
                    role="tool",
                    content="旧文件内容",
                    tool_call_id="prior_call",
                    name="read_file",
                ),
            ),
            max_output_tokens=512,
            tools=(
                ProviderToolDefinition(
                    name="read_file",
                    description="读取文件",
                    input_schema=tool_schema,
                ),
            ),
        )

        events = [event async for event in provider.stream(request, await self._token())]

        call = next(event for event in events if isinstance(event, ProviderToolCall))
        self.assertEqual("call_1", call.id)
        self.assertEqual("read_file", call.name)
        self.assertEqual({"file_id": "file_1"}, call.arguments)
        self.assertEqual("tool_calls", events[-1].finish_reason)
        sent = completions.calls[0]
        self.assertEqual("read_file", sent["tools"][0]["function"]["name"])
        self.assertEqual(tool_schema, sent["tools"][0]["function"]["parameters"])
        self.assertEqual("prior_call", sent["messages"][2]["tool_call_id"])
        self.assertEqual(
            '{"file_id":"old"}',
            sent["messages"][1]["tool_calls"][0]["function"]["arguments"],
        )

    async def test_stream_preserves_malformed_tool_arguments_as_parse_error(self) -> None:
        # 健壮性：模型返回不可解析的工具参数时，Provider 不应抛致命错误终止 run，
        # 而是产出一个带 parse_error 的调用，交由运行时作为可恢复错误反馈给模型。
        malformed = SimpleNamespace(
            index=0,
            id="call_bad",
            function=SimpleNamespace(
                name="read_file",
                arguments='{"file_id": "unterminated',
            ),
        )
        stream = StubStream(
            (
                chunk(tool_calls=(malformed,)),
                chunk(finish_reason="tool_calls"),
            )
        )
        completions = StubCompletions(stream=stream)
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
        )
        request = ProviderRequest(
            request_id="r:1",
            model="deepseek-chat",
            messages=(ProviderMessage(role="user", content="读取文件"),),
            max_output_tokens=512,
            tools=(
                ProviderToolDefinition(
                    name="read_file",
                    description="读取文件",
                    input_schema={"type": "object"},
                ),
            ),
        )

        events = [event async for event in provider.stream(request, await self._token())]

        call = next(event for event in events if isinstance(event, ProviderToolCall))
        self.assertEqual("call_bad", call.id)
        self.assertEqual("read_file", call.name)
        self.assertEqual({}, call.arguments)
        self.assertIsNotNone(call.parse_error)
        self.assertIn("不是有效 JSON", call.parse_error)
        self.assertEqual("tool_calls", events[-1].finish_reason)

    async def test_close_releases_sdk_client(self) -> None:
        client = StubClient(StubCompletions(stream=StubStream(())))
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=client,
        )

        await provider.close()

        self.assertTrue(client.closed)

    async def test_rate_limit_is_normalized_without_upstream_message(self) -> None:
        error = StubApiError(
            status_code=429,
            request_id="provider-request-1",
            retry_after="1.5",
        )
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret-never-logged",
            base_url="https://api.deepseek.com",
            client=StubClient(StubCompletions(error=error)),
        )

        with self.assertRaises(ProviderError) as raised:
            _ = [
                event
                async for event in provider.stream(self._request(), await self._token())
            ]

        self.assertEqual("rate_limited", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(1500, raised.exception.retry_after_ms)
        self.assertEqual("provider-request-1", raised.exception.provider_request_id)
        self.assertNotIn("sensitive", raised.exception.safe_message)

    async def test_conflict_is_retryable_without_upstream_message(self) -> None:
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret-never-logged",
            base_url="https://api.deepseek.com",
            client=StubClient(StubCompletions(error=StubApiError(status_code=409))),
        )

        with self.assertRaises(ProviderError) as raised:
            _ = [
                event
                async for event in provider.stream(self._request(), await self._token())
            ]

        self.assertEqual("provider_unavailable", raised.exception.code)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("sensitive", raised.exception.safe_message)

    async def test_deepseek_resource_finish_reason_is_retryable(self) -> None:
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(
                StubCompletions(
                    stream=StubStream(
                        (chunk(finish_reason="insufficient_system_resource"),)
                    )
                )
            ),
        )

        with self.assertRaises(ProviderError) as raised:
            _ = [
                event
                async for event in provider.stream(self._request(), await self._token())
            ]

        self.assertEqual("provider_unavailable", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

    async def test_context_limit_and_network_errors_are_normalized(self) -> None:
        context_error = StubApiError(
            status_code=400,
            body={"error": {"code": "context_length_exceeded"}},
        )
        context_provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(StubCompletions(error=context_error)),
        )
        with self.assertRaises(ProviderError) as context_raised:
            _ = [
                event
                async for event in context_provider.stream(
                    self._request(), await self._token()
                )
            ]
        self.assertEqual("context_too_large", context_raised.exception.code)
        self.assertFalse(context_raised.exception.retryable)

        network_provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(StubCompletions(error=ApiConnectionError())),
        )
        with self.assertRaises(ProviderError) as network_raised:
            _ = [
                event
                async for event in network_provider.stream(
                    self._request(), await self._token()
                )
            ]
        self.assertEqual("network_error", network_raised.exception.code)
        self.assertTrue(network_raised.exception.retryable)

    async def test_cancellation_closes_upstream_stream(self) -> None:
        stream = BlockingStream()
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(StubCompletions(stream=stream)),
        )
        manager = CancellationManager()
        token = await manager.acquire("turn-1", "variant-1")

        async def consume() -> None:
            async for _ in provider.stream(self._request(), token):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.started.wait(), timeout=1)
        token.cancel()
        with self.assertRaises(RuntimeCancelled):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(stream.closed)

    async def test_cancellation_aborts_pending_stream_creation(self) -> None:
        completions = BlockingCompletions()
        provider = OpenAICompatibleProvider(
            name="deepseek",
            api_key="secret",
            base_url="https://api.deepseek.com",
            client=StubClient(completions),
        )
        manager = CancellationManager()
        token = await manager.acquire("turn-1", "variant-1")

        async def consume() -> None:
            async for _ in provider.stream(self._request(), token):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(completions.started.wait(), timeout=1)
        token.cancel()
        with self.assertRaises(RuntimeCancelled):
            await asyncio.wait_for(task, timeout=1)
        self.assertTrue(completions.cancelled)

    async def test_unconfigured_provider_raises_safe_error(self) -> None:
        provider = UnconfiguredProvider("deepseek")
        with self.assertRaises(ProviderError) as raised:
            _ = [
                event
                async for event in provider.stream(self._request(), await self._token())
            ]
        self.assertEqual("provider_not_configured", raised.exception.code)
        self.assertFalse(raised.exception.retryable)
