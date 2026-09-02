from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    ProductRuntimeEventProjection,
    RunStatus,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "progress"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        events = self.responses.pop(0) if self.responses else ()
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class ProgressTool:
    """实现 execute_with_progress 的示例工具。"""

    definition = ToolDefinition(
        name="progress_tool",
        description="report progress",
        input_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=5.0,
    )

    def __init__(self) -> None:
        self.progress_calls: list[tuple[str, object]] = []

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        del token
        return ToolResult(tool_call_id=call.id, content="done")

    async def execute_with_progress(
        self,
        call: ToolCall,
        token: CancellationToken,
        *,
        on_progress,
    ) -> ToolResult:
        del token
        on_progress(message="任务开始", percent=0.0)
        on_progress(message="任务进行中", percent=0.5)
        self.progress_calls.append(("executed", call.arguments.get("task")))
        return ToolResult(tool_call_id=call.id, content="done")


class PlainTool:
    """不实现 execute_with_progress(退化路径)。"""

    definition = ProgressTool.definition

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        del token
        return ToolResult(tool_call_id=call.id, content="plain done")


class ToolProgressTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "progress.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _start_run(self):
        conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="执行任务",
            client_request_id="progress-1",
        )
        return conversation, submission


class ToolProgressExecutionTest(ToolProgressTestBase):
    def test_progress_events_are_emitted_and_projected(self) -> None:
        conversation, submission = self._start_run()
        tool = ProgressTool()
        registry = ToolRegistry()
        registry.register(tool)
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_progress",
                        name="progress_tool",
                        arguments={"task": "长任务"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="progress-model",
            max_output_tokens=256,
            max_model_turns=4,
        )
        result = asyncio.run(
            executor.execute(
                submission.run.id,
                cancellation_token=CancellationToken(),
            )
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertTrue(tool.progress_calls)

        events = self.repository.list_runtime_events(submission.run.id)
        progress_events = [
            event for event in events if event.event_type == "tool_progress_update"
        ]
        self.assertEqual(len(progress_events), 2)
        messages = {event.payload.get("message") for event in progress_events}
        self.assertIn("任务开始", messages)
        self.assertIn("任务进行中", messages)

        # 产品事件投影。
        projection = ProductRuntimeEventProjection(self.repository).project_conversation(
            conversation.id
        )
        product_types = [event.event_type for event in projection]
        self.assertIn("tool_execution.progress", product_types)

    def test_plain_tool_without_progress_still_executes(self) -> None:
        conversation, submission = self._start_run()
        registry = ToolRegistry()
        registry.register(PlainTool())
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_plain",
                        name="progress_tool",
                        arguments={"task": "x"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="progress-model",
            max_output_tokens=256,
            max_model_turns=4,
        )
        result = asyncio.run(
            executor.execute(
                submission.run.id,
                cancellation_token=CancellationToken(),
            )
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)
        events = self.repository.list_runtime_events(submission.run.id)
        self.assertNotIn(
            "tool_progress_update", [event.event_type for event in events]
        )


class ShellProgressTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_shell_reports_progress(self) -> None:
        from endless_task.workspace_runtime.shell_runner import run_shell_command

        calls: list[tuple[str, object]] = []

        def on_progress(message: str, percent=None) -> None:
            calls.append((message, percent))

        result = await run_shell_command(
            command="sleep 1; echo done",
            cwd=Path("."),
            timeout_seconds=10.0,
            no_change_timeout_seconds=10.0,
            on_progress=on_progress,
            progress_interval_seconds=0.2,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("done", result.stdout)
        # 至少一次进度(开始回调)与可能的间隔回调。
        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "命令开始执行")


if __name__ == "__main__":
    unittest.main()
