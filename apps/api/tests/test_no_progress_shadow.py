"""M2 prelude Stage 2: RS-2 no-progress shadow evaluation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.reliability import (
    StopLevel,
    StopPolicyProfile,
    TurnEvidence,
    build_turn_signal,
    evaluate_turn_history,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

from tests.test_runtime_v2_execution import FakeTool, ScriptedProvider


class TurnHistoryTest(unittest.TestCase):
    def test_identical_tool_outcome_counts_across_growing_context(self) -> None:
        evidences = (
            TurnEvidence(
                context_fingerprint="ctx_1",
                tool_signature="read_file",
                tool_outcome_fingerprint="h_1",
            ),
            TurnEvidence(
                context_fingerprint="ctx_2",  # context grew, still repeated call
                tool_signature="read_file",
                tool_outcome_fingerprint="h_1",
            ),
        )
        evaluation = evaluate_turn_history(evidences, StopPolicyProfile())
        self.assertEqual(StopLevel.RESTRICT, evaluation.level)
        self.assertEqual("identical_tool_outcome", evaluation.detector)

    def test_changed_outcome_is_progress(self) -> None:
        evidences = (
            TurnEvidence(
                context_fingerprint="ctx_1",
                tool_signature="read_file",
                tool_outcome_fingerprint="h_1",
            ),
            TurnEvidence(
                context_fingerprint="ctx_2",
                tool_signature="read_file",
                tool_outcome_fingerprint="h_2",
            ),
        )
        evaluation = evaluate_turn_history(evidences, StopPolicyProfile())
        self.assertEqual(StopLevel.NONE, evaluation.level)

    def test_signal_building_round_trip(self) -> None:
        evidence = TurnEvidence(
            context_fingerprint="ctx_1",
            tool_signature="run_shell",
            tool_outcome_fingerprint="h",
        )
        signal = build_turn_signal(evidence)
        self.assertEqual("ctx_1", signal.context_fingerprint)
        self.assertEqual("run_shell", signal.tool_signature)


class ShadowIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_repeated_identical_tool_calls_raise_shadow_level(self) -> None:
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
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("先读取。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("再读取。"),
                    ProviderToolCall(
                        id="call_2",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool(content="file content")
        registry = ToolRegistry()
        registry.register(tool)
        evaluations = []
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            no_progress_observer=evaluations.append,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertGreaterEqual(len(evaluations), 1)
        self.assertIn(
            evaluations[-1].level,
            (StopLevel.REMIND, StopLevel.RESTRICT, StopLevel.STOP),
        )
        self.assertEqual("identical_tool_outcome", evaluations[-1].detector)


if __name__ == "__main__":
    unittest.main()


class EnforcementIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """RS-2 enforcement: safe stop after repeated identical tool calls."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_repeated_identical_calls_stop_safely_when_enabled(self) -> None:
        from endless_task.runtime_v2 import RunStatus

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
        turns = []
        for index in range(3):
            turns.append(
                (
                    ProviderTextDelta(f"读取{index}。"),
                    ProviderToolCall(
                        id=f"call_{index}",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                )
            )
        turns.append((ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")))
        provider = ScriptedProvider(turns)
        tool = FakeTool(content="file content")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            no_progress_enforcement_enabled=True,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        # Stopped after the third identical tool turn; the final text turn
        # was never requested.
        self.assertEqual(3, len(provider.requests))
        self.assertIn("安全停止", result.content)

    async def test_default_off_runs_to_completion(self) -> None:
        from endless_task.runtime_v2 import RunStatus

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
        turns = []
        for index in range(4):
            turns.append(
                (
                    ProviderTextDelta(f"读取{index}。"),
                    ProviderToolCall(
                        id=f"call_{index}",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                )
            )
        turns.append((ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")))
        provider = ScriptedProvider(turns)
        tool = FakeTool(content="file content")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(5, len(provider.requests))
