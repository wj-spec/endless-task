"""M4A DR-1 slice 3: RuntimeV2AgentKernel production adapter conformance."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcomeStatus,
    RunUsage,
)
from endless_task.agent_platform import AgentPlatformError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2AgentKernel,
    ToolExecutionLimits,
    TranscriptEntryType,
)
from endless_task.runtime_v2.agent_kernel_adapter import (
    ConversationFactory,
    ExecutorBuilder,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class FailingProvider:
    name = "failing"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        raise ProviderError(
            "provider_unavailable",
            "模型服务不可用。",
            retryable=True,
        )
        yield  # pragma: no cover - makes this an async generator


class PausedProvider:
    name = "paused"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.started = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderTextDelta("部分")
        self.started.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class ReadFileTool:
    """最小只读工具：只用于证明适配器带回真实工具调用数。"""

    def __init__(self, name: str = "read_file") -> None:
        self.definition = ToolDefinition(
            name=name,
            description="读取文件",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=2.0,
        )
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content="file content")


def trace_context(run_id: str = "child_1"):
    from endless_task.runtime_ledger import TraceContext

    return TraceContext(
        trace_id="trace_parent_1",
        run_id=run_id,
        correlation_id="corr_1",
    )


def run_command(run_id: str = "child_1") -> RunCommand:
    return RunCommand(
        run_id=run_id,
        conversation_id=f"conv_delegation_{run_id}",
        lane_id=f"lane_delegation_{run_id}",
        trigger_entry_id=f"entry_delegation_{run_id}",
        message=AgentMessage(
            role=AgentMessageRole.USER,
            content="调查并总结",
        ),
        trace=trace_context(run_id),
        capability_profile="subagent_readonly",
    )


class RuntimeV2AgentKernelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.executors_created = 0

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _conversation_factory(self) -> ConversationFactory:
        async def factory(child_run_id: str, *, workspace_id=None):
            return self.chat_repository.create_delegation_conversation(
                child_run_id, workspace_id=workspace_id
            )

        return factory

    def _executor_builder(self, provider, registry=None) -> ExecutorBuilder:
        def build(command: RunCommand) -> AgentRunExecutor:
            del command  # 这些用例只关心执行器本身，不校验它拿到的预算
            self.executors_created += 1
            return AgentRunExecutor(
                repository=self.repository,
                provider=provider,
                tool_registry=registry or ToolRegistry(),
                model="scripted-model",
                tool_execution_limits=ToolExecutionLimits(
                    max_concurrent_calls=1,
                    max_calls_per_turn=4,
                ),
            )

        return build

    def _kernel(self, provider, registry=None) -> RuntimeV2AgentKernel:
        return RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=self._conversation_factory(),
            executor_builder=self._executor_builder(provider, registry),
        )

    def test_completed_child_projects_assistant_message_and_persists_run(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("调查完成，结论如下。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        kernel = self._kernel(provider)
        command = run_command()

        outcome = asyncio.run(kernel.run(command))

        self.assertEqual(RunOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual("child_1", outcome.run_id)
        self.assertEqual("调查完成，结论如下。", outcome.assistant_message.content)
        # 终态必须带回真实用量：四列的成本闸不能建在恒 0 上。
        self.assertEqual(
            RunUsage(input_tokens=10, output_tokens=4, model_turns=1, tool_calls=0),
            outcome.usage,
        )
        run = self.repository.get_run("child_1")
        self.assertEqual(RunStatus.COMPLETED, run.status)
        entries = self.repository.list_entries(run.lane_id)
        self.assertEqual(TranscriptEntryType.USER_MESSAGE, entries[0].type)
        self.assertEqual(TranscriptEntryType.ASSISTANT_MESSAGE, entries[-1].type)
        self.assertEqual("child_1", entries[-1].source_run_id)

    def test_child_usage_counts_real_tool_calls(self) -> None:
        """工具调用数必须是真值：适配器不许把它写成 0 了事。"""
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("先读文件。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=11,
                        output_tokens=4,
                    ),
                ),
                (
                    ProviderTextDelta("读完了。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=13,
                        output_tokens=6,
                    ),
                ),
            ]
        )
        tool = ReadFileTool()
        registry = ToolRegistry()
        registry.register(tool)
        kernel = self._kernel(provider, registry)

        outcome = asyncio.run(kernel.run(run_command()))

        self.assertEqual(RunOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual(
            RunUsage(input_tokens=24, output_tokens=10, model_turns=2, tool_calls=1),
            outcome.usage,
        )
        self.assertEqual(1, len(tool.calls))

    def test_failed_child_projects_failed_with_diagnostic(self) -> None:
        kernel = self._kernel(FailingProvider())
        outcome = asyncio.run(kernel.run(run_command()))
        self.assertEqual(RunOutcomeStatus.FAILED, outcome.status)
        self.assertTrue(
            any(d.code == "provider_unavailable" for d in outcome.diagnostics)
        )
        run = self.repository.get_run("child_1")
        self.assertEqual(RunStatus.FAILED, run.status)

    def test_cancel_propagates_to_running_child(self) -> None:
        provider = PausedProvider()
        kernel = self._kernel(provider)
        command = run_command()

        async def scenario():
            task = asyncio.create_task(kernel.run(command))
            await provider.started.wait()
            await kernel.cancel("child_1", "不再需要")
            return await task

        outcome = asyncio.run(scenario())
        self.assertEqual(RunOutcomeStatus.CANCELLED, outcome.status)
        run = self.repository.get_run("child_1")
        self.assertEqual(RunStatus.CANCELLED, run.status)

    def test_steer_on_inactive_run_rejected(self) -> None:
        kernel = self._kernel(ScriptedProvider([]))
        with self.assertRaises(AgentPlatformError) as caught:
            asyncio.run(
                kernel.steer(
                    "child_1",
                    AgentMessage(role=AgentMessageRole.USER, content="继续"),
                )
            )
        self.assertEqual("invalid_agent_kernel_value", caught.exception.code)

    def test_cancel_on_inactive_run_rejected(self) -> None:
        kernel = self._kernel(ScriptedProvider([]))
        with self.assertRaises(AgentPlatformError) as caught:
            asyncio.run(kernel.cancel("child_1", "停止"))
        self.assertEqual("invalid_agent_kernel_value", caught.exception.code)

    def test_duplicate_active_run_rejected(self) -> None:
        provider = PausedProvider()
        kernel = self._kernel(provider)
        command = run_command()

        async def scenario():
            task = asyncio.create_task(kernel.run(command))
            await provider.started.wait()
            with self.assertRaises(AgentPlatformError) as caught:
                await kernel.run(command)
            self.assertEqual("invalid_agent_kernel_value", caught.exception.code)
            await kernel.cancel("child_1", "停止")
            return await task

        outcome = asyncio.run(scenario())
        self.assertEqual(RunOutcomeStatus.CANCELLED, outcome.status)

    def test_each_run_gets_fresh_executor(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("一"),
                    ProviderCompleted(finish_reason="stop"),
                ),
                (
                    ProviderTextDelta("二"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        kernel = self._kernel(provider)
        first = asyncio.run(kernel.run(run_command(run_id="child_1")))
        second = asyncio.run(kernel.run(run_command(run_id="child_2")))
        self.assertEqual(RunOutcomeStatus.COMPLETED, first.status)
        self.assertEqual(RunOutcomeStatus.COMPLETED, second.status)
        self.assertEqual(2, self.executors_created)

    def test_executor_builder_receives_the_run_command(self) -> None:
        """构造器拿得到 RunCommand——子运行的时长墙就是靠它算出来的。"""
        provider = ScriptedProvider(
            [(ProviderTextDelta("完成"), ProviderCompleted())]
        )
        seen: list[RunCommand] = []

        def build(command: RunCommand) -> AgentRunExecutor:
            seen.append(command)
            self.executors_created += 1
            return AgentRunExecutor(
                repository=self.repository,
                provider=provider,
                tool_registry=ToolRegistry(),
                model="scripted-model",
            )

        kernel = RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=self._conversation_factory(),
            executor_builder=build,
        )

        asyncio.run(kernel.run(run_command("child_builder")))

        self.assertEqual(["child_builder"], [item.run_id for item in seen])


if __name__ == "__main__":
    unittest.main()
