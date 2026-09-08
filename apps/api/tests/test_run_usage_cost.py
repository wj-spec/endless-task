"""C5 成本/延迟可见：快照用量汇总与成本上限升级。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_ledger.pricing import PriceEntry, PricingCatalog
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

from tests.test_runtime_v2_execution import FakeTool, ScriptedProvider

#: 测试专用定价表：scripted/scripted-model 每百万 token 各 $1（便于算数）。
CATALOG = PricingCatalog(
    revision="test-1",
    prices={
        "scripted/scripted-model": PriceEntry(
            input_per_million=1.0,
            output_per_million=1.0,
        )
    },
)


class _UsageTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "任务"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, run

    def _events(self, run_id: str, event_type: str) -> list:
        return [
            event
            for event in self.repository.list_runtime_events(run_id)
            if event.event_type == event_type
        ]


class CostCapTest(_UsageTestCase):
    async def test_cost_cap_escalates_once_with_cost_payload(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("先读一下。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=100_000,
                        output_tokens=0,
                    ),
                ),
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=100_000,
                        output_tokens=0,
                    ),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            cost_cap_usd=0.1,
            pricing_catalog=CATALOG,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("cost_cap_exceeded", payload["reason"])
        self.assertIn("成本", payload["summary"])
        # 第一次跨过上限时即升级（首轮 100K 输入 = $0.1）。
        self.assertAlmostEqual(0.1, payload["cost"]["usedUsd"], places=8)
        self.assertAlmostEqual(0.1, payload["cost"]["capUsd"], places=8)
        self.assertTrue(payload["cost"]["priced"])

    async def test_no_cap_means_no_escalation(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10_000_000,
                        output_tokens=0,
                    ),
                )
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            pricing_catalog=CATALOG,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))

    async def test_unpriced_model_never_trips_cap(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10_000_000,
                        output_tokens=0,
                    ),
                )
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="unlisted-model",
            cost_cap_usd=0.000001,
            pricing_catalog=CATALOG,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))


class UsageSnapshotTest(_UsageTestCase):
    async def test_snapshot_reports_tokens_cost_and_duration(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=2000,
                        output_tokens=1000,
                    ),
                )
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
            pricing_catalog=CATALOG,
        )
        conversation = self.chat_repository.create_conversation()
        await gateway.send(conversation.id, "任务")
        await gateway.shutdown()
        snapshot = gateway.snapshot(conversation.id)
        usage = snapshot.get("usage")
        self.assertIsNotNone(usage)
        assert isinstance(usage, dict)
        self.assertEqual(2000, usage["inputTokens"])
        self.assertEqual(1000, usage["outputTokens"])
        self.assertEqual(3000, usage["totalTokens"])
        self.assertEqual(1, usage["turns"])
        self.assertTrue(usage["costPriced"])
        self.assertAlmostEqual(0.003, usage["costUsd"], places=8)
        self.assertEqual("test-1", usage["priceRevision"])
        self.assertEqual(["scripted/scripted-model"], usage["models"])
        self.assertIsInstance(usage["durationMs"], int)
        self.assertGreaterEqual(usage["durationMs"], 0)

    async def test_snapshot_marks_unpriced_models(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=10,
                    ),
                )
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="unlisted-model",
            max_output_tokens=128,
            pricing_catalog=CATALOG,
        )
        conversation = self.chat_repository.create_conversation()
        await gateway.send(conversation.id, "任务")
        await gateway.shutdown()
        usage = gateway.snapshot(conversation.id).get("usage")
        assert isinstance(usage, dict)
        self.assertIsNone(usage["costUsd"])
        self.assertFalse(usage["costPriced"])
        self.assertEqual(1, usage["unpricedTurns"])


if __name__ == "__main__":
    unittest.main()
