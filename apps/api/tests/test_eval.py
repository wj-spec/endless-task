from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional, Sequence

from endless_task.runtime_v2 import (
    Actor,
    ModelTurnStatus,
    RunStatus,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.eval import (
    EvaluationService,
    EvalHarvestSpec,
    EvalSeverity,
    EvalVerdict,
    aggregate,
    diff,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)


class EvalPhaseATest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "eval.db"
        self.database = Database(self.database_path)
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.service = EvaluationService(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_run(
        self,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        tools: Sequence[tuple[str, dict, ToolExecutionStatus]] = (),
        assistant: bool = True,
    ):
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
        turn = self.repository.create_model_turn(
            run_id=run.id,
            provider="fake",
            model="fake-model",
        )
        self.repository.update_run_status(run.id, status)
        for index, (tool_name, arguments, tool_status) in enumerate(tools):
            tool = self.repository.create_tool_execution(
                model_turn_id=turn.id,
                call_id=f"call_{index}",
                tool_name=tool_name,
                arguments=arguments,
            )
            self.repository.update_tool_execution_status(tool.id, tool_status)
            self.repository.append_runtime_event(
                run_id=run.id,
                model_turn_id=turn.id,
                event_type="tool_execution_completed",
                payload={"toolExecutionId": tool.id},
            )
        # Finalize the model turn only after its tool executions exist.
        self.repository.update_model_turn_status(
            turn.id,
            ModelTurnStatus.COMPLETED,
            input_tokens=10,
            output_tokens=6,
        )

        assistant_entry = None
        if assistant:
            assistant_entry = self.repository.append_entry(
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
                assistant_entry_id=assistant_entry.id,
                is_active_variant=True,
            )
        self.repository.append_runtime_event(
            run_id=run.id,
            event_type=f"run_{status.value}" if status is RunStatus.COMPLETED else "run_completed",
            payload={},
        )
        self.repository.set_conversation_pointer(
            conversation_id=conversation_id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        # Return the run re-read from storage: the create_run object carries the
        # pre-mutation status (created), which would mask the completed outcome.
        return conversation_id, lane, trigger, self.repository.get_run(run.id), turn

    def test_completed_read_only_run_passes(self) -> None:
        _, _, _, run, _ = self._seed_run(
            tools=[("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED)]
        )
        evaluation = self.service.evaluate_run(run)
        self.assertEqual(EvalVerdict.PASS, evaluation.verdict)
        self.assertTrue(evaluation.score_card.metric("completion").value)
        self.assertTrue(evaluation.score_card.metric("approval_gate").value)
        self.assertFalse(evaluation.score_card.has_blocker)

    def test_failed_tool_is_warning_not_blocker(self) -> None:
        _, _, _, run, _ = self._seed_run(
            tools=[("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.FAILED)]
        )
        evaluation = self.service.evaluate_run(run)
        self.assertEqual(EvalVerdict.WARN, evaluation.verdict)
        self.assertEqual(
            EvalSeverity.WARNING,
            evaluation.score_card.metric("tool_correctness").severity,
        )
        self.assertFalse(evaluation.score_card.has_blocker)

    def test_write_tool_without_approval_not_blocker_auto(self) -> None:
        # 与 pi 对齐：写入已绑定工作区自动执行，无需逐次确认，因此不作为 approval_gate blocker。
        _, _, _, run, _ = self._seed_run(
            tools=[("write_workspace_file", {"path": "b.txt"}, ToolExecutionStatus.COMPLETED)]
        )
        evaluation = self.service.evaluate_run(run)
        self.assertEqual(EvalVerdict.PASS, evaluation.verdict)
        self.assertEqual(
            EvalSeverity.INFO,
            evaluation.score_card.metric("approval_gate").severity,
        )
        self.assertFalse(evaluation.score_card.has_blocker)

    def test_external_action_without_approval_is_blocker(self) -> None:
        # 越出工作区的破坏性/外部动作（删除）仍未确认执行 → approval_gate BLOCKER。
        _, _, _, run, _ = self._seed_run(
            tools=[("delete_workspace_file", {"path": "b.txt"}, ToolExecutionStatus.COMPLETED)]
        )
        evaluation = self.service.evaluate_run(run)
        self.assertEqual(EvalVerdict.FAIL, evaluation.verdict)
        self.assertEqual(
            EvalSeverity.BLOCKER,
            evaluation.score_card.metric("approval_gate").severity,
        )
        self.assertTrue(evaluation.score_card.has_blocker)

    def test_duplicate_tool_signature_detected_as_loop(self) -> None:
        _, _, _, run, _ = self._seed_run(
            tools=[
                ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED),
                ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED),
            ]
        )
        evaluation = self.service.evaluate_run(run)
        self.assertEqual(EvalVerdict.FAIL, evaluation.verdict)
        self.assertEqual(
            EvalSeverity.BLOCKER,
            evaluation.score_card.metric("loop_detected").severity,
        )
        self.assertTrue(evaluation.score_card.metric("loop_detected").value)

    def test_harvest_filters_by_require_tools(self) -> None:
        self._seed_run(assistant=False, tools=[
            ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED)
        ])
        self._seed_run(assistant=False, tools=[])

        with_tools = self._run_harvest(EvalHarvestSpec(require_tools=True))
        self.assertEqual(1, len(with_tools))

        all_runs = self._run_harvest(EvalHarvestSpec(require_tools=False))
        self.assertEqual(2, len(all_runs))

    def _run_harvest(self, spec: EvalHarvestSpec):
        from endless_task.eval.harvest import harvest_runs

        return harvest_runs(self.repository, spec)

    def test_evaluate_batch_persists_and_aggregates(self) -> None:
        self._seed_run(tools=[
            ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED)
        ])
        batch_id, aggregation, results = self.service.evaluate_batch(
            EvalHarvestSpec(require_tools=True),
            mode="deterministic",
        )
        self.assertNotEqual("", batch_id)
        self.assertEqual(1, aggregation.run_count)
        self.assertEqual(1, aggregation.pass_count)
        self.assertEqual(1, len(results))

        from endless_task.eval.storage import SqliteEvalRepository

        repository = SqliteEvalRepository(self.database)
        loaded = repository.load_run_results(batch_id)
        self.assertEqual(batch_id, repository.get_batch(batch_id).id)
        self.assertEqual("complete", repository.get_batch(batch_id).status)
        self.assertEqual(1, len(loaded))

    def test_diff_flags_blocking_approval_regression(self) -> None:
        self._seed_run(assistant=False, tools=[
            ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED)
        ])
        _, baseline_agg, _ = self.service.evaluate_batch(
            EvalHarvestSpec(require_tools=True),
        )
        # Second batch uses an ungated external-action (delete) tool -> approval_gate regression.
        self._seed_run(assistant=False, tools=[
            ("delete_workspace_file", {"path": "b.txt"}, ToolExecutionStatus.COMPLETED)
        ])
        _, candidate_agg, _ = self.service.evaluate_batch(
            EvalHarvestSpec(require_tools=True),
        )

        result = diff(baseline_agg, candidate_agg)
        self.assertTrue(result.has_blocking_regression)
        self.assertEqual(1, result.exit_code)
        regression_keys = {d.key for d in result.blocking_regressions}
        self.assertIn("approval_gate", regression_keys)

    def test_aggregation_rolls_up_flags(self) -> None:
        self._seed_run(tools=[
            ("read_workspace_file", {"path": "a.txt"}, ToolExecutionStatus.COMPLETED)
        ])
        evaluation = self.service.evaluate_run(
            self.repository.list_runs(statuses=(RunStatus.COMPLETED,))[0]
        )
        aggregation = aggregate([evaluation])
        self.assertEqual(1, aggregation.pass_count)
        self.assertEqual(1, aggregation.metrics["completion"].pass_rate)
