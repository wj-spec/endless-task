"""M6 W6-2: composition-root runtime trace wiring (flag gated)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.api import AppSettings
from endless_task.api.app import (
    _build_container,
    _parse_runtime_trace_mode,
)
from endless_task.runtime import FakeProvider


def _settings(
    directory: str,
    *,
    trace_mode: str,
    otel_mode: str = "0",
    retry_mode: str = "0",
) -> AppSettings:
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        tool_platform_v2_enabled=True,
        runtime_trace_mode=trace_mode,
        otel_export_mode=otel_mode,
        provider_retry_mode=retry_mode,
        # M3B slice B auto-restore and B4 reflection are independent fanout
        # members; switch them off so these tests measure trace/otel wiring in
        # isolation.
        run_auto_restore_enabled=False,
        reflection_enabled=False,
    )


class TraceCompositionTest(unittest.TestCase):
    def _container(self, *, trace_mode: str):
        directory = tempfile.mkdtemp()
        container = _build_container(
            _settings(directory, trace_mode=trace_mode),
            FakeProvider(chunks=("ok",)),
            None,
        )
        container.database.initialize()
        return container

    def test_flag_off_wires_no_observer(self) -> None:
        container = self._container(trace_mode="0")
        self.assertIsNone(container.runtime_v2_trace_observer)

    def test_flag_on_wires_observer(self) -> None:
        container = self._container(trace_mode="all")
        observer = container.runtime_v2_trace_observer
        self.assertIsNotNone(observer)
        # Executors share the same observer via the gateway.
        self.assertIs(
            observer,
            container.runtime_v2_gateway._trace_observer,
        )

    def test_flag_on_wires_span_recorder(self) -> None:
        container = self._container(trace_mode="all")
        recorder = container.runtime_v2_span_recorder
        self.assertIsNotNone(recorder)
        # The gateway forwards the same recorder to its executors.
        self.assertIs(recorder, container.runtime_v2_gateway._span_recorder)

    def test_flag_off_wires_no_span_recorder(self) -> None:
        container = self._container(trace_mode="0")
        self.assertIsNone(container.runtime_v2_span_recorder)

    def test_flag_errors_also_wires_observer(self) -> None:
        container = self._container(trace_mode="errors")
        self.assertIsNotNone(container.runtime_v2_trace_observer)

    def test_flag_sampled_also_wires_observer(self) -> None:
        container = self._container(trace_mode="sampled")
        self.assertIsNotNone(container.runtime_v2_trace_observer)


class TraceFlagParsingTest(unittest.TestCase):
    def test_strict_mode_parse(self) -> None:
        self.assertEqual("0", _parse_runtime_trace_mode("0"))
        self.assertEqual("0", _parse_runtime_trace_mode("off"))
        self.assertEqual("errors", _parse_runtime_trace_mode("errors"))
        self.assertEqual("sampled", _parse_runtime_trace_mode("sampled"))
        self.assertEqual("all", _parse_runtime_trace_mode("all"))
        self.assertEqual("all", _parse_runtime_trace_mode("1"))

    def test_default_is_all_after_real_provider_validation(self) -> None:
        # W6-8: real deepseek E2E passed -> RUNTIME_TRACE defaults to
        # "all" (documented flip per 02 11.4). OTEL stays default off.
        settings = AppSettings(database_path=Path("unused.db"))
        self.assertEqual("all", settings.runtime_trace_mode)
        self.assertEqual("0", settings.otel_export_mode)

    def test_invalid_mode_rejected(self) -> None:
        for value in ("prometheus", "bogus", "verbose"):
            with self.assertRaises(ValueError):
                _parse_runtime_trace_mode(value)

    def test_settings_from_environment_parses_flag(self) -> None:
        import os

        old = os.environ.get("ENDLESS_TASK_RUNTIME_TRACE")
        os.environ["ENDLESS_TASK_RUNTIME_TRACE"] = "sampled"
        try:
            settings = AppSettings.from_environment()
            self.assertEqual("sampled", settings.runtime_trace_mode)
        finally:
            if old is None:
                os.environ.pop("ENDLESS_TASK_RUNTIME_TRACE", None)
            else:
                os.environ["ENDLESS_TASK_RUNTIME_TRACE"] = old


if __name__ == "__main__":
    unittest.main()


class OtelCompositionTest(unittest.TestCase):
    def _member_names(self, *, trace_mode: str, otel_mode: str) -> list[str]:
        directory = tempfile.mkdtemp()
        container = _build_container(
            _settings(directory, trace_mode=trace_mode, otel_mode=otel_mode),
            FakeProvider(chunks=("ok",)),
            None,
        )
        container.database.initialize()
        observer = container.runtime_v2_trace_observer
        if observer is None:
            return []
        return [
            type(member).__name__
            for member in getattr(observer, "_observers", [])
        ]

    def test_otel_off_adds_no_bridge(self) -> None:
        self.assertEqual(
            ["LedgerTraceObserver", "RunTrajectoryExporter"],
            self._member_names(trace_mode="all", otel_mode="0"),
        )

    def test_otel_on_alone_wires_bridge(self) -> None:
        self.assertEqual(
            ["JournalOtelBridge"],
            self._member_names(trace_mode="0", otel_mode="otlp-http"),
        )

    def test_both_on_wires_all_three(self) -> None:
        self.assertEqual(
            [
                "LedgerTraceObserver",
                "RunTrajectoryExporter",
                "JournalOtelBridge",
            ],
            self._member_names(trace_mode="all", otel_mode="otlp-http"),
        )

    def test_strict_otel_mode_parse(self) -> None:
        from endless_task.api.app import _parse_otel_export_mode

        self.assertEqual("0", _parse_otel_export_mode("0"))
        self.assertEqual("0", _parse_otel_export_mode("off"))
        self.assertEqual("otlp-http", _parse_otel_export_mode("otlp-http"))
        self.assertEqual("otlp-http", _parse_otel_export_mode("1"))
        with self.assertRaises(ValueError):
            _parse_otel_export_mode("prometheus")


class ProviderRetryCompositionTest(unittest.TestCase):
    def _container(self, *, retry_mode: str):
        directory = tempfile.mkdtemp()
        container = _build_container(
            _settings(directory, trace_mode="0", retry_mode=retry_mode),
            FakeProvider(chunks=("ok",)),
            None,
        )
        container.database.initialize()
        return container

    def test_mode_0_wires_shadow_evaluator(self) -> None:
        container = self._container(retry_mode="0")
        evaluator = container.runtime_v2_gateway._provider_retry_evaluator
        self.assertIsNone(evaluator)

    def test_mode_1_wires_evaluator(self) -> None:
        container = self._container(retry_mode="1")
        evaluator = container.runtime_v2_gateway._provider_retry_evaluator
        self.assertIsNotNone(evaluator)
        # The gateway forwards it to its executors.
        self.assertIs(
            evaluator,
            container.runtime_v2_gateway._provider_retry_evaluator,
        )

    def test_strict_mode_parse(self) -> None:
        from endless_task.api.app import _parse_strict_mode

        self.assertEqual("0", _parse_strict_mode("0", name="X"))
        self.assertEqual("1", _parse_strict_mode("1", name="X"))
        self.assertEqual("1", _parse_strict_mode("on", name="X"))
        with self.assertRaises(ValueError):
            _parse_strict_mode("2", name="X")
