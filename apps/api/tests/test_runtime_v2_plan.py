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
    Actor,
    ContextProjection,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
    UpdatePlanTool,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry


class ScriptedProvider:
    name = "plan"

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


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


class UpdatePlanToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "plan.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_execute_writes_plan_entry_and_event(self) -> None:
        conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="请完成一个多步骤任务",
            client_request_id="plan-1",
        )
        self.repository.start_run(submission.run.id)
        tool = UpdatePlanTool(self.repository)
        from endless_task.tooling import ToolCall, ToolCallStatus

        call = ToolCall(
            id="call_1",
            conversation_id=conversation.id,
            turn_id="turn_1",
            response_variant_id=submission.run.id,
            tool_name="update_plan",
            arguments={
                "plan": "重构导出模块",
                "steps": [
                    {"title": "分析现状"},
                    {"title": "设计接口"},
                    {"title": "实现迁移"},
                ],
            },
            status=ToolCallStatus.CREATED,
            created_at="2026-01-01T00:00:00Z",
        )
        result = await tool.execute(call, CancellationToken())
        self.assertIn("执行计划", result.content)
        self.assertIn("设计接口", result.content)

        # plan entry 在 lane 内,source_run 指向当前 run。
        entries = self.repository.list_lane_context_entries(submission.lane.id)
        plan_entries = [
            entry for entry in entries if entry.type is TranscriptEntryType.PLAN
        ]
        self.assertEqual(len(plan_entries), 1)
        self.assertEqual(plan_entries[0].actor, Actor.ASSISTANT)
        self.assertEqual(plan_entries[0].payload["title"], "重构导出模块")
        self.assertEqual(
            [step["title"] for step in plan_entries[0].payload["steps"]],
            ["分析现状", "设计接口", "实现迁移"],
        )
        self.assertEqual(plan_entries[0].source_run_id, submission.run.id)

        events = self.repository.list_runtime_events(submission.run.id)
        self.assertIn("plan_updated", [event.event_type for event in events])

    async def test_empty_plan_rejected(self) -> None:
        conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="任务",
            client_request_id="plan-2",
        )
        tool = UpdatePlanTool(self.repository)
        from endless_task.tooling import ToolCall, ToolCallStatus, ToolError

        call = ToolCall(
            id="call_2",
            conversation_id=conversation.id,
            turn_id="turn_1",
            response_variant_id=submission.run.id,
            tool_name="update_plan",
            arguments={"plan": "   "},
            status=ToolCallStatus.CREATED,
            created_at="2026-01-01T00:00:00Z",
        )
        with self.assertRaises(ToolError):
            await tool.execute(call, CancellationToken())


class PlanProjectionTest(unittest.TestCase):
    def test_plan_entry_projects_as_system_message(self) -> None:
        from endless_task.runtime_v2.domain import (
            TranscriptEntryRecord,
            TranscriptEntryStatus,
        )

        plan_entry = TranscriptEntryRecord(
            id="entry_plan",
            conversation_id="conv_1",
            parent_id=None,
            lane_id="lane_1",
            seq=1,
            type=TranscriptEntryType.PLAN,
            type_version=1,
            actor=Actor.ASSISTANT,
            status=TranscriptEntryStatus.FINAL,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            payload={"content": "1. 分析 2. 实现"},
            context_policy={"include_in_llm": True, "transform": "full"},
            display={},
        )
        result = ContextProjection().project((plan_entry,))
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].role, "system")
        self.assertIn("1. 分析", result.messages[0].content)
        self.assertIn(plan_entry.id, result.included_entry_ids)


class PlanGatewayIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "plan.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_model_can_write_plan_and_see_it_next_turn(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_plan",
                        name="update_plan",
                        arguments={
                            "plan": "完整任务",
                            "steps": [
                                {"title": "调研"},
                                {"title": "实现"},
                                {"title": "验证"},
                            ],
                        },
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成，计划已执行。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(UpdatePlanTool(self.repository))
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="plan-model",
            max_output_tokens=256,
        )
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(
            conversation.id,
            "帮我做一个完整任务",
            client_request_id="plan-gateway-1",
        )
        await _wait_until(
            lambda: self.repository.get_run(handle.run_id).status.value == "completed"
        )

        # plan entry 已固化。
        entries = self.repository.list_lane_context_entries(handle.lane_id)
        plan_entries = [
            entry for entry in entries if entry.type is TranscriptEntryType.PLAN
        ]
        self.assertEqual(len(plan_entries), 1)
        # 第二个模型请求的上下文包含 plan(工具结果回灌,本轮可见)。
        self.assertGreaterEqual(len(provider.requests), 2)
        second_messages = provider.requests[1].messages
        plan_visible = [
            message
            for message in second_messages
            if "调研" in message.content
        ]
        self.assertEqual(len(plan_visible), 1)
        # plan 同时持久化为 entry(后续 Run 投影为 system 消息)。
        projection = ContextProjection().project(entries)
        plan_system = [
            message
            for message in projection.messages
            if message.role == "system" and "调研" in message.content
        ]
        self.assertEqual(len(plan_system), 1)


