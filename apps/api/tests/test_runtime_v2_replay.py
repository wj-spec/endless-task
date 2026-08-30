from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.repositories import ConflictError
from endless_task.runtime_v2 import (
    Actor,
    CrashRecoveryAction,
    CrashRecoveryClassification,
    CrashRecoveryReason,
    ModelTurnStatus,
    RunStatus,
    RuntimeV2ReplayService,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)


class RuntimeV2ReplayServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "replay.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.service = RuntimeV2ReplayService(self.repository)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self, conversation_id: str | None = None):
        if conversation_id is None:
            conversation = self.chat_repository.create_conversation()
            conversation_id = conversation.id
        lane = self.repository.create_lane(conversation_id=conversation_id)
        trigger = self.repository.append_entry(
            conversation_id=conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation_id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation_id, lane, trigger, run

    def test_replays_run_turn_tool_and_partial_content(self) -> None:
        conversation_id, lane, trigger, run = self._create_run()
        turn = self.repository.create_model_turn(
            run_id=run.id,
            provider="fake",
            model="fake-model",
        )
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
        )

        self.repository.update_run_status(run.id, RunStatus.RUNNING)
        self.repository.update_model_turn_status(turn.id, ModelTurnStatus.STREAMING)
        self.repository.update_tool_execution_status(
            tool.id,
            ToolExecutionStatus.RUNNING,
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_turn_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_text_delta",
            payload={"delta": "你好"},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_started",
            payload={"toolExecutionId": tool.id},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_completed",
            payload={"toolExecutionId": tool.id},
        )
        self.repository.update_tool_execution_status(
            tool.id,
            ToolExecutionStatus.COMPLETED,
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_turn_completed",
            payload={"inputTokens": 12, "outputTokens": 8},
        )
        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.COMPLETED,
            input_tokens=12,
            output_tokens=8,
        )
        assistant = self.repository.append_entry(
            conversation_id=conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "文件已读取"},
            context_policy={"include_in_llm": True, "transform": "full"},
            parent_id=trigger.id,
            source_run_id=run.id,
        )
        self.repository.set_run_assistant_entry(
            run_id=run.id,
            assistant_entry_id=assistant.id,
            is_active_variant=True,
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_completed",
            payload={},
        )
        self.repository.update_run_status(run.id, RunStatus.COMPLETED)
        self.repository.set_conversation_pointer(
            conversation_id=conversation_id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )

        replay = self.service.replay_run(run.id)
        self.assertEqual(RunStatus.COMPLETED, replay.derived_status)
        self.assertEqual("你好", replay.partial_content)
        self.assertEqual(1, len(replay.model_turns))
        self.assertEqual(ModelTurnStatus.COMPLETED, replay.model_turns[0].derived_status)
        self.assertEqual("你好", replay.model_turns[0].partial_content)
        self.assertEqual(
            ToolExecutionStatus.COMPLETED,
            replay.model_turns[0].tool_executions[0].derived_status,
        )
        self.assertEqual((), replay.warnings)
        self.assertEqual((), replay.model_turns[0].warnings)
        self.assertEqual((), replay.model_turns[0].tool_executions[0].warnings)

        snapshot = self.service.build_conversation_snapshot(conversation_id)
        self.assertEqual((trigger.id, assistant.id), tuple(
            entry.id for entry in snapshot.entries
        ))
        self.assertEqual(run.id, snapshot.active_run_id)
        self.assertEqual(12, snapshot.input_tokens)
        self.assertEqual(8, snapshot.output_tokens)
        self.assertEqual("你好", snapshot.active_run.partial_content)

    def test_classifies_interrupted_provider_state_as_recoverable(self) -> None:
        conversation_id, lane, _, run = self._create_run()
        turn = self.repository.create_model_turn(run_id=run.id)
        self.repository.update_run_status(run.id, RunStatus.RUNNING)
        self.repository.update_model_turn_status(turn.id, ModelTurnStatus.STREAMING)
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_turn_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_text_delta",
            payload={"delta": "partial"},
        )
        self.repository.set_active_run_variant(run.id)
        self.repository.set_conversation_pointer(
            conversation_id=conversation_id,
            active_lane_id=lane.id,
        )

        report = self.service.classify_crash_recovery(run.id)

        self.assertEqual(CrashRecoveryClassification.RECOVERABLE, report.classification)
        self.assertEqual(CrashRecoveryAction.RESUME, report.action)
        self.assertTrue(report.can_auto_resume)
        self.assertIn(
            CrashRecoveryReason.INTERRUPTED_MODEL_TURN,
            tuple(finding.reason for finding in report.findings),
        )
        self.assertEqual("partial", report.replay.partial_content)

        snapshot = self.service.build_conversation_snapshot(conversation_id)
        self.assertEqual(1, snapshot.snapshot_version)
        self.assertEqual(3, snapshot.last_event_seq)

    def test_interrupted_mixed_tool_states_never_auto_resume(self) -> None:
        conversation_id, lane, trigger, run = self._create_run()
        turn = self.repository.create_model_turn(run_id=run.id)
        completed = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_completed",
            tool_name="read_file",
            arguments={"path": "a.txt"},
        )
        running = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_running",
            tool_name="write_file",
            arguments={"path": "b.txt", "content": "value"},
        )
        result = self.repository.append_entry(
            conversation_id=conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.TOOL_RESULT,
            actor=Actor.TOOL,
            payload={"content": "file content"},
            context_policy={"include_in_llm": True, "transform": "tool_result"},
            parent_id=trigger.id,
            source_run_id=run.id,
        )
        self.repository.update_run_status(run.id, RunStatus.RUNNING)
        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.EXECUTING_TOOLS,
        )
        self.repository.update_tool_execution_status(
            completed.id,
            ToolExecutionStatus.COMPLETED,
            result_entry_id=result.id,
        )
        self.repository.update_tool_execution_status(
            running.id,
            ToolExecutionStatus.RUNNING,
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="model_turn_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_started",
            payload={"toolExecutionId": completed.id},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_completed",
            payload={"toolExecutionId": completed.id},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            model_turn_id=turn.id,
            event_type="tool_execution_started",
            payload={"toolExecutionId": running.id},
        )

        report = self.service.classify_crash_recovery(run.id)
        self.assertEqual(
            CrashRecoveryClassification.NEEDS_USER_ACTION,
            report.classification,
        )
        self.assertEqual(CrashRecoveryAction.AWAIT_USER_DECISION, report.action)
        self.assertFalse(report.can_auto_resume)
        self.assertEqual((running.id,), report.side_effect_uncertain_tool_execution_ids)
        self.assertEqual((), report.waiting_approval_tool_execution_ids)
        self.assertIn(
            CrashRecoveryReason.TOOL_SIDE_EFFECT_UNCERTAIN,
            tuple(finding.reason for finding in report.findings),
        )

    def test_waiting_approval_requires_user_action(self) -> None:
        _, _, _, run = self._create_run()
        turn = self.repository.create_model_turn(run_id=run.id)
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_approval",
            tool_name="write_file",
            arguments={"path": "b.txt"},
        )
        self.repository.update_run_status(run.id, RunStatus.WAITING_APPROVAL)
        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.EXECUTING_TOOLS,
        )
        self.repository.update_tool_execution_status(
            tool.id,
            ToolExecutionStatus.WAITING_APPROVAL,
        )

        report = self.service.classify_crash_recovery(run.id)
        self.assertEqual(
            CrashRecoveryClassification.NEEDS_USER_ACTION,
            report.classification,
        )
        self.assertEqual((tool.id,), report.waiting_approval_tool_execution_ids)
        self.assertFalse(report.can_auto_resume)

    def test_completed_tool_without_result_requires_user_action(self) -> None:
        _, _, _, run = self._create_run()
        turn = self.repository.create_model_turn(run_id=run.id)
        tool = self.repository.create_tool_execution(
            model_turn_id=turn.id,
            call_id="call_missing_result",
            tool_name="write_file",
            arguments={"path": "b.txt"},
        )
        self.repository.update_run_status(run.id, RunStatus.RUNNING)
        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.EXECUTING_TOOLS,
        )
        self.repository.update_tool_execution_status(
            tool.id,
            ToolExecutionStatus.COMPLETED,
        )

        report = self.service.classify_crash_recovery(run.id)
        self.assertEqual((tool.id,), report.side_effect_uncertain_tool_execution_ids)
        self.assertIn(
            CrashRecoveryReason.TOOL_RESULT_MISSING,
            tuple(finding.reason for finding in report.findings),
        )
        self.assertFalse(report.can_auto_resume)

    def test_interrupted_cancellation_can_be_finalized(self) -> None:
        _, _, _, run = self._create_run()
        turn = self.repository.create_model_turn(run_id=run.id)
        self.repository.update_run_status(run.id, RunStatus.CANCELLING)
        self.repository.update_model_turn_status(turn.id, ModelTurnStatus.STREAMING)

        report = self.service.classify_crash_recovery(run.id)
        self.assertEqual(CrashRecoveryClassification.RECOVERABLE, report.classification)
        self.assertEqual(
            CrashRecoveryAction.FINALIZE_CANCELLATION,
            report.action,
        )
        self.assertFalse(report.can_auto_resume)

    def test_event_and_persisted_state_conflict_produces_warning(self) -> None:
        _, _, _, run = self._create_run()
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        replay = self.service.replay_run(run.id)

        self.assertEqual(RunStatus.RUNNING, replay.derived_status)
        self.assertEqual(RunStatus.CREATED, replay.record.status)
        self.assertEqual(1, len(replay.warnings))
        self.assertIn("persisted status is authoritative", replay.warnings[0])

    def test_detects_interrupted_runs(self) -> None:
        _, _, _, run = self._create_run()
        interrupted = self.service.audit_interrupted_runs()
        self.assertEqual((run.id,), tuple(item.record.id for item in interrupted))
        self.assertEqual(
            CrashRecoveryClassification.RECOVERABLE,
            interrupted[0].classification,
        )

    def test_journal_sequence_corruption_is_non_recoverable(self) -> None:
        _, _, _, run = self._create_run()
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_status_changed",
            payload={"status": "waiting_approval"},
        )
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM v2_runtime_events WHERE run_id = ? AND event_seq = 1",
                (run.id,),
            )

        report = self.service.classify_crash_recovery(run.id)
        self.assertEqual(
            CrashRecoveryClassification.NON_RECOVERABLE,
            report.classification,
        )
        self.assertEqual(CrashRecoveryAction.MARK_FAILED, report.action)
        self.assertIsNone(report.replay)
        self.assertIn(
            CrashRecoveryReason.JOURNAL_SEQUENCE_CORRUPT,
            tuple(finding.reason for finding in report.findings),
        )

    def test_rejects_event_sequence_gap(self) -> None:
        _, _, _, run = self._create_run()
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_started",
            payload={},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_status_changed",
            payload={"status": "waiting_approval"},
        )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type="run_cancelled",
            payload={},
        )
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM v2_runtime_events WHERE run_id = ? AND event_seq = 2",
                (run.id,),
            )

        with self.assertRaises(ConflictError):
            self.service.replay_run(run.id)
