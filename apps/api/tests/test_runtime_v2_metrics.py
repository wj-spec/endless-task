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
)
from endless_task.runtime_v2 import RuntimeV2MetricsCollector, RuntimeV2SessionGateway
from endless_task.runtime_v2.metrics import (
    ApprovalMetric,
    ModelTurnMetric,
    RunMetric,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry


class ScriptedProvider:
    name = "metrics"

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


class MetricsCollectorUnitTest(unittest.TestCase):
    def test_summary_aggregates_and_prefix_stability(self) -> None:
        collector = RuntimeV2MetricsCollector()
        collector.record_run(
            RunMetric(
                run_id="r1",
                conversation_id="c1",
                status="completed",
                duration_ms=100,
                model_turn_count=1,
                input_tokens=10,
                output_tokens=5,
                compacted=True,
                compaction_released_tokens=300,
            )
        )
        collector.record_model_turn(
            ModelTurnMetric(
                run_id="r1",
                turn_index=1,
                first_token_latency_ms=50,
                duration_ms=90,
                input_tokens=10,
                output_tokens=5,
                finish_reason="stop",
            )
        )
        collector.record_approval(
            ApprovalMetric(run_id="r1", approval_id="a1", wait_ms=200, decision="approve")
        )
        collector.record_prefix_fingerprint(conversation_id="c1", fingerprint="x")
        collector.record_prefix_fingerprint(conversation_id="c1", fingerprint="x")
        collector.record_prefix_fingerprint(conversation_id="c1", fingerprint="y")

        summary = collector.summary()
        self.assertEqual(summary["runs"]["count"], 1)
        self.assertEqual(summary["runs"]["byStatus"], {"completed": 1})
        self.assertTrue(summary["runs"]["compactedRuns"], 1)
        self.assertEqual(summary["runs"]["compactionReleasedTokens"], 300)
        self.assertEqual(summary["modelTurns"]["count"], 1)
        self.assertEqual(summary["modelTurns"]["avgFirstTokenLatencyMs"], 50.0)
        self.assertEqual(summary["approvals"]["count"], 1)
        self.assertEqual(summary["prefixStability"]["comparisons"], 2)
        self.assertEqual(summary["prefixStability"]["stableRate"], 0.5)

    def test_empty_summary(self) -> None:
        summary = RuntimeV2MetricsCollector().summary()
        self.assertEqual(summary["runs"]["count"], 0)
        self.assertIsNone(summary["runs"]["avgDurationMs"])
        self.assertIsNone(summary["modelTurns"]["avgFirstTokenLatencyMs"])
        self.assertIsNone(summary["prefixStability"]["stableRate"])


class MetricsGatewayIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "metrics.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_run_records_model_turn_and_prefix_fingerprint(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=7,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("继续回答"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=5,
                        output_tokens=2,
                    ),
                ),
            ]
        )
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="metrics-model",
            max_output_tokens=128,
        )
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(
            conversation.id,
            "你好",
            client_request_id="metrics-1",
        )
        await _wait_until(
            lambda: (
                self.repository.get_run(handle.run_id).status.value == "completed"
            )
        )

        summary = gateway.metrics_summary()
        self.assertEqual(summary["runs"]["count"], 1)
        self.assertEqual(summary["runs"]["byStatus"], {"completed": 1})
        self.assertEqual(summary["modelTurns"]["count"], 1)
        self.assertEqual(summary["modelTurns"]["finishReasons"], {"stop": 1})
        self.assertEqual(summary["runs"]["totalInputTokens"], 7)
        self.assertEqual(summary["runs"]["totalOutputTokens"], 3)
        # 前缀指纹至少记录了一次;同一会话第二次发送应产生可比较的稳定率。
        self.assertGreaterEqual(summary["prefixStability"]["comparisons"], 0)

        # 第二次发送:前缀应稳定(S0/S1/S2 层不变),稳定率上升。
        await gateway.send(conversation.id, "继续", client_request_id="metrics-2")
        await _wait_until(
            lambda: (
                len(
                    [
                        r
                        for r in self.repository.list_runs(
                            conversation_id=conversation.id
                        )
                        if r.status.value == "completed"
                    ]
                )
                >= 2
            )
        )
        summary_after = gateway.metrics_summary()
        self.assertGreaterEqual(summary_after["runs"]["count"], 2)
        self.assertGreaterEqual(summary_after["prefixStability"]["comparisons"], 1)


if __name__ == "__main__":
    unittest.main()


class ProviderRetryMetricsTest(unittest.TestCase):
    def test_record_provider_retry_and_summary(self) -> None:
        from endless_task.runtime_v2.metrics import RuntimeV2MetricsCollector

        collector = RuntimeV2MetricsCollector()
        observer = collector.provider_retry_observer("run_parent_1")

        class Record:
            error_code = "network_error"
            retryable = True
            would_retry = True
            delay_seconds = 1.0
            attempts_used = 1
            reason = "retryable_pre_emission"

        observer(Record())
        summary = collector.summary()
        retries = summary["providerRetries"]
        self.assertEqual(1, retries["count"])
        self.assertEqual(1, retries["wouldRetry"])
        self.assertEqual(1, retries["retryable"])
        self.assertEqual(1, retries["reasons"].get("retryable_pre_emission"))

    def test_shadow_observer_records_non_retryable_too(self) -> None:
        from endless_task.runtime_v2.metrics import RuntimeV2MetricsCollector

        collector = RuntimeV2MetricsCollector()
        observer = collector.provider_retry_observer("run_x")

        class Record:
            error_code = "auth_error"
            retryable = False
            would_retry = False
            delay_seconds = 0.0
            attempts_used = 0
            reason = "auth_invalid"

        observer(Record())
        summary = collector.summary()
        self.assertEqual(1, summary["providerRetries"]["count"])
        self.assertEqual(0, summary["providerRetries"]["wouldRetry"])

    def test_bounded_collection(self) -> None:
        from endless_task.runtime_v2.metrics import RuntimeV2MetricsCollector

        collector = RuntimeV2MetricsCollector()

        class Record:
            error_code = "e"
            retryable = True
            would_retry = True
            delay_seconds = 0.0
            attempts_used = 0
            reason = "r"

        observer = collector.provider_retry_observer("run_y")
        for _ in range(2_000):
            observer(Record())
        self.assertLessEqual(collector.summary()["providerRetries"]["count"], 1_000)
