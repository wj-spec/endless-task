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


def _settings(directory: str, *, trace_mode: str) -> AppSettings:
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        tool_platform_v2_enabled=True,
        runtime_trace_mode=trace_mode,
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
