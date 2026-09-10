"""D1 补评估维度：引用正确性 / 记忆质量 / 反思质量（确定性指标）。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Optional

from endless_task.eval.evaluators import (
    CitationCorrectnessEvaluator,
    MemoryQualityEvaluator,
    MemoryWriteFact,
    ReflectionFact,
    ReflectionQualityEvaluator,
    RunEvaluationContext,
)
from endless_task.eval.models import EvalSeverity
from endless_task.runtime_v2 import RunStatus
from endless_task.runtime_v2.domain import RunRecord


@dataclass
class _TurnRecord:
    id: str


@dataclass
class _Turn:
    """与 ModelTurnReplayResult 同形：record.id + partial_content。"""

    id: str
    partial_content: str = ""

    @property
    def record(self) -> _TurnRecord:
        return _TurnRecord(id=self.id)


@dataclass
class _Replay:
    model_turns: tuple = ()


def _context(
    *,
    turns=(),
    citations=None,
    writes=(),
    reflections=(),
) -> RunEvaluationContext:
    return RunEvaluationContext(
        run=RunRecord(
            id="run_1",
            conversation_id="conv_1",
            lane_id="lane_1",
            trigger_entry_id="entry_1",
            sibling_group_id="sib_1",
            status=RunStatus.COMPLETED,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        replay=_Replay(model_turns=tuple(turns)),
        citation_labels_by_turn=citations or {},
        memory_writes=tuple(writes),
        reflections=tuple(reflections),
    )


def _metric(context, evaluator, key):
    return next(
        metric
        for metric in evaluator.evaluate(context)
        if metric.key == key
    )


class CitationCorrectnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = CitationCorrectnessEvaluator()

    def test_all_markers_resolve(self) -> None:
        context = _context(
            turns=(_Turn("turn_1", "结论见 [K1] 与 [K2]。"),),
            citations={"turn_1": frozenset({"K1", "K2"})},
        )
        metric = _metric(context, self.evaluator, "citation_correctness")
        self.assertTrue(metric.value)
        self.assertEqual(EvalSeverity.INFO, metric.severity)
        self.assertIn("markers=2", metric.notes)

    def test_unresolved_marker_is_blocker(self) -> None:
        context = _context(
            turns=(_Turn("turn_1", "结论见 [K1] 与 [K9]。"),),
            citations={"turn_1": frozenset({"K1"})},
        )
        metric = _metric(context, self.evaluator, "citation_correctness")
        self.assertFalse(metric.value)
        self.assertEqual(EvalSeverity.BLOCKER, metric.severity)
        count = _metric(context, self.evaluator, "citation_unresolved_count")
        self.assertEqual(1, count.value)

    def test_marker_without_any_injection_is_unresolved(self) -> None:
        context = _context(turns=(_Turn("turn_1", "见 [K1]。"),))
        metric = _metric(context, self.evaluator, "citation_correctness")
        self.assertFalse(metric.value)

    def test_no_markers_is_pass(self) -> None:
        context = _context(turns=(_Turn("turn_1", "没有引用。"),))
        metric = _metric(context, self.evaluator, "citation_correctness")
        self.assertTrue(metric.value)
        self.assertIn("markers=0", metric.notes)

    def test_labels_are_per_turn(self) -> None:
        # 第二轮引用了第一轮注入的编号 → 仍未命中本轮注入。
        context = _context(
            turns=(
                _Turn("turn_1", "第一轮 [K1]。"),
                _Turn("turn_2", "第二轮 [K1]。"),
            ),
            citations={"turn_1": frozenset({"K1"}), "turn_2": frozenset({"K2"})},
        )
        metric = _metric(context, self.evaluator, "citation_correctness")
        self.assertFalse(metric.value)
        self.assertEqual(
            1, _metric(context, self.evaluator, "citation_unresolved_count").value
        )


class MemoryQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = MemoryQualityEvaluator()

    def test_sourced_active_writes_pass(self) -> None:
        context = _context(
            writes=(
                MemoryWriteFact("mem_1", has_source=True, status="active"),
                MemoryWriteFact("mem_2", has_source=True, status="active"),
            )
        )
        metric = _metric(context, self.evaluator, "memory_quality")
        self.assertTrue(metric.value)
        self.assertEqual(
            2, _metric(context, self.evaluator, "memory_write_count").value
        )

    def test_unsourced_write_is_warning(self) -> None:
        context = _context(
            writes=(MemoryWriteFact("mem_1", has_source=False),)
        )
        metric = _metric(context, self.evaluator, "memory_quality")
        self.assertFalse(metric.value)
        self.assertEqual(EvalSeverity.WARNING, metric.severity)
        self.assertIn("unsourced_or_inactive=1", metric.notes)

    def test_inactive_write_is_warning(self) -> None:
        context = _context(
            writes=(MemoryWriteFact("mem_1", has_source=True, status="expired"),)
        )
        self.assertFalse(_metric(context, self.evaluator, "memory_quality").value)

    def test_no_writes_passes_with_zero_count(self) -> None:
        context = _context()
        self.assertTrue(
            _metric(context, self.evaluator, "memory_quality").value
        )
        self.assertEqual(
            0, _metric(context, self.evaluator, "memory_write_count").value
        )


class ReflectionQualityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.evaluator = ReflectionQualityEvaluator()

    def test_sourced_insights_pass(self) -> None:
        context = _context(
            reflections=(
                ReflectionFact("mref_1", has_sources=True, insight_length=40),
            )
        )
        metric = _metric(context, self.evaluator, "reflection_quality")
        self.assertTrue(metric.value)
        self.assertEqual(
            1, _metric(context, self.evaluator, "reflection_count").value
        )

    def test_unsourced_insight_is_warning(self) -> None:
        context = _context(
            reflections=(
                ReflectionFact("mref_1", has_sources=False, insight_length=40),
            )
        )
        metric = _metric(context, self.evaluator, "reflection_quality")
        self.assertFalse(metric.value)
        self.assertEqual(EvalSeverity.WARNING, metric.severity)

    def test_thin_insight_is_warning(self) -> None:
        context = _context(
            reflections=(
                ReflectionFact("mref_1", has_sources=True, insight_length=3),
            )
        )
        self.assertFalse(
            _metric(context, self.evaluator, "reflection_quality").value
        )


class EvaluationServiceDimensionTest(unittest.TestCase):
    """服务层：把仓库里的事实喂进新指标（端到端可回归）。"""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from endless_task.domain.models import RetrievalEventKind
        from endless_task.eval.service import EvaluationService
        from endless_task.runtime_v2 import Actor, TranscriptEntryType
        from endless_task.storage import (
            Database,
            SqliteMemoryReflectionRepository,
            SqliteRetrievalEventRepository,
            SqliteRuntimeV2MemoryRepository,
            SqliteRuntimeV2Repository,
        )

        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "d1.db")
        self.database.initialize()
        self.runtime = SqliteRuntimeV2Repository(self.database)
        self.retrieval = SqliteRetrievalEventRepository(self.database)
        self.memories = SqliteRuntimeV2MemoryRepository(self.database)
        self.reflections = SqliteMemoryReflectionRepository(self.database)
        from endless_task.storage import SqliteChatRepository

        conversation_id = SqliteChatRepository(self.database).create_conversation().id
        lane = self.runtime.create_lane(conversation_id=conversation_id)
        trigger = self.runtime.append_entry(
            conversation_id=conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "任务"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.run = self.runtime.create_run(
            conversation_id=conversation_id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        self.service = EvaluationService(
            self.database,
            retrieval_event_repository=self.retrieval,
            memory_repository=self.memories,
            reflection_repository=self.reflections,
        )
        self._RetrievalEventKind = RetrievalEventKind

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _metrics(self):
        result = self.service.evaluate_run(self.run)
        return {metric.key: metric for metric in result.score_card.metrics}

    def _inject(self, *, turn_id: str, labels: tuple[str, ...]) -> None:
        self.retrieval.record(
            self._RetrievalEventKind.INJECTION,
            "查询",
            conversation_id=self.run.conversation_id,
            turn_id=turn_id,
            detail={"citations": [{"label": label} for label in labels]},
        )

    def _say(self, turn_id: str, text: str) -> None:
        self.runtime.append_runtime_event(
            run_id=self.run.id,
            event_type="model_text_delta",
            payload={"delta": text},
            model_turn_id=turn_id,
        )

    def test_citation_metric_uses_injected_labels(self) -> None:
        """生产契约：v2 注入按 run id 记账（app.py 传 turn_id=run_id）。"""
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._inject(turn_id=self.run.id, labels=("K1",))
        labels = self.service._citation_labels(self.run)
        self.assertEqual({"K1"}, set(labels[turn.id]))
        # 无回答内容 → 没有标记，引用正确性为通过。
        metrics = self._metrics()
        self.assertTrue(metrics["citation_correctness"].value)

    def test_run_scoped_injection_resolves_turn_markers(self) -> None:
        """回归 HV-1：真实注入的 [K#] 不能被判成杜撰。"""
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._inject(turn_id=self.run.id, labels=("K1", "K2", "K3"))
        self._say(turn.id, "结论见 [K2]。")
        metrics = self._metrics()
        self.assertTrue(metrics["citation_correctness"].value)
        self.assertEqual(EvalSeverity.INFO, metrics["citation_correctness"].severity)
        self.assertIn("markers=1, unresolved=0", metrics["citation_correctness"].notes)
        self.assertEqual(0, metrics["citation_unresolved_count"].value)

    def test_fabricated_marker_outside_injected_set_is_blocker(self) -> None:
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._inject(turn_id=self.run.id, labels=("K1",))
        self._say(turn.id, "结论见 [K1] 与 [K9]。")
        metrics = self._metrics()
        self.assertFalse(metrics["citation_correctness"].value)
        self.assertEqual(EvalSeverity.BLOCKER, metrics["citation_correctness"].severity)
        self.assertEqual(1, metrics["citation_unresolved_count"].value)

    def test_marker_without_any_injection_is_blocker(self) -> None:
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._say(turn.id, "结论见 [K1]。")
        metrics = self._metrics()
        self.assertFalse(metrics["citation_correctness"].value)
        self.assertEqual(1, metrics["citation_unresolved_count"].value)

    def test_labels_from_other_runs_do_not_resolve(self) -> None:
        """同一会话里别的 run 注入的编号仍然算误标（指向上文其他轮）。"""
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._inject(turn_id=self.run.id, labels=("K1",))
        self._inject(turn_id="run_other", labels=("K9",))
        self._say(turn.id, "结论见 [K1] 与 [K9]。")
        metrics = self._metrics()
        self.assertFalse(metrics["citation_correctness"].value)
        self.assertEqual(1, metrics["citation_unresolved_count"].value)

    def test_turn_scoped_entry_wins_over_run_scoped_inheritance(self) -> None:
        """若某 turn 自己就有记录，不被 run 级标签覆盖。"""
        turn = self.runtime.start_model_turn(run_id=self.run.id)
        self._inject(turn_id=self.run.id, labels=("K1",))
        self._inject(turn_id=turn.id, labels=("K7",))
        labels = self.service._citation_labels(self.run)
        self.assertEqual({"K7"}, set(labels[turn.id]))

    def test_memory_writes_are_measured(self) -> None:
        from endless_task.runtime_v2 import MemoryScope

        self.memories.create_memory(
            scope=MemoryScope.RUN_SCRATCH,
            kind="fact",
            content="运行期记忆",
            conversation_id=self.run.conversation_id,
            lane_id=self.run.lane_id,
            run_id=self.run.id,
            source_entry_id=self.run.trigger_entry_id,
        )
        metrics = self._metrics()
        self.assertEqual(1, metrics["memory_write_count"].value)
        self.assertTrue(metrics["memory_quality"].value)

    def test_reflections_are_measured(self) -> None:
        self.reflections.create(
            conversation_id=self.run.conversation_id,
            run_id=self.run.id,
            trigger="tool_failure",
            signature="sig_1",
            insight_content="下次先确认路径再调用工具。",
            proposal_id="mprop_1",
            source_refs=({"runId": self.run.id},),
        )
        metrics = self._metrics()
        self.assertEqual(1, metrics["reflection_count"].value)
        self.assertTrue(metrics["reflection_quality"].value)

    def test_missing_repositories_report_empty_facts(self) -> None:
        from endless_task.eval.service import EvaluationService

        bare = EvaluationService(self.database)
        metrics = {
            metric.key: metric
            for metric in bare.evaluate_run(self.run).score_card.metrics
        }
        self.assertEqual(0, metrics["memory_write_count"].value)
        self.assertEqual(0, metrics["reflection_count"].value)
        self.assertTrue(metrics["citation_correctness"].value)


if __name__ == "__main__":
    unittest.main()
