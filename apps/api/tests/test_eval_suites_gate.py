"""M6 OE-4: Eval suites, trajectory-connected evaluation and release gate."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from datetime import date
from pathlib import Path

from endless_task.eval.aggregator import aggregate
from endless_task.eval.gate import (
    EvalBaseline,
    Waiver,
    gate_to_json,
    gate_to_markdown,
    run_gate,
)
from endless_task.eval.models import (
    Aggregation,
    EvalMetric,
    EvalSeverity,
    EvalUnit,
    RunEvaluation,
    ScoreCard,
)
from endless_task.eval.suites import (
    KNOWN_METRIC_KEYS,
    PENDING_SUITE_NAMES,
    SUITE_CATALOG,
    EvalSuite,
    suite_names_for_change,
    suites_for_change,
)
from endless_task.eval.trajectory_eval import evaluate_trajectories, evaluate_trajectory
from endless_task.runtime_ledger.trajectory import TrajectoryBundle, TrajectoryManifest
from endless_task.runtime_v2.domain import RunStatus


def manifest(run_id: str = "run_1") -> TrajectoryManifest:
    return TrajectoryManifest(
        schema_version=1,
        runtime_version="0.14.0",
        provider="fake",
        model="fake-model",
        config_fingerprint="cfg-1",
        redaction_policy_revision="r1",
        source_run_id=run_id,
    )


def bundle(
    *,
    run_id: str = "run_1",
    events: list[dict],
    tool_outcomes: list[dict] | None = None,
    usages: list[dict] | None = None,
) -> TrajectoryBundle:
    return TrajectoryBundle(
        manifest=manifest(run_id),
        events=tuple(events),
        tool_outcomes=tuple(tool_outcomes or ()),
        usages=tuple(usages or ()),
    )


def event(event_id: str, event_type: str, occurred_at: str, data: dict | None = None) -> dict:
    return {
        "eventId": event_id,
        "eventType": event_type,
        "occurredAt": occurred_at,
        "traceId": "trace_1",
        "runId": "run_1",
        "safetyCritical": False,
        "data": data or {},
    }


def effect_event(event_id: str, effect_id: str, outcome: str, at: str) -> dict:
    return event(
        event_id,
        "effect_receipt",
        at,
        {
            "effect_id": effect_id,
            "tool_call_id": f"call_{effect_id}",
            "outcome": outcome,
            "effect_type": "file_write",
        },
    )


class SuiteCatalogTest(unittest.TestCase):
    def test_all_suite_keys_known(self) -> None:
        for suite in SUITE_CATALOG.values():
            self.assertTrue(
                set(suite.metric_keys) <= KNOWN_METRIC_KEYS,
                suite.name,
            )

    def test_unknown_suite_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EvalSuite(
                name="bad",
                description="x",
                metric_keys=("completion", "no_such_metric"),
            )

    def test_change_gates_reference_registered_suites(self) -> None:
        # tool_platform -> core_loop + schema_and_tool_use + safety (08 §10.2)
        self.assertEqual(
            ("core_loop", "schema_and_tool_use", "safety_and_approval"),
            suite_names_for_change("tool_platform"),
        )
        self.assertEqual(
            ("delegation", "safety_and_approval"),
            suite_names_for_change("delegation"),
        )
        self.assertEqual(
            ("skill_selection", "core_loop"),
            suite_names_for_change("skills"),
        )

    def test_unknown_change_area_rejected(self) -> None:
        with self.assertRaises(ValueError):
            suite_names_for_change("no_such_area")

    def test_pending_topics_not_registered_as_empty(self) -> None:
        # Topics without metric coverage must stay pending, not empty suites.
        for name in PENDING_SUITE_NAMES:
            self.assertNotIn(name, SUITE_CATALOG)
        self.assertIn("context_compaction", PENDING_SUITE_NAMES)
        self.assertIn("sandbox_escape", PENDING_SUITE_NAMES)

    def test_suites_for_change_returns_instances(self) -> None:
        suites = suites_for_change("tool_platform")
        self.assertEqual(3, len(suites))
        self.assertTrue(all(isinstance(s, EvalSuite) for s in suites))


class TrajectoryEvaluationTest(unittest.TestCase):
    def _completed_bundle(self) -> TrajectoryBundle:
        return bundle(
            events=[
                effect_event("evt_1", "fx_1", "committed", "2026-09-05T00:00:00Z"),
                event("evt_2", "run.completed", "2026-09-05T00:00:01Z"),
            ],
            tool_outcomes=[{"toolCallId": "call_fx_1", "status": "completed"}],
            usages=[{"requestCount": 1, "costUsd": 0.012}],
        )

    def test_completed_bundle_passes(self) -> None:
        card = evaluate_trajectory(self._completed_bundle())
        self.assertEqual("run_1", card.run_id)
        self.assertTrue(card.metric("completion").value)
        self.assertTrue(card.metric("approval_gate").value)
        self.assertEqual(0, card.metric("trajectory_unknown_effect_count").value)
        self.assertFalse(card.has_blocker)
        self.assertEqual("pass", card.verdict.value)

    def test_unknown_effect_blocks(self) -> None:
        b = bundle(
            events=[
                effect_event("evt_1", "fx_1", "unknown", "2026-09-05T00:00:00Z"),
                event("evt_2", "run.completed", "2026-09-05T00:00:01Z"),
            ]
        )
        card = evaluate_trajectory(b)
        self.assertEqual(1, card.metric("trajectory_unknown_effect_count").value)
        self.assertFalse(card.metric("approval_gate").value)
        self.assertEqual(EvalSeverity.BLOCKER, card.metric("approval_gate").severity)
        self.assertTrue(card.has_blocker)

    def test_failed_run_completion_blocked(self) -> None:
        b = bundle(
            events=[event("evt_1", "run.failed", "2026-09-05T00:00:00Z", {"error": "x"})]
        )
        card = evaluate_trajectory(b)
        self.assertFalse(card.metric("completion").value)
        self.assertEqual(EvalSeverity.BLOCKER, card.metric("completion").severity)

    def test_orphan_tool_outcome_warns_robustness(self) -> None:
        b = bundle(
            events=[event("evt_1", "run.completed", "2026-09-05T00:00:00Z")],
            tool_outcomes=[{"toolCallId": "call_orphan", "status": "completed"}],
        )
        card = evaluate_trajectory(b)
        self.assertFalse(card.metric("robustness").value)
        self.assertEqual(1, card.metric("trajectory_consistency_note_count").value)
        self.assertTrue(card.warnings)

    def test_cost_and_request_rollup(self) -> None:
        card = evaluate_trajectory(self._completed_bundle())
        self.assertEqual(0.012, card.metric("trajectory_cost_usd_total").value)
        self.assertEqual(1, card.metric("trajectory_request_count").value)

    def test_tool_failure_metrics(self) -> None:
        b = bundle(
            events=[event("evt_1", "run.completed", "2026-09-05T00:00:00Z")],
            tool_outcomes=[
                {"toolCallId": "ok", "status": "completed"},
                {"toolCallId": "bad", "status": "failed"},
            ],
        )
        card = evaluate_trajectory(b)
        self.assertEqual(2, card.metric("tool_call_count").value)
        self.assertEqual(1, card.metric("tool_failure_count").value)
        self.assertFalse(card.metric("tool_correctness").value)

    def test_batch_aggregates_with_repo_run_objects(self) -> None:
        completed = self._completed_bundle()
        failed = bundle(
            events=[event("evt_1", "run.failed", "2026-09-05T00:00:00Z")]
        )
        cards = evaluate_trajectories([completed, failed])
        aggregation = aggregate(cards)
        self.assertEqual(2, aggregation.run_count)
        self.assertEqual(1, aggregation.pass_count)
        self.assertEqual(1, aggregation.fail_count)
        self.assertIn("completion", aggregation.metrics)
        # The metric is comparable in pass_rate terms.
        self.assertGreaterEqual(aggregation.metrics["completion"].pass_rate, 0.5)

    def test_aggregate_accepts_repo_run_evaluations(self) -> None:
        # Regression guard: aggregate() still accepts RunEvaluation objects.
        run_eval = RunEvaluation(
            run_id="run_x",
            run_status=RunStatus.COMPLETED,
            score_card=ScoreCard(
                run_id="run_x",
                metrics=(
                    EvalMetric(
                        key="completion",
                        value=True,
                        unit=EvalUnit.BOOL,
                        severity=EvalSeverity.INFO,
                        source="deterministic",
                    ),
                ),
            ),
        )
        aggregation = aggregate([run_eval])
        self.assertEqual(1, aggregation.run_count)
        self.assertEqual(1.0, aggregation.metrics["completion"].pass_rate)


def _score_card(run_id: str, completion: bool, tool_failures: int = 0) -> ScoreCard:
    metrics = [
        EvalMetric(
            key="completion",
            value=completion,
            unit=EvalUnit.BOOL,
            severity=EvalSeverity.INFO if completion else EvalSeverity.BLOCKER,
            source="deterministic",
        )
    ]
    if tool_failures:
        metrics.append(
            EvalMetric(
                key="tool_failure_count",
                value=tool_failures,
                unit=EvalUnit.COUNT,
                severity=EvalSeverity.WARNING,
                source="deterministic",
            )
        )
    return ScoreCard(run_id=run_id, metrics=tuple(metrics))


def _aggregation(suite_name: str, revision: str, run_ids: list[str]) -> EvalBaseline:
    cards = [_score_card(rid, True) for rid in run_ids]
    return EvalBaseline(
        suite_name=suite_name,
        revision=revision,
        aggregation=aggregate(cards),
    )


class ReleaseGateTest(unittest.TestCase):
    def test_gate_passes_when_candidate_equals_baseline(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2", "r3"])
        candidate = aggregate([_score_card("r1", True), _score_card("r2", True)])
        result = run_gate(suite_name="core_loop", baseline=baseline, candidate=candidate)
        self.assertTrue(result.passed)
        self.assertEqual("rev-1", result.baseline_revision)
        self.assertEqual((), result.blocked)

    def test_gate_blocks_completion_regression(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        candidate = aggregate(
            [_score_card("r3", True), _score_card("r4", False)]
        )
        result = run_gate(suite_name="core_loop", baseline=baseline, candidate=candidate)
        self.assertFalse(result.passed)
        keys = {item.key for item in result.blocked}
        self.assertIn("completion", keys)

    def test_active_waiver_releases_key(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        candidate = aggregate(
            [_score_card("r3", True), _score_card("r4", False)]
        )
        waiver = Waiver(
            metric_key="completion",
            reason="known provider flake, tracked",
            owner="platform-team",
            expires_at=date(2030, 1, 1),
        )
        result = run_gate(
            suite_name="core_loop",
            baseline=baseline,
            candidate=candidate,
            waivers=[waiver],
        )
        self.assertTrue(result.passed)
        self.assertEqual(("completion",), result.waived)

    def test_expired_waiver_does_not_release(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        candidate = aggregate(
            [_score_card("r3", True), _score_card("r4", False)]
        )
        waiver = Waiver(
            metric_key="completion",
            reason="expired",
            owner="platform-team",
            expires_at=date(2020, 1, 1),
        )
        result = run_gate(
            suite_name="core_loop",
            baseline=baseline,
            candidate=candidate,
            waivers=[waiver],
            on=date(2026, 9, 5),
        )
        self.assertFalse(result.passed)
        self.assertIn("completion", result.expired_waivers)
        self.assertEqual((), result.waived)

    def test_baseline_roundtrip_dict(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        restored = EvalBaseline.from_dict(baseline.to_dict())
        self.assertEqual("rev-1", restored.revision)
        self.assertEqual(2, restored.aggregation.run_count)

    def test_gate_reports_json_and_markdown(self) -> None:
        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        candidate = aggregate(
            [_score_card("r3", True), _score_card("r4", False)]
        )
        result = run_gate(suite_name="core_loop", baseline=baseline, candidate=candidate)
        payload = gate_to_json(result)
        self.assertIn('"passed": false', payload)
        self.assertIn('"suite": "core_loop"', payload)
        markdown = gate_to_markdown(result)
        self.assertIn("Eval gate: core_loop", markdown)
        self.assertIn("completion", markdown)


if __name__ == "__main__":
    unittest.main()


class WaiverParsingTest(unittest.TestCase):
    def test_parses_well_formed_waiver(self) -> None:
        from endless_task.eval.gate import waiver_from_string

        waiver = waiver_from_string("completion;known flake;platform-team;2030-01-01")
        self.assertEqual("completion", waiver.metric_key)
        self.assertEqual("known flake", waiver.reason)
        self.assertEqual("platform-team", waiver.owner)
        self.assertEqual(date(2030, 1, 1), waiver.expires_at)

    def test_rejects_malformed_waiver(self) -> None:
        from endless_task.eval.gate import waiver_from_string

        for bad in (
            "completion;reason;owner",
            ";;owner;2030-01-01",
            "completion;reason;owner;not-a-date",
        ):
            with self.assertRaises(ValueError):
                waiver_from_string(bad)

    def test_write_gate_report_lands_artifacts(self) -> None:
        import tempfile
        from pathlib import Path

        from endless_task.eval.gate import write_gate_report

        baseline = _aggregation("core_loop", "rev-1", ["r1", "r2"])
        candidate = aggregate(
            [_score_card("r3", True), _score_card("r4", False)]
        )
        result = run_gate(
            suite_name="core_loop", baseline=baseline, candidate=candidate
        )
        with tempfile.TemporaryDirectory() as directory:
            json_path, md_path = write_gate_report(
                result, Path(directory)
            )
            self.assertTrue(json_path.exists())
            self.assertTrue(md_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual("core_loop", payload["suite"])
            self.assertIn("Eval gate: core_loop", md_path.read_text(encoding="utf-8"))


class GateCliTest(unittest.TestCase):
    """eval gate CLI: candidate batch vs frozen baseline JSON artifact."""

    def setUp(self) -> None:
        import tempfile

        from endless_task.eval.storage import SqliteEvalRepository
        from endless_task.storage import Database

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "eval.db"
        )
        self.database.initialize()
        self._repository = SqliteEvalRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _make_batch(self, *, completions: list[bool]) -> str:
        batch_id = self._repository.create_batch(
            mode="deterministic", filters={}, judge_provider=None,
            judge_model=None, run_count=len(completions),
        )
        results = []
        for index, completed in enumerate(completions):
            from endless_task.runtime_v2 import RunStatus

            results.append(
                RunEvaluation(
                    run_id=f"run_{index}",
                    run_status=RunStatus.COMPLETED,
                    score_card=_score_card(
                        f"run_{index}", completed, tool_failures=1 if not completed else 0
                    ),
                )
            )
        aggregation = aggregate(results)
        for result in results:
            self._repository.append_run_result(batch_id, result)
        self._repository.finish_batch(batch_id, aggregation)
        return batch_id

    def _write_baseline(self) -> Path:
        baseline = _aggregation("core_loop", "rev-frozen", ["b1", "b2"])
        baseline_path = (
            Path(self._temporary_directory.name) / "baseline.json"
        )
        baseline_path.write_text(
            json.dumps(baseline.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )
        return baseline_path

    def test_gate_passes_and_lands_report(self) -> None:
        from endless_task.eval.cli import run_eval_command

        candidate_id = self._make_batch(completions=[True, True])
        baseline_path = self._write_baseline()
        report_dir = Path(self._temporary_directory.name) / "reports"

        arguments = SimpleNamespace(
            eval_command="gate",
            suite="core_loop",
            baseline=str(baseline_path),
            candidate=candidate_id,
            waivers=[],
            report_dir=str(report_dir),
            suite_blocking=False
        )

        exit_code = run_eval_command(
            SimpleNamespace(database_path=str(self.database.path)),
            arguments,
        )
        self.assertEqual(0, exit_code)
        self.assertTrue((report_dir / "gate-core_loop.json").exists())
        self.assertTrue((report_dir / "gate-core_loop.md").exists())

    def test_gate_fails_on_completion_regression(self) -> None:
        from endless_task.eval.cli import run_eval_command

        candidate_id = self._make_batch(completions=[True, False])
        baseline_path = self._write_baseline()

        arguments = SimpleNamespace(
            eval_command="gate",
            suite="core_loop",
            baseline=str(baseline_path),
            candidate=candidate_id,
            waivers=[],
            report_dir=None,
            suite_blocking=False
        )

        exit_code = run_eval_command(
            SimpleNamespace(database_path=str(self.database.path)),
            arguments,
        )
        self.assertEqual(1, exit_code)

    def test_gate_waiver_releases_blocking_regression(self) -> None:
        from endless_task.eval.cli import run_eval_command

        candidate_id = self._make_batch(completions=[True, False])
        baseline_path = self._write_baseline()

        arguments = SimpleNamespace(
            eval_command="gate",
            suite="core_loop",
            baseline=str(baseline_path),
            candidate=candidate_id,
            waivers=["completion;known flake;platform-team;2030-01-01"],
            report_dir=None,
            suite_blocking=False
        )

        exit_code = run_eval_command(
            SimpleNamespace(database_path=str(self.database.path)),
            arguments,
        )
        self.assertEqual(0, exit_code)

    def test_gate_rejects_unknown_suite(self) -> None:
        from endless_task.eval.cli import run_eval_command

        arguments = SimpleNamespace(
            eval_command="gate",
            suite="no_such_suite",
            baseline="x.json",
            candidate="irrelevant",  # suite checked before candidate load
            waivers=[],
            report_dir=None,
            suite_blocking=False
        )

        exit_code = run_eval_command(
            SimpleNamespace(database_path=str(self.database.path)),
            arguments,
        )
        self.assertEqual(2, exit_code)

    def test_suites_lists_catalog(self) -> None:
        from endless_task.eval.cli import run_eval_command

        arguments = SimpleNamespace(
            eval_command="suites"
        )

        exit_code = run_eval_command(
            SimpleNamespace(database_path=str(self.database.path)),
            arguments,
        )
        self.assertEqual(0, exit_code)
