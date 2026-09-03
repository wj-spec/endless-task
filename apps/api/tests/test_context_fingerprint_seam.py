"""M2 main-path prelude stage 1: per-turn context fingerprint signal."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderMessage,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RuntimeV2MetricsCollector,
    RunStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.execution import _messages_fingerprint
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

from tests.test_runtime_v2_execution import ScriptedProvider


class MessagesFingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_content_sensitive(self) -> None:
        messages = (
            ProviderMessage(role="system", content="sys"),
            ProviderMessage(role="user", content="hello"),
        )
        self.assertEqual(
            _messages_fingerprint(messages),
            _messages_fingerprint(messages),
        )
        changed = _messages_fingerprint(
            (ProviderMessage(role="system", content="sys"),
             ProviderMessage(role="user", content="bye"))
        )
        self.assertNotEqual(_messages_fingerprint(messages), changed)
        self.assertEqual(64, len(_messages_fingerprint(messages)))

    def test_tool_call_ids_are_included(self) -> None:
        from endless_task.runtime.provider import ProviderToolCall

        with_call = (
            ProviderMessage(
                role="assistant",
                content="",
                tool_calls=(ProviderToolCall(id="call_1", name="read_file", arguments={}),),
            ),
        )
        without = (ProviderMessage(role="assistant", content=""),)
        self.assertNotEqual(
            _messages_fingerprint(with_call),
            _messages_fingerprint(without),
        )


class FingerprintRecordingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_run_records_context_fingerprint_per_turn(self) -> None:
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
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("好的，收到。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        metrics = RuntimeV2MetricsCollector()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            metrics=metrics,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        fingerprints = metrics.context_fingerprints(conversation.id)
        self.assertGreaterEqual(len(fingerprints), 1)
        recorded = fingerprints[0]
        expected = _messages_fingerprint(provider.requests[-1].messages)
        self.assertEqual(expected, recorded)


if __name__ == "__main__":
    unittest.main()
