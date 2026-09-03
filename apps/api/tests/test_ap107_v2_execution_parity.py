"""AP-107 Stage 4c: flag-on auto subset runs through the v2 pipeline with
observable outcomes identical to the legacy path (real sqlite harness)."""

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
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2ReplayService,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

from tests.test_runtime_v2_execution import FakeTool, ScriptedProvider


class V2PipelineExecutionParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )

    def _run_scenario(self, *, v2_pipeline_enabled: bool):
        run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("我先读取文件。"),
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
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=13,
                        output_tokens=6,
                    ),
                ),
            ]
        )
        tool = FakeTool(content="file content")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            v2_pipeline_enabled=v2_pipeline_enabled,
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        executions = self.repository.list_tool_executions(result.model_turn_ids[0])
        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        return result, executions, tool, replay

    def test_flag_on_read_tool_matches_flag_off_outcome(self) -> None:
        result_off, executions_off, tool_off, replay_off = self._run_scenario(
            v2_pipeline_enabled=False
        )
        result_on, executions_on, tool_on, replay_on = self._run_scenario(
            v2_pipeline_enabled=True
        )

        self.assertEqual(result_off.status, result_on.status)
        self.assertEqual(RunStatus.COMPLETED, result_on.status)
        self.assertEqual(result_off.content, result_on.content)
        self.assertEqual("我先读取文件。文件内容是 file content。", result_on.content)
        self.assertEqual(len(executions_off), len(executions_on))
        self.assertEqual(ToolExecutionStatus.COMPLETED, executions_on[0].status)
        self.assertIsNotNone(executions_on[0].result_entry_id)
        self.assertEqual(1, len(tool_on.calls))
        self.assertEqual(1, len(tool_off.calls))
        self.assertEqual(replay_off.derived_status, replay_on.derived_status)
        self.assertEqual(RunStatus.COMPLETED, replay_on.derived_status)
        self.assertEqual((), replay_on.warnings)


if __name__ == "__main__":
    unittest.main()
