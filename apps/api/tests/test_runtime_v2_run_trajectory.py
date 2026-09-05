"""M6 W6-3: failed-run trajectory export from the v2 journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_v2.run_trajectory import (
    FanoutTraceObserver,
    JournalEventView,
    JournalOtelBridge,
    JournalRunView,
    RunTrajectoryExporter,
    default_journal_reader,
)


def _view(
    *,
    run_id: str = "run_1",
    status: str = "failed",
    error_code: str | None = "provider_timeout",
    events: list | None = None,
) -> JournalRunView:
    return JournalRunView(
        run_id=run_id,
        status=status,
        error_code=error_code,
        safe_message="模型调用超时。",
        started_at="2026-09-05T00:00:00Z",
        finished_at="2026-09-05T00:00:01Z",
        correlation_id="corr_1",
        events=tuple(
            events
            or [
                JournalEventView(
                    event_id="evt_1",
                    event_type="run_failed",
                    occurred_at="2026-09-05T00:00:01Z",
                    safety_critical=True,
                    data={"errorCode": "provider_timeout"},
                )
            ]
        ),
    )


class ExporterUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.exporter = RunTrajectoryExporter(
            export_root=self.root,
            journal_reader=lambda run_id: _view(run_id=run_id),
            runtime_version="0.0.0",
            provider="fake",
            model="fake-model",
            config_fingerprint="cfg",
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_failed_run_exports_bundle_directory(self) -> None:
        directory = self.exporter.export_failed_run("run_1")
        self.assertIsNotNone(directory)
        bundle_dir = Path(directory)  # type: ignore[arg-type]
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
            self.assertTrue((bundle_dir / filename).exists(), filename)
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual("run_1", manifest["sourceRunId"])
        expected = json.loads(
            (bundle_dir / "expected.json").read_text(encoding="utf-8")
        )
        self.assertEqual("failed", expected["terminal"])
        self.assertEqual("provider_timeout", expected["errorCode"])
        # 07 G1: wall-clock duration lands in the bundle for offline P95.
        self.assertEqual(1000, expected["durationMs"])

    def test_completed_run_not_auto_exported(self) -> None:
        exporter = RunTrajectoryExporter(
            export_root=self.root,
            journal_reader=lambda run_id: _view(run_id=run_id, status="completed"),
            runtime_version="0.0.0",
            provider="fake",
            model="fake-model",
            config_fingerprint="cfg",
        )
        result = exporter.export_failed_run("run_1")
        self.assertIsNone(result)
        self.assertFalse((self.root / "trajectory-run_1").exists())

    def test_explicit_export_of_completed_run(self) -> None:
        exporter = RunTrajectoryExporter(
            export_root=self.root,
            journal_reader=lambda run_id: _view(run_id=run_id, status="completed"),
            runtime_version="0.0.0",
            provider="fake",
            model="fake-model",
            config_fingerprint="cfg",
        )
        directory = exporter.export_run("run_1")
        self.assertIsNotNone(directory)
        self.assertTrue((Path(directory) / "manifest.json").exists())

    def test_redacts_secrets_in_journal_payloads(self) -> None:
        # Journal payload may contain provider error bodies with secrets;
        # the OE-3 redaction pipeline scrubs them before writing jsonl.
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        exporter = RunTrajectoryExporter(
            export_root=self.root,
            journal_reader=lambda run_id: _view(
                run_id=run_id,
                events=[
                    JournalEventView(
                        event_id="evt_1",
                        event_type="model_turn_failed",
                        occurred_at="2026-09-05T00:00:01Z",
                        safety_critical=True,
                        data={"errorBody": f"auth failed token={secret}"},
                    )
                ],
            ),
            runtime_version="0.0.0",
            provider="fake",
            model="fake-model",
            config_fingerprint="cfg",
        )
        exporter.export_failed_run("run_2")
        content = (self.root / "trajectory-run_2" / "events.jsonl").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(secret, content)
        self.assertIn("[REDACTED]", content)

    def test_export_error_never_raises(self) -> None:
        def explode(run_id: str) -> JournalRunView:
            raise RuntimeError("journal unavailable")

        exporter = RunTrajectoryExporter(
            export_root=self.root,
            journal_reader=explode,
            runtime_version="0.0.0",
            provider="fake",
            model="fake-model",
            config_fingerprint="cfg",
        )
        self.assertIsNone(exporter.export_failed_run("run_1"))
        self.assertIsNone(exporter.export_run("run_1"))


class FanoutObserverTest(unittest.TestCase):
    def test_fans_out_to_all_observers(self) -> None:
        import asyncio

        calls: list[str] = []

        class One:
            async def on_run_terminal(self, run_id: str) -> None:
                calls.append(f"one:{run_id}")

        class Two:
            async def on_run_terminal(self, run_id: str) -> None:
                calls.append(f"two:{run_id}")

        fanout = FanoutTraceObserver([One(), Two()])
        asyncio.run(fanout.on_run_terminal("run_x"))
        self.assertEqual(["one:run_x", "two:run_x"], calls)

    def test_one_failing_observer_does_not_block_others(self) -> None:
        import asyncio

        calls: list[str] = []

        class Exploding:
            async def on_run_terminal(self, run_id: str) -> None:
                raise RuntimeError("boom")

        class Working:
            async def on_run_terminal(self, run_id: str) -> None:
                calls.append(run_id)

        fanout = FanoutTraceObserver([Exploding(), Working()])
        asyncio.run(fanout.on_run_terminal("run_y"))
        self.assertEqual(["run_y"], calls)

    def test_empty_fanout_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FanoutTraceObserver([])


class JournalReaderTest(unittest.TestCase):
    def test_default_journal_reader_maps_events(self) -> None:
        class _Status:
            def __init__(self, value: str) -> None:
                self.value = value

        class _Run:
            def __init__(self) -> None:
                self.id = "run_1"
                self.status = _Status("failed")
                self.error_code = "x"
                self.safe_message = "m"
                self.started_at = "s"
                self.finished_at = "f"
                self.correlation_id = "corr"

        class _Event:
            event_id = "e1"
            event_type = "run_failed"
            occurred_at = "at"
            payload = {"errorCode": "x"}

        class _Repo:
            def get_run(self, run_id):
                return _Run()

            def list_runtime_events(self, run_id):
                return [_Event()]

        view = default_journal_reader(_Repo())("run_1")
        self.assertEqual("failed", view.status)
        self.assertEqual(1, len(view.events))
        self.assertTrue(view.events[0].safety_critical)


if __name__ == "__main__":
    unittest.main()


class FailedRunExecutorE2ETest(unittest.TestCase):
    """Real AgentRunExecutor failed run -> fanout -> trajectory bundle."""

    def setUp(self) -> None:
        from endless_task.runtime_v2 import Actor, TranscriptEntryType
        from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "trace.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self._Actor = Actor
        self._TranscriptEntryType = TranscriptEntryType

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=self._TranscriptEntryType.USER_MESSAGE,
            actor=self._Actor.USER,
            payload={"content": "hi"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )

    def test_failed_run_exports_bundle_through_fanout(self) -> None:
        import asyncio

        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.runtime.provider import ProviderError
        from endless_task.runtime_v2 import AgentRunExecutor
        from endless_task.tooling import ToolRegistry

        class FailingProvider:
            name = "failing"

            async def stream(self, request, cancellation_token):
                raise ProviderError(
                    "provider_timeout", "模型调用超时。", retryable=False
                )

        run = self._create_run()
        exporter = RunTrajectoryExporter(
            export_root=Path(self._temporary_directory.name) / "exports",
            journal_reader=default_journal_reader(self.repository),
            runtime_version="0.0.0",
            provider="failing",
            model="fake-model",
            config_fingerprint="cfg",
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=FailingProvider(),
            tool_registry=ToolRegistry(),
            model="fake-model",
            trace_observer=FanoutTraceObserver([exporter]),
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        self.assertEqual("failed", result.status.value)
        bundle_dir = (
            Path(self._temporary_directory.name)
            / "exports"
            / f"trajectory-{run.id}"
        )
        self.assertTrue(bundle_dir.is_dir(), bundle_dir)
        manifest = json.loads(
            (bundle_dir / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(run.id, manifest["sourceRunId"])
        events = [
            json.loads(line)
            for line in (bundle_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertTrue(
            any(event["eventType"] == "run_failed" for event in events),
            events,
        )


class JournalOtelBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        from endless_task.runtime_ledger.otel_exporter import (
            OtelExportMode,
            OtelExporter,
            OtelExporterConfig,
        )

        self._temporary_directory = tempfile.TemporaryDirectory()

        class RecordingTransport:
            def __init__(self) -> None:
                self.bodies: list[bytes] = []

            def __call__(self, endpoint: str, body: bytes) -> int:
                self.bodies.append(body)
                return 200

        self.transport = RecordingTransport()
        self.exporter = OtelExporter(
            OtelExporterConfig(mode=OtelExportMode.OTLP_HTTP),
            transport=self.transport,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _events_view(self, *, events: list | None = None):
        return JournalRunView(
            run_id="run_1",
            status="failed",
            error_code="x",
            safe_message="m",
            started_at="s",
            finished_at="f",
            correlation_id="corr_1",
            events=tuple(
                events
                or [
                    JournalEventView(
                        event_id="e1",
                        event_type="run_failed",
                        occurred_at="2026-09-05T00:00:01Z",
                        safety_critical=True,
                        data={"errorCode": "provider_timeout"},
                    )
                ]
            ),
        )

    def test_exports_allowlisted_events_only(self) -> None:
        import asyncio

        bridge = JournalOtelBridge(
            exporter=self.exporter,
            journal_reader=lambda run_id: self._events_view(
                events=[
                    JournalEventView(
                        event_id="e1",
                        event_type="run_failed",
                        occurred_at="2026-09-05T00:00:01Z",
                        safety_critical=True,
                        data={"errorCode": "provider_timeout", "secret": "sk-abc"},
                    ),
                    # content-bearing event must NOT export
                    JournalEventView(
                        event_id="e2",
                        event_type="model_text_delta",
                        occurred_at="2026-09-05T00:00:02Z",
                        safety_critical=False,
                        data={"delta": "user content"},
                    ),
                ]
            ),
        )
        asyncio.run(bridge.on_run_terminal("run_1"))
        self.assertEqual(1, len(self.transport.bodies))
        body = json.loads(self.transport.bodies[0])
        logs = body["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        self.assertEqual(1, len(logs))
        self.assertEqual("run_failed", logs[0]["body"]["stringValue"])
        keys = {item["key"] for item in logs[0]["attributes"]}
        # safe payload key exported; secret key dropped
        self.assertIn("errorCode", keys)
        self.assertNotIn("secret", keys)

    def test_no_exportable_events_sends_nothing(self) -> None:
        import asyncio

        bridge = JournalOtelBridge(
            exporter=self.exporter,
            journal_reader=lambda run_id: self._events_view(
                events=[
                    JournalEventView(
                        event_id="e1",
                        event_type="model_turn_started",
                        occurred_at="2026-09-05T00:00:01Z",
                        safety_critical=False,
                        data={},
                    )
                ]
            ),
        )
        asyncio.run(bridge.on_run_terminal("run_1"))
        self.assertEqual([], self.transport.bodies)

    def test_bridge_never_raises_on_reader_error(self) -> None:
        import asyncio

        def explode(run_id: str) -> JournalRunView:
            raise RuntimeError("journal down")

        bridge = JournalOtelBridge(exporter=self.exporter, journal_reader=explode)
        asyncio.run(bridge.on_run_terminal("run_1"))
