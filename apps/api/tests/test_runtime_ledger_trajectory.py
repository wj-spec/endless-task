"""M6 OE-3: trajectory export, redaction and Level 1 recorded-outcome replay."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import EffectOutcome, EffectReceipt
from endless_task.runtime_ledger.protocol import (
    RuntimeLedgerEvent,
    TraceContext,
)
from endless_task.runtime_ledger.replay import (
    ReplayClassification,
    replay_trajectory,
)
from endless_task.runtime_ledger.sqlite_recorder import SqliteRuntimeLedger
from endless_task.runtime_ledger.trajectory import (
    TrajectoryBundle,
    TrajectoryManifest,
    export_trajectory,
    load_trajectory_bundle,
    redact_text,
    write_trajectory_bundle,
)
from endless_task.storage import Database


def trace(run_id: str = "run_1") -> TraceContext:
    return TraceContext(
        trace_id="trace_1",
        run_id=run_id,
        correlation_id="corr_1",
    )


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


class RedactionPipelineTest(unittest.TestCase):
    def test_redacts_secret_patterns_in_text(self) -> None:
        text = "password=supersecret123, api_key: abcdef1234567890"
        redacted = redact_text(text)
        self.assertNotIn("supersecret123", redacted)
        self.assertNotIn("abcdef1234567890", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_redacts_openai_style_and_aws_keys(self) -> None:
        for secret in (
            "sk-proj-abcdefghijklmnopqrstuvwxyz",
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
        ):
            self.assertNotIn(secret, redact_text(f"token={secret}"))

    def test_plain_text_unchanged(self) -> None:
        text = "The quick brown fox jumps over the lazy dog."
        self.assertEqual(text, redact_text(text))

    def test_drops_raw_payload_keys_and_scrubs_nested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "trace.db")
            database.initialize()
            ledger = SqliteRuntimeLedger(database)
            import asyncio

            asyncio.run(
                ledger.append_event(
                    RuntimeLedgerEvent(
                        event_id="evt_1",
                        event_type="tool.called",
                        occurred_at="2026-09-05T00:00:00Z",
                        trace=trace(),
                        data={
                            "tool_call_id": "call_1",
                            "raw_arguments": '{"api_key": "sk-abcdefghijklmnopqrstuvwxyz"}',
                            "raw_result": "<file bytes>",
                        },
                        safety_critical=False,
                    )
                )
            )
            bundle = export_trajectory(ledger, manifest=manifest())
            event = bundle.events[0]
            self.assertEqual("[REDACTED]", event["data"]["raw_arguments"])
            self.assertEqual("[REDACTED]", event["data"]["raw_result"])
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", json.dumps(bundle.to_json()))

    def test_redacts_message_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "trace.db")
            database.initialize()
            ledger = SqliteRuntimeLedger(database)
            bundle = export_trajectory(
                ledger,
                manifest=manifest(),
                messages=[
                    {
                        "role": "assistant",
                        "content": 'Use api_key="sk-abcdefghijklmnopqrstuvwxyz"',
                    }
                ],
            )
            content = bundle.messages[0]["content"]
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", content)
            self.assertIn("[REDACTED]", content)


class TrajectoryExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "trace.db"
        )
        self.database.initialize()
        self.ledger = SqliteRuntimeLedger(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _append(self, event: RuntimeLedgerEvent) -> None:
        import asyncio

        asyncio.run(self.ledger.append_event(event))

    def test_export_failed_run_bundle_content(self) -> None:
        self._append(
            RuntimeLedgerEvent(
                event_id="run_evt",
                event_type="run.failed",
                occurred_at="2026-09-05T00:00:01Z",
                trace=trace(),
                data={"error": "provider timeout"},
                safety_critical=True,
            )
        )
        bundle = export_trajectory(self.ledger, manifest=manifest())
        self.assertEqual("run_1", bundle.manifest.source_run_id)
        self.assertEqual(1, len(bundle.events))
        self.assertEqual("run.failed", bundle.events[0]["eventType"])
        self.assertTrue(bundle.bundle_digest)

    def test_write_and_load_roundtrip_matches_layout(self) -> None:
        self._append(
            RuntimeLedgerEvent(
                event_id="evt_1",
                event_type="run.started",
                occurred_at="2026-09-05T00:00:00Z",
                trace=trace(),
                data={"run": "run_1"},
            )
        )
        bundle = export_trajectory(
            self.ledger,
            manifest=manifest(),
            tool_outcomes=[{"toolCallId": "call_1", "status": "completed"}],
            effects=[{"effect_id": "fx_1", "outcome": "committed"}],
            expected={"terminal": "completed"},
        )
        directory = Path(self._temporary_directory.name) / "trajectory-run_1"
        write_trajectory_bundle(bundle, directory)

        for filename in (
            "manifest.json",
            "events.jsonl",
            "spans.jsonl",
            "messages.redacted.jsonl",
            "tool-outcomes.redacted.jsonl",
            "context-segments.jsonl",
            "effects.jsonl",
            "expected.json",
        ):
            self.assertTrue((directory / filename).exists(), filename)

        loaded = load_trajectory_bundle(directory)
        self.assertEqual("run_1", loaded.manifest.source_run_id)
        self.assertEqual(1, len(loaded.events))
        self.assertEqual("run.started", loaded.events[0]["eventType"])
        self.assertEqual(1, len(loaded.tool_outcomes))
        self.assertEqual(1, len(loaded.effects))
        self.assertEqual("completed", loaded.expected["terminal"])
        # Digest is recomputed for in-memory bundles only; loaded bundles
        # carry the manifest's records without a stored digest.
        self.assertEqual(bundle.manifest, loaded.manifest)

    def test_load_empty_bundle(self) -> None:
        directory = Path(self._temporary_directory.name) / "trajectory-empty"
        write_trajectory_bundle(
            export_trajectory(self.ledger, manifest=manifest("run_empty")),
            directory,
        )
        loaded = load_trajectory_bundle(directory)
        self.assertEqual("run_empty", loaded.manifest.source_run_id)
        self.assertEqual((), loaded.events)
        self.assertEqual((), loaded.effects)

    def test_jsonl_files_are_line_delimited_json(self) -> None:
        self._append(
            RuntimeLedgerEvent(
                event_id="evt_a",
                event_type="run.started",
                occurred_at="2026-09-05T00:00:00Z",
                trace=trace(),
                data={},
            )
        )
        directory = Path(self._temporary_directory.name) / "trajectory-jsonl"
        write_trajectory_bundle(
            export_trajectory(self.ledger, manifest=manifest()),
            directory,
        )
        line = (directory / "events.jsonl").read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        self.assertEqual("evt_a", parsed["eventId"])


class RecordedOutcomeReplayTest(unittest.TestCase):
    def _bundle(
        self,
        *,
        events: list[dict],
        tool_outcomes: list[dict] | None = None,
        run_id: str = "run_1",
    ) -> TrajectoryBundle:
        return TrajectoryBundle(
            manifest=manifest(run_id),
            events=tuple(events),
            tool_outcomes=tuple(tool_outcomes or ()),
        )

    def _event(
        self,
        event_id: str,
        event_type: str,
        occurred_at: str,
        data: dict | None = None,
    ) -> dict:
        return {
            "eventId": event_id,
            "eventType": event_type,
            "occurredAt": occurred_at,
            "traceId": "trace_1",
            "runId": "run_1",
            "safetyCritical": False,
            "data": data or {},
        }

    def test_replay_is_deterministic_same_bundle(self) -> None:
        bundle = self._bundle(
            events=[
                self._event(
                    "evt_1",
                    "effect_receipt",
                    "2026-09-05T00:00:00Z",
                    {
                        "effect_id": "fx_1",
                        "tool_call_id": "call_1",
                        "outcome": "committed",
                        "effect_type": "file_write",
                    },
                ),
                self._event("evt_2", "run.completed", "2026-09-05T00:00:01Z"),
            ]
        )
        first = replay_trajectory(bundle)
        second = replay_trajectory(bundle)
        self.assertEqual(first.classification, second.classification)
        self.assertEqual(first.ordered_event_ids, second.ordered_event_ids)
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(ReplayClassification.COMPLETED, first.classification)

    def test_replay_orders_events_deterministically(self) -> None:
        # Out-of-order bundle input still yields the ordered sequence.
        bundle = self._bundle(
            events=[
                self._event("evt_b", "run.completed", "2026-09-05T00:00:02Z"),
                self._event(
                    "evt_a",
                    "effect_receipt",
                    "2026-09-05T00:00:01Z",
                    {"effect_id": "fx_1", "tool_call_id": "call_1", "outcome": "committed"},
                ),
            ]
        )
        result = replay_trajectory(bundle)
        self.assertEqual(("evt_a", "evt_b"), result.ordered_event_ids)

    def test_replay_unknown_effect_downgrades_completed_to_inconclusive(self) -> None:
        bundle = self._bundle(
            events=[
                self._event(
                    "evt_1",
                    "effect_receipt",
                    "2026-09-05T00:00:00Z",
                    {"effect_id": "fx_1", "tool_call_id": "call_1", "outcome": "unknown"},
                ),
                self._event("evt_2", "run.completed", "2026-09-05T00:00:01Z"),
            ]
        )
        result = replay_trajectory(bundle)
        self.assertEqual(ReplayClassification.INCONCLUSIVE, result.classification)
        self.assertEqual(1, result.unknown_effect_count)
        self.assertTrue(any("inconclusive" in note for note in result.consistency_notes))

    def test_replay_failed_run_classification(self) -> None:
        bundle = self._bundle(
            events=[
                self._event("evt_1", "run.failed", "2026-09-05T00:00:00Z", {"error": "x"})
            ]
        )
        result = replay_trajectory(bundle)
        self.assertEqual(ReplayClassification.FAILED, result.classification)

    def test_replay_cancelled_run_classification(self) -> None:
        bundle = self._bundle(
            events=[
                self._event("evt_1", "run.cancelled", "2026-09-05T00:00:00Z")
            ]
        )
        result = replay_trajectory(bundle)
        self.assertEqual(ReplayClassification.CANCELLED, result.classification)

    def test_replay_empty_bundle_is_empty_classification(self) -> None:
        result = replay_trajectory(self._bundle(events=[]))
        self.assertEqual(ReplayClassification.EMPTY, result.classification)

    def test_replay_notes_missing_effect_receipt_for_tool_outcome(self) -> None:
        bundle = self._bundle(
            events=[
                self._event("evt_1", "run.completed", "2026-09-05T00:00:00Z")
            ],
            tool_outcomes=[{"toolCallId": "call_orphan", "status": "completed"}],
        )
        result = replay_trajectory(bundle)
        self.assertTrue(
            any("no matching effect receipt" in note for note in result.consistency_notes)
        )

    def test_replay_effect_outcome_rollup(self) -> None:
        bundle = self._bundle(
            events=[
                self._event(
                    "evt_1",
                    "effect_receipt",
                    "2026-09-05T00:00:00Z",
                    {
                        "effect_id": "fx_commit",
                        "tool_call_id": "call_1",
                        "outcome": "committed",
                        "effect_type": "file_write",
                    },
                ),
                self._event(
                    "evt_2",
                    "effect_receipt",
                    "2026-09-05T00:00:01Z",
                    {
                        "effect_id": "fx_rollback",
                        "tool_call_id": "call_2",
                        "outcome": "rolled_back",
                        "effect_type": "db_update",
                    },
                ),
                self._event("evt_3", "run.completed", "2026-09-05T00:00:02Z"),
            ]
        )
        result = replay_trajectory(bundle)
        self.assertEqual(ReplayClassification.COMPLETED, result.classification)
        outcomes = {effect.effect_id: effect.outcome for effect in result.effects}
        self.assertEqual(
            {"fx_commit": "committed", "fx_rollback": "rolled_back"},
            outcomes,
        )

    def test_same_content_different_bundle_instances_same_digest(self) -> None:
        bundle_a = self._bundle(
            events=[
                self._event("evt_1", "run.completed", "2026-09-05T00:00:00Z")
            ]
        )
        bundle_b = TrajectoryBundle(
            manifest=manifest(),
            events=(
                {
                    "eventId": "evt_1",
                    "eventType": "run.completed",
                    "occurredAt": "2026-09-05T00:00:00Z",
                    "traceId": "trace_1",
                    "runId": "run_1",
                    "safetyCritical": False,
                    "data": {},
                },
            ),
            tool_outcomes=(),
        )
        self.assertEqual(
            replay_trajectory(bundle_a).digest,
            replay_trajectory(bundle_b).digest,
        )


if __name__ == "__main__":
    unittest.main()
