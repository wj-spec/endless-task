"""B4 反思服务的集成：证据收集 → 提案 → 确认/拒绝。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import MemoryKind
from endless_task.memory import MemoryReflectionService
from endless_task.runtime_v2 import RunStatus
from endless_task.storage import (
    Database,
    SqliteMemoryProposalRepository,
    SqliteMemoryReflectionRepository,
    SqliteMemoryRepository,
    SqliteRuntimeV2Repository,
)


class _ReflectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "b4.db")
        self.database.initialize()
        self.runtime = SqliteRuntimeV2Repository(self.database)
        self.memories = SqliteMemoryRepository(self.database)
        self.proposals = SqliteMemoryProposalRepository(self.database)
        self.reflections = SqliteMemoryReflectionRepository(self.database)
        self.hub_events: list[tuple[str, int]] = []
        self.service = MemoryReflectionService(
            runtime_repository=self.runtime,
            memory_repository=self.memories,
            proposal_repository=self.proposals,
            reflection_repository=self.reflections,
            hub_event_sink=lambda conversation_id, count: self.hub_events.append(
                (conversation_id, count)
            ),
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _conversation_with_run(self):
        from endless_task.runtime_v2 import Actor, TranscriptEntryType

        # 用 v2 仓储直接建会话/车道/运行（与运行时一致）。
        chat_conversation = self._chat_conversation()
        lane = self.runtime.create_lane(conversation_id=chat_conversation)
        trigger = self.runtime.append_entry(
            conversation_id=chat_conversation,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "任务"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.runtime.create_run(
            conversation_id=chat_conversation,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return chat_conversation, run

    def _chat_conversation(self) -> str:
        from endless_task.storage import SqliteChatRepository

        return SqliteChatRepository(self.database).create_conversation().id

    def _fail_tool(self, run_id: str, *, tool_name: str, error_code: str) -> None:
        turn = self.runtime.start_model_turn(run_id=run_id)
        record, _ = self.runtime.record_tool_call(
            model_turn_id=turn.id,
            call_id=f"call_{tool_name}_{error_code}",
            tool_name=tool_name,
            arguments={"path": "a.txt"},
        )
        self.runtime.append_runtime_event(
            run_id=run_id,
            model_turn_id=turn.id,
            event_type="tool_execution_failed",
            payload={
                "toolExecutionId": record.id,
                "toolName": tool_name,
                "errorCode": error_code,
            },
        )

    def _escalate(self, run_id: str, reason: str, summary: str = "") -> None:
        self.runtime.append_runtime_event(
            run_id=run_id,
            event_type="run_awaiting_user",
            payload={
                "reason": reason,
                "summary": summary,
                "options": ["continue", "change_approach", "take_over"],
            },
        )


class ReflectRunTest(_ReflectionTestCase):
    def test_completed_run_without_signals_does_not_reflect(self) -> None:
        _, run = self._conversation_with_run()
        report = self.service.reflect_run(run.id)
        self.assertFalse(report.reflected)
        self.assertEqual((), report.created)

    def test_repeated_failure_creates_proposal_and_trace(self) -> None:
        conversation_id, run = self._conversation_with_run()
        self._fail_tool(run.id, tool_name="write_workspace_file", error_code="permission_denied")
        self._fail_tool(run.id, tool_name="write_workspace_file", error_code="permission_denied")
        report = self.service.reflect_run(run.id)
        self.assertTrue(report.reflected)
        self.assertEqual(1, len(report.created))
        candidate = report.created[0]
        self.assertIn("permission_denied", candidate.proposal.content)
        self.assertIn("反思", candidate.proposal.reason)
        self.assertEqual(MemoryKind.FACT, candidate.proposal.kind)
        record = self.reflections.find_by_proposal(candidate.proposal.id)
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual("tool_failure", record.trigger)
        self.assertEqual(run.id, record.run_id)
        self.assertTrue(record.source_refs)
        self.assertEqual([(conversation_id, 1)], self.hub_events)

    def test_same_lesson_is_not_proposed_twice(self) -> None:
        _, run = self._conversation_with_run()
        self._fail_tool(run.id, tool_name="run_shell", error_code="tool_timeout")
        self._fail_tool(run.id, tool_name="run_shell", error_code="tool_timeout")
        self.assertEqual(1, len(self.service.reflect_run(run.id).created))
        self.assertEqual(0, len(self.service.reflect_run(run.id).created))
        self.assertEqual(1, len(self.reflections.list_records()))

    def test_escalation_reason_becomes_insight(self) -> None:
        _, run = self._conversation_with_run()
        self._escalate(run.id, "no_progress", "连续 4 轮没有实质进展。")
        report = self.service.reflect_run(run.id)
        self.assertEqual(1, len(report.created))
        self.assertIn("无进展", report.created[0].proposal.content)

    def test_max_insights_per_run_is_respected(self) -> None:
        service = MemoryReflectionService(
            runtime_repository=self.runtime,
            memory_repository=self.memories,
            proposal_repository=self.proposals,
            reflection_repository=self.reflections,
            max_insights_per_run=1,
        )
        _, run = self._conversation_with_run()
        self._escalate(run.id, "no_progress")
        self._escalate(run.id, "verification_failed", "报告未写入")
        self.assertEqual(1, len(service.reflect_run(run.id).created))


class FinalizeTest(_ReflectionTestCase):
    def test_accept_marks_insight_important_and_links(self) -> None:
        _, run = self._conversation_with_run()
        self._escalate(run.id, "no_progress")
        candidate = self.service.reflect_run(run.id).created[0]
        _, insight = self.proposals.accept_proposal(candidate.proposal.id)
        self.assertIsNotNone(self.service.finalize(candidate.proposal.id, insight))
        stored = self.memories.get_memory(insight.id)
        self.assertGreaterEqual(stored.importance, 0.8)
        record = self.reflections.find_by_proposal(candidate.proposal.id)
        assert record is not None
        self.assertEqual("accepted", record.status)
        self.assertEqual(insight.id, record.insight_memory_id)

    def test_reject_keeps_no_memory_and_blocks_repeat(self) -> None:
        _, run = self._conversation_with_run()
        self._escalate(run.id, "no_progress")
        candidate = self.service.reflect_run(run.id).created[0]
        self.proposals.reject_proposal(candidate.proposal.id)
        self.assertIsNotNone(self.service.reject(candidate.proposal.id))
        record = self.reflections.find_by_proposal(candidate.proposal.id)
        assert record is not None
        self.assertEqual("rejected", record.status)
        self.assertEqual(0, len(self.service.reflect_run(run.id).created))

    def test_finalize_without_record_returns_none(self) -> None:
        memory = self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="普通记忆",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        self.assertIsNone(self.service.finalize("mprop_missing", memory))

    def test_failed_run_is_reflected(self) -> None:
        _, run = self._conversation_with_run()
        self.runtime.transition_run_status(
            run.id,
            RunStatus.FAILED,
            event_type="run_failed",
            error_code="provider_unavailable",
            safe_message="服务暂时不可用。",
        )
        report = self.service.reflect_run(run.id)
        self.assertTrue(report.reflected)
        self.assertEqual(1, len(report.created))
        self.assertIn("provider_unavailable", report.created[0].proposal.content)


if __name__ == "__main__":
    unittest.main()
