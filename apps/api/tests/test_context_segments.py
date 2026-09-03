"""M2 AP-206 Stage A: entries -> context segments shadow mapping."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.context_engine import ContextSegmentKind
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import Actor, TranscriptEntryStatus, TranscriptEntryType
from endless_task.runtime_v2.context_segments import build_context_shadow
from endless_task.runtime_v2.domain import TranscriptEntryRecord


def entry(
    entry_id: str,
    entry_type: TranscriptEntryType,
    *,
    content: str = "text",
    include: bool = True,
    seq: int = 0,
) -> TranscriptEntryRecord:
    return TranscriptEntryRecord(
        id=entry_id,
        conversation_id="conversation_1",
        parent_id=None,
        lane_id="lane_1",
        seq=seq,
        type=entry_type,
        type_version=1,
        actor=Actor.USER if entry_type is TranscriptEntryType.USER_MESSAGE else Actor.ASSISTANT,
        status=TranscriptEntryStatus.FINAL,
        created_at="2026-09-03T00:00:00Z",
        updated_at="2026-09-03T00:00:00Z",
        payload={"content": content},
        context_policy={"include_in_llm": include, "transform": "full"},
        display={},
    )


class ContextSegmentsMapperTest(unittest.TestCase):
    def test_maps_user_last_to_current_user_and_history(self) -> None:
        entries = (
            entry("user_1", TranscriptEntryType.USER_MESSAGE, seq=1),
            entry("tool_1", TranscriptEntryType.TOOL_RESULT, seq=2, content="out"),
            entry("user_2", TranscriptEntryType.USER_MESSAGE, seq=3),
        )
        report = build_context_shadow(entries)
        self.assertEqual(3, report.included_count)
        kinds = {segment.kind for segment in report.segments}
        self.assertIn(ContextSegmentKind.CURRENT_USER, kinds)
        self.assertIn(ContextSegmentKind.RECENT_HISTORY, kinds)
        self.assertIn(ContextSegmentKind.TOOL_RESULT, kinds)
        self.assertGreater(report.estimated_tokens, 0)

    def test_kind_specific_entries_map(self) -> None:
        entries = (
            entry("sys_1", TranscriptEntryType.SYSTEM_NOTICE),
            entry("plan_1", TranscriptEntryType.PLAN),
            entry("sum_1", TranscriptEntryType.CONTEXT_SUMMARY),
            entry("art_1", TranscriptEntryType.ARTIFACT_REF),
        )
        kinds = {segment.kind for segment in build_context_shadow(entries).segments}
        self.assertIn(ContextSegmentKind.SYSTEM, kinds)
        self.assertIn(ContextSegmentKind.OPTIONAL_INSTRUCTION, kinds)
        self.assertIn(ContextSegmentKind.CHECKPOINT, kinds)
        self.assertIn(ContextSegmentKind.ARTIFACT, kinds)

    def test_excluded_by_policy_are_skipped(self) -> None:
        entries = (
            entry("hide_1", TranscriptEntryType.TOOL_RESULT, include=False),
            entry("show_1", TranscriptEntryType.USER_MESSAGE),
        )
        report = build_context_shadow(entries)
        self.assertEqual(2, report.entry_count)
        self.assertEqual(1, report.included_count)
        self.assertEqual(("show_1",), report.segments[0].source_ids)

    def test_source_ids_preserve_entry_provenance(self) -> None:
        report = build_context_shadow((entry("e1", TranscriptEntryType.USER_MESSAGE),))
        self.assertEqual(("e1",), report.segments[0].source_ids)


class ShadowObserverIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository

        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_executor_reports_context_shadow(self) -> None:
        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime_v2 import AgentRunExecutor, RunStatus
        from endless_task.tooling import ToolRegistry

        from tests.test_runtime_v2_execution import ScriptedProvider

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
            [(ProviderTextDelta("好的。"), ProviderCompleted(finish_reason="stop"))]
        )
        reports = []
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            context_shadow_observer=reports.append,
            context_window_tokens=32768,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(reports))
        self.assertGreaterEqual(reports[0].included_count, 1)
        self.assertGreater(reports[0].estimated_tokens, 0)
        self.assertTrue(reports[0].has_plan)
        self.assertEqual(64, len(reports[0].fingerprint or ""))


if __name__ == "__main__":
    unittest.main()


class PlanShadowUnitTest(unittest.TestCase):
    def test_plan_shadow_without_budget_reports_estimate_only(self) -> None:
        from endless_task.runtime_v2.context_segments import build_plan_shadow

        report = build_plan_shadow(
            (entry("u1", TranscriptEntryType.USER_MESSAGE, seq=1),)
        )
        self.assertFalse(report.has_plan)
        self.assertGreater(report.estimated_tokens, 0)

    def test_plan_shadow_with_budget_runs_retention_and_planning(self) -> None:
        from endless_task.context_engine import ContextBudget
        from endless_task.runtime_v2.context_segments import build_plan_shadow

        entries = (
            entry("user_1", TranscriptEntryType.USER_MESSAGE, seq=1, content="hi"),
            entry("tool_1", TranscriptEntryType.TOOL_RESULT, seq=2, content="x" * 200),
            entry("tool_2", TranscriptEntryType.TOOL_RESULT, seq=3, content="y" * 200),
            entry("tool_3", TranscriptEntryType.TOOL_RESULT, seq=4, content="z" * 200),
        )
        budget = ContextBudget(
            window_tokens=100,
            reserved_output_tokens=10,
            safety_margin_tokens=0,
        )
        report = build_plan_shadow(entries, budget=budget, max_kept_results=1)
        self.assertTrue(report.has_plan)
        self.assertEqual(64, len(report.fingerprint or ""))
        # with max_kept_results=1 only the newest of three tool results survives
        self.assertEqual(2, report.pruned_count)
        self.assertIn(ContextSegmentKind.CURRENT_USER, {s.kind for s in report.segments})

    def test_oversized_tool_result_spills(self) -> None:
        from endless_task.context_engine import ContextBudget
        from endless_task.runtime_v2.context_segments import build_plan_shadow

        entries = (
            entry("user_1", TranscriptEntryType.USER_MESSAGE, seq=1, content="hi"),
            entry("tool_1", TranscriptEntryType.TOOL_RESULT, seq=2, content="z" * 400),
        )
        budget = ContextBudget(
            window_tokens=100,
            reserved_output_tokens=10,
            safety_margin_tokens=0,
        )
        report = build_plan_shadow(entries, budget=budget, max_kept_results=5)
        self.assertEqual(1, report.spilled_count)
        self.assertIn("tool_1", report.excluded_source_ids)
        self.assertFalse(any(s.kind is ContextSegmentKind.TOOL_RESULT for s in report.segments))


class StageCCutoverIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Flag-on (context_engine_v2_enabled) filters entries by the v2 plan."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository

        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def _run(
        self, *, v2_enabled: bool, window: int, content: str, max_output: int = 8192
    ):
        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime_v2 import AgentRunExecutor, RunStatus
        from endless_task.tooling import ToolRegistry

        from tests.test_runtime_v2_execution import FakeTool, ScriptedProvider

        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "请读取并继续。"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        tool_calls = [
            ProviderToolCall(
                id=f"call_{index}",
                name="read_file",
                arguments={"path": f"{index}.txt"},
            )
            for index in range(1)
        ]
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("读取。"),
                    *tuple(tool_calls),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool(content=content)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=max_output,
            context_engine_v2_enabled=v2_enabled,
            context_window_tokens=window,
        )
        result = await executor.execute(
            run.id,
            cancellation_token=CancellationToken(),
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        return provider, tool

    async def test_under_budget_flag_on_matches_flag_off(self) -> None:
        provider_off, _ = await self._run(
            v2_enabled=False, window=32768, content="small content"
        )
        provider_on, _ = await self._run(
            v2_enabled=True, window=32768, content="small content"
        )
        self.assertEqual(
            [m.content for m in provider_off.requests[-1].messages],
            [m.content for m in provider_on.requests[-1].messages],
        )

    async def test_current_run_tool_feedback_is_kept_legacy_when_enabled(self) -> None:
        # Stage C only applies the v2 plan to the INITIAL context (history);
        # the current run's tool feedback is an active paired result and must
        # keep reaching the model (active-pair rule), even when oversized.
        provider_on, _ = await self._run(
            v2_enabled=True, window=200, content="x" * 3000, max_output=40
        )
        last = provider_on.requests[-1].messages
        tool_messages = [m for m in last if getattr(m, "role", None) == "tool"]
        self.assertEqual(1, len(tool_messages))
        user_text = "".join(
            m.content for m in last if getattr(m, "role", None) == "user"
        )
        self.assertIn("请读取并继续", user_text)
