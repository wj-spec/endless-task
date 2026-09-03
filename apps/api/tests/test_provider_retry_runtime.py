"""M3A RS-1: provider retry orchestration policy + shadow + runtime retry."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.reliability import (
    ProviderRetryConfig,
    ProviderRetryEvaluator,
    ProviderRetryRecord,
)
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime.cancellation import CancellationToken


class EvaluatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ProviderRetryEvaluator(ProviderRetryConfig(max_attempts=2))

    def record(self, code: str, **kwargs) -> ProviderRetryRecord:
        values = {"attempts_used": 0, "emitted_events": False}
        values.update(kwargs)
        return self.evaluator.evaluate(error_code=code, **values)

    def test_shadow_default_never_retries(self) -> None:
        shadow = ProviderRetryEvaluator(ProviderRetryConfig(max_attempts=0))
        record = shadow.evaluate(
            error_code="network_error",
            attempts_used=0,
            emitted_events=False,
        )
        self.assertTrue(record.retryable)
        self.assertFalse(record.would_retry)

    def test_retryable_network_error_retries_within_budget(self) -> None:
        record = self.record("network_error")
        self.assertTrue(record.would_retry)
        self.assertGreater(record.delay_seconds, 0)

    def test_auth_and_invalid_never_retry(self) -> None:
        for code in ("authentication_failed", "invalid_provider_response", "quota_exhausted"):
            with self.subTest(code=code):
                self.assertFalse(self.record(code).would_retry)

    def test_never_retry_after_events_emitted(self) -> None:
        self.assertFalse(
            self.record("network_error", emitted_events=True).would_retry
        )

    def test_attempt_cap_respected(self) -> None:
        self.assertFalse(self.record("network_error", attempts_used=2).would_retry)

    def test_deadline_respected(self) -> None:
        config = ProviderRetryConfig(max_attempts=2, deadline_seconds=0.05)
        evaluator = ProviderRetryEvaluator(config)
        record = evaluator.evaluate(
            error_code="network_error",
            attempts_used=0,
            emitted_events=False,
            now=0.04,
        )
        self.assertFalse(record.would_retry)

    def test_invalid_config_fails_closed(self) -> None:
        with self.assertRaises(Exception):
            ProviderRetryConfig(max_attempts=-1)
        with self.assertRaises(Exception):
            ProviderRetryConfig(deadline_seconds=0)


class _RetryThenOkProvider:
    """Fails the first stream call with a retryable error, then answers."""

    name = "retry-provider"

    def __init__(self) -> None:
        self.raised_once = False
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ):
        self.requests.append(request)
        if not self.raised_once:
            self.raised_once = True
            raise ProviderError(
                "network_error",
                "模拟网络故障。",
                retryable=True,
            )
        yield ProviderTextDelta("第二次成功。")
        yield ProviderCompleted(
            finish_reason="stop",
            input_tokens=10,
            output_tokens=5,
        )


class RuntimeRetryIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Real AgentRunExecutor run retries a pre-event provider failure once."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository

        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_provider_retry_happens_once_and_shadow_is_recorded(self) -> None:
        from endless_task.runtime_v2 import Actor, AgentRunExecutor, RunStatus, TranscriptEntryType
        from endless_task.tooling import ToolRegistry

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "hi"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        provider = _RetryThenOkProvider()
        shadows: list[ProviderRetryRecord] = []
        evaluator = ProviderRetryEvaluator(ProviderRetryConfig(max_attempts=1))
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="retry-model",
            provider_retry_evaluator=evaluator,
            provider_retry_observer=shadows.append,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertIn("第二次成功", result.content)
        self.assertEqual(2, len(provider.requests))
        self.assertEqual(1, len(shadows))
        self.assertTrue(shadows[0].retryable)
        self.assertTrue(shadows[0].would_retry)
        self.assertEqual("network_error", shadows[0].error_code)


if __name__ == "__main__":
    unittest.main()
