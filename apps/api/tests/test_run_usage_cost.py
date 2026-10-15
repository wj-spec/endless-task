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
from endless_task.runtime_v2.run_budget import RunBudget
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

    async def test_budget_cost_cap_trips_without_legacy_parameter(self) -> None:
        """预算里的按类 cap 必须被真正消费（否则三档 $1/$3/$10 会被静默忽略）。

        不给 legacy 的 ``cost_cap_usd`` 形参，只给 ``run_budget``：执行器若仍只读
        构造参数，cap 就是 0.0（关闭），本条会看到 0 条升级而红。
        """
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=100_000,
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
            run_budget=RunBudget(hard_seconds=10.0, cost_cap_usd=0.1),
            pricing_catalog=CATALOG,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("cost_cap_exceeded", payload["reason"])
        self.assertAlmostEqual(0.1, payload["cost"]["capUsd"], places=8)

    async def test_budget_cost_cap_zero_disables_the_breaker(self) -> None:
        """R8 语义表第三行：显式把该类 cap 设为 0 = 关闭成本断路器。

        与上一条**同样的花销**（首轮 100K 输入 = $0.1）：cap=0.1 时升级，
        cap=0.0 时必须一条都不升——证明 0 是"关闭"而不是"忽略"或"极小上限"。
        """
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=100_000,
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
            run_budget=RunBudget(hard_seconds=10.0, cost_cap_usd=0.0),
            pricing_catalog=CATALOG,
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))


class CostStopPolicyTest(_UsageTestCase):
    """M3-3：成本到顶对**子运行档**（`stop_on_cost=True`）是真停止。

    同一条成本闸两种语义：父档"提请一次、继续跑"（今天的字节级行为），子档
    "优雅收尾"（COMPLETED + 引导语 + willStop=True，不再有后续模型调用）。
    两条用例用**同一份脚本**（首轮 100K 输入 = $0.1 正好到顶）只差策略位，
    对照出"策略位才是差别"。
    """

    def _two_turn_provider(self) -> ScriptedProvider:
        # 第二轮是"陷阱"：真停止时它必须**从未被消费**。
        return ScriptedProvider(
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

    async def test_stop_on_cost_budget_stops_the_run_gracefully(self) -> None:
        _, run = self._create_run()
        provider = self._two_turn_provider()
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(
                hard_seconds=10.0, cost_cap_usd=0.1, stop_on_cost=True
            ),
            pricing_catalog=CATALOG,
        )

        result = await executor.execute(run.id, cancellation_token=CancellationToken())

        # 优雅收尾而不是故障：落 COMPLETED，正文带上"怎么继续"的引导语。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertIsNone(result.run.error_code)
        self.assertIn("成本上限", result.content)
        # 真停止：脚本里的第二轮从未被消费。
        self.assertEqual(1, len(provider.requests))
        self.assertEqual(1, len(provider.responses))
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("cost_cap_exceeded", payload["reason"])
        self.assertTrue(payload["willStop"])
        # 报告自己带着 willStop=true，摘要就必须如实说"已经停止"，不能照抄父档
        # 那句"继续推进会继续消耗预算"——否则用户看到的字面与发生的事相反。
        self.assertIn("已按成本上限在这里停止", payload["summary"])
        self.assertNotIn("继续推进会继续消耗预算", payload["summary"])
        self.assertAlmostEqual(0.1, payload["cost"]["usedUsd"], places=8)
        self.assertAlmostEqual(0.1, payload["cost"]["capUsd"], places=8)
        self.assertTrue(payload["cost"]["priced"])
        entry = self.repository.get_entry(result.assistant_entry_id)
        self.assertEqual("cost_budget_stop", entry.payload["finishReason"])

    async def test_advise_only_budget_keeps_running_past_the_cap(self) -> None:
        # 父档（stop_on_cost=False，RunBudget 的默认值）：提请一次、继续跑完。
        _, run = self._create_run()
        provider = self._two_turn_provider()
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(hard_seconds=10.0, cost_cap_usd=0.1),
            pricing_catalog=CATALOG,
        )

        result = await executor.execute(run.id, cancellation_token=CancellationToken())

        self.assertEqual(RunStatus.COMPLETED, result.status)
        # 运行继续：第二轮照常被消费，最终按模型自己的 finish_reason 收尾。
        self.assertEqual(2, len(provider.requests))
        self.assertIn("完成。", result.content)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        self.assertEqual("cost_cap_exceeded", escalated[0].payload["reason"])
        self.assertFalse(escalated[0].payload["willStop"])
        # 父档（只提请、不停止）的摘要必须**逐字不变**：只有停止那一档才换措辞。
        self.assertEqual(
            "本次运行成本已达约 $0.1000（上限 $0.1000），继续推进会继续消耗预算。",
            escalated[0].payload["summary"],
        )
        entry = self.repository.get_entry(result.assistant_entry_id)
        self.assertEqual("stop", entry.payload["finishReason"])

    async def test_stop_on_cost_budget_never_trips_on_an_unpriced_model(self) -> None:
        """如实的例外：未定价模型 → 配了上限也拦不住（真实子运行形状）。

        `estimate_cost_usd` 对未定价的 provider/model 返回 None，
        `cost_cap_exceeded(cost_usd=None, ...)` 恒为 False。用子运行档的真实形状
        （`cost_cap_usd=3.0` + `stop_on_cost=True`）钉住这条如实的前提：脚本里的
        第二轮照常被消费、运行跑到自己的 finish_reason、一条升级都不发。
        """
        _, run = self._create_run()
        provider = self._two_turn_provider()
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="unlisted-model",
            run_budget=RunBudget(
                hard_seconds=10.0, cost_cap_usd=3.0, stop_on_cost=True
            ),
            pricing_catalog=CATALOG,
        )

        result = await executor.execute(run.id, cancellation_token=CancellationToken())

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(2, len(provider.requests))
        self.assertIn("完成。", result.content)
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))

    async def test_stop_on_cost_budget_runs_on_when_the_cap_is_not_reached(self) -> None:
        # 策略位不等于"配了就停"：cap 没到必须照常跑，否则每个子运行都会被误杀。
        _, run = self._create_run()
        provider = self._two_turn_provider()
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(
                hard_seconds=10.0, cost_cap_usd=1.0, stop_on_cost=True
            ),
            pricing_catalog=CATALOG,
        )

        result = await executor.execute(run.id, cancellation_token=CancellationToken())

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(2, len(provider.requests))
        self.assertIn("完成。", result.content)
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
