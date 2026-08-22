from __future__ import annotations

import asyncio
import unittest

from endless_task.runtime import (
    AgentLoop,
    CancellationManager,
    ProviderCompleted,
    ProviderMessage,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime.cancellation import RuntimeCancelled
from endless_task.tooling import (
    ToolApprovalMode,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        for event in self.responses[len(self.requests) - 1]:
            cancellation_token.raise_if_cancelled()
            yield event


class RecordingTool:
    def __init__(
        self,
        *,
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=1.0,
        max_output_characters=200_000,
        content="文件内容",
    ) -> None:
        self.definition = ToolDefinition(
            name="read_file",
            description="读取已授权的文本文件。",
            input_schema={
                "type": "object",
                "properties": {"file_id": {"type": "string"}},
                "required": ["file_id"],
                "additionalProperties": False,
            },
            effect=effect,
            approval_mode=approval_mode,
            timeout_seconds=timeout_seconds,
            max_output_characters=max_output_characters,
        )
        self.content = content
        self.calls = []

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content=self.content)


class BlockingTool(RecordingTool):
    def __init__(self, *, timeout_seconds=1.0) -> None:
        super().__init__(timeout_seconds=timeout_seconds)
        self.started = asyncio.Event()
        self.cancelled = False

    async def execute(self, call, cancellation_token):
        del call, cancellation_token
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class FailingTool(RecordingTool):
    def __init__(self) -> None:
        super().__init__()
        self.error = ToolError(
            "file_not_found",
            "找不到这个文件，可能已被删除。",
            retryable=False,
        )

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        raise self.error


def tool_call(call_id: str, file_id: str = "file_1") -> ProviderToolCall:
    return ProviderToolCall(
        id=call_id,
        name="read_file",
        arguments={"file_id": file_id},
    )


class AgentLoopTest(unittest.IsolatedAsyncioTestCase):
    async def _token(self):
        return await CancellationManager().acquire("turn_1", "variant_1")

    def _loop(self, provider, tool, *, max_iterations=4, max_tool_calls=8):
        registry = ToolRegistry()
        registry.register(tool)
        return AgentLoop(
            provider=provider,
            tool_registry=registry,
            model="fake-model",
            max_output_tokens=256,
            temperature=None,
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
        )

    async def _run(self, loop, token=None):
        deltas = []

        async def on_delta(delta):
            deltas.append(delta)

        result = await loop.run(
            request_id="variant_1",
            conversation_id="conversation_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            messages=(ProviderMessage(role="user", content="读取文件"),),
            cancellation_token=token or await self._token(),
            on_text_delta=on_delta,
        )
        return result, deltas

    async def test_tool_result_is_returned_to_model_before_final_answer(self) -> None:
        provider = ScriptedProvider(
            (
                (tool_call("call_1"), ProviderCompleted("tool_calls", 4, 1)),
                (ProviderTextDelta("总结完成"), ProviderCompleted("stop", 6, 2)),
            )
        )
        tool = RecordingTool(content="abcdef", max_output_characters=3)

        result, deltas = await self._run(self._loop(provider, tool))

        self.assertEqual("总结完成", result.content)
        self.assertEqual(10, result.input_tokens)
        self.assertEqual(3, result.output_tokens)
        self.assertEqual(2, result.iterations)
        self.assertEqual(1, result.tool_calls)
        self.assertEqual(["总结完成"], deltas)
        self.assertEqual(1, len(tool.calls))
        self.assertEqual("read_file", provider.requests[0].tools[0].name)
        self.assertEqual(
            ["user", "assistant", "tool"],
            [message.role for message in provider.requests[1].messages],
        )
        self.assertEqual("abc", provider.requests[1].messages[-1].content)
        self.assertEqual("call_1", provider.requests[1].messages[-1].tool_call_id)

    async def test_tool_failure_is_returned_to_model_instead_of_ending_turn(self) -> None:
        provider = ScriptedProvider(
            (
                (tool_call("call_1"), ProviderCompleted("tool_calls", 4, 1)),
                (
                    ProviderTextDelta("文件不存在，无法继续读取。"),
                    ProviderCompleted("stop", 6, 2),
                ),
            )
        )
        tool = FailingTool()

        result, deltas = await self._run(self._loop(provider, tool))

        self.assertEqual("文件不存在，无法继续读取。", result.content)
        self.assertEqual(2, result.iterations)
        self.assertEqual(1, result.tool_calls)
        self.assertEqual(1, len(tool.calls))
        failure_message = provider.requests[1].messages[-1]
        self.assertEqual("tool", failure_message.role)
        self.assertEqual("call_1", failure_message.tool_call_id)
        self.assertIn("file_not_found", failure_message.content)
        self.assertIn("找不到这个文件", failure_message.content)

    async def test_invalid_arguments_are_rejected_before_execution(self) -> None:
        provider = ScriptedProvider(
            ((tool_call("call_1", file_id=3), ProviderCompleted("tool_calls")),)
        )
        tool = RecordingTool()

        with self.assertRaises(ToolError) as raised:
            await self._run(self._loop(provider, tool))

        self.assertEqual("invalid_tool_arguments", raised.exception.code)
        self.assertEqual([], tool.calls)

    async def test_duplicate_operation_is_not_executed_twice(self) -> None:
        provider = ScriptedProvider(
            (
                (tool_call("call_1"), ProviderCompleted("tool_calls")),
                (tool_call("call_2"), ProviderCompleted("tool_calls")),
            )
        )
        tool = RecordingTool()

        with self.assertRaises(ToolError) as raised:
            await self._run(self._loop(provider, tool))

        self.assertEqual("duplicate_tool_call", raised.exception.code)
        self.assertEqual(1, len(tool.calls))

    async def test_iteration_limit_stops_before_another_tool_execution(self) -> None:
        provider = ScriptedProvider(
            (
                (tool_call("call_1", "file_1"), ProviderCompleted("tool_calls")),
                (tool_call("call_2", "file_2"), ProviderCompleted("tool_calls")),
            )
        )
        tool = RecordingTool()

        with self.assertRaises(ToolError) as raised:
            await self._run(self._loop(provider, tool, max_iterations=2))

        self.assertEqual("tool_loop_limit", raised.exception.code)
        self.assertEqual(1, len(tool.calls))

    async def test_total_tool_call_limit_stops_batch_before_execution(self) -> None:
        provider = ScriptedProvider(
            (
                (
                    tool_call("call_1", "file_1"),
                    tool_call("call_2", "file_2"),
                    ProviderCompleted("tool_calls"),
                ),
            )
        )
        tool = RecordingTool()

        with self.assertRaises(ToolError) as raised:
            await self._run(self._loop(provider, tool, max_tool_calls=1))

        self.assertEqual("tool_call_limit", raised.exception.code)
        self.assertEqual([], tool.calls)

    async def test_tool_timeout_cancels_execution_and_notifies_model(self) -> None:
        provider = ScriptedProvider(
            (
                (tool_call("call_1"), ProviderCompleted("tool_calls")),
                (
                    ProviderTextDelta("读取超时，请稍后重试。"),
                    ProviderCompleted("stop"),
                ),
            )
        )
        tool = BlockingTool(timeout_seconds=0.01)

        result, _ = await self._run(self._loop(provider, tool))

        self.assertEqual("读取超时，请稍后重试。", result.content)
        self.assertTrue(tool.cancelled)
        failure_message = provider.requests[1].messages[-1]
        self.assertEqual("tool", failure_message.role)
        self.assertEqual("call_1", failure_message.tool_call_id)
        self.assertIn("tool_timeout", failure_message.content)

    async def test_turn_cancellation_interrupts_tool(self) -> None:
        provider = ScriptedProvider(
            ((tool_call("call_1"), ProviderCompleted("tool_calls")),)
        )
        tool = BlockingTool()
        token = await self._token()
        execution = asyncio.create_task(self._run(self._loop(provider, tool), token))
        await asyncio.wait_for(tool.started.wait(), timeout=1)

        token.cancel()

        with self.assertRaises(RuntimeCancelled):
            await asyncio.wait_for(execution, timeout=1)
        self.assertTrue(tool.cancelled)

    async def test_required_approval_never_executes_in_r1_1(self) -> None:
        provider = ScriptedProvider(
            ((tool_call("call_1"), ProviderCompleted("tool_calls")),)
        )
        tool = RecordingTool(
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )

        with self.assertRaises(ToolError) as raised:
            await self._run(self._loop(provider, tool))

        self.assertEqual("approval_required", raised.exception.code)
        self.assertEqual([], tool.calls)


if __name__ == "__main__":
    unittest.main()