if __name__ == "__main__":
    unittest.main()


class UpdatePlanProgressTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "plan.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _declare(self, tool, call, plan, steps=None, update=None):
        from endless_task.tooling import ToolCall, ToolCallStatus

        arguments: dict = {"plan": plan}
        if steps is not None:
            arguments["steps"] = steps
        if update is not None:
            arguments["update"] = update
        call = ToolCall(
            id=call,
            conversation_id=self.conversation.id,
            turn_id="turn_1",
            response_variant_id=self.run.id,
            tool_name="update_plan",
            arguments=arguments,
            status=ToolCallStatus.CREATED,
            created_at="2026-01-01T00:00:00Z",
        )
        return await tool.execute(call, CancellationToken())

    def _latest_plan(self):
        entries = self.repository.list_lane_context_entries(self.run.lane_id)
        for entry in reversed(entries):
            if entry.type is TranscriptEntryType.PLAN:
                return entry
        return None

    async def asyncSetUp(self) -> None:
        self.conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=self.conversation.id,
            lane_id=None,
            content="多步骤任务",
            client_request_id="plan-progress",
        )
        self.run = submission.run
        self.repository.start_run(self.run.id)
        self.tool = UpdatePlanTool(self.repository)

    async def test_single_step_update_merges_and_tracks_current(self) -> None:
        await self._declare(
            self.tool,
            "call_1",
            "重构",
            steps=[{"title": "分析"}, {"title": "设计"}, {"title": "实现"}],
        )
        first = self._latest_plan()
        self.assertEqual(first.payload["currentStepIndex"], None)

        await self._declare(
            self.tool,
            "call_2",
            "重构",
            update={"index": 0, "status": "completed"},
        )
        await self._declare(
            self.tool,
            "call_3",
            "重构",
            update={"index": 1, "status": "in_progress"},
        )
        latest = self._latest_plan()
        self.assertEqual(latest.payload["steps"][0]["status"], "completed")
        self.assertEqual(latest.payload["steps"][1]["status"], "in_progress")
        self.assertEqual(latest.payload["steps"][2]["status"], "pending")
        self.assertEqual(latest.payload["currentStepIndex"], 1)

    async def test_rewrite_steps_replaces_list(self) -> None:
        await self._declare(
            self.tool,
            "call_1",
            "任务",
            steps=[{"title": "旧步骤A"}, {"title": "旧步骤B"}],
        )
        await self._declare(
            self.tool,
            "call_2",
            "任务",
            steps=[{"title": "新步骤A"}, {"title": "新步骤B"}, {"title": "新步骤C"}],
        )
        latest = self._latest_plan()
        self.assertEqual(
            [step["title"] for step in latest.payload["steps"]],
            ["新步骤A", "新步骤B", "新步骤C"],
        )
        # append-only:两个 plan entry 都在。
        plan_entries = [
            entry
            for entry in self.repository.list_lane_context_entries(self.run.lane_id)
            if entry.type is TranscriptEntryType.PLAN
        ]
        self.assertEqual(len(plan_entries), 2)

    async def test_first_call_without_steps_rejected(self) -> None:
        from endless_task.tooling import ToolError

        with self.assertRaises(ToolError):
            await self._declare(self.tool, "call_1", "没有步骤")

    async def test_plan_updated_event_carries_structured_payload(self) -> None:
        await self._declare(
            self.tool,
            "call_1",
            "任务",
            steps=[{"title": "一步"}],
            update=None,
        )
        events = self.repository.list_runtime_events(self.run.id)
        plan_events = [
            event for event in events if event.event_type == "plan_updated"
        ]
        self.assertEqual(len(plan_events), 1)
        self.assertEqual(plan_events[0].payload["title"], "任务")
        self.assertEqual(plan_events[0].payload["steps"][0]["title"], "一步")
