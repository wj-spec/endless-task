"""M5 SK-5: usage recording + declarative package cases."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.skills.package_cases import (
    PackageCase,
    PackageCaseResult,
    load_package_cases,
    run_package_case,
    run_package_suite,
)
from endless_task.skills.usage import (
    InMemorySkillUsageRecorder,
    SkillUsageEvent,
    SkillUsageSnapshot,
)


class SkillUsageRecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorder = InMemorySkillUsageRecorder()
        self.digest = "a" * 64

    def test_records_and_snapshots_counts(self) -> None:
        self.recorder.surfaced(self.digest)
        self.recorder.surfaced(self.digest)
        self.recorder.invoked(self.digest)
        self.recorder.body_read(self.digest)
        self.recorder.missing_dependencies(self.digest)
        snapshot = self.recorder.snapshot(self.digest)
        self.assertEqual(2, snapshot.surfaced)
        self.assertEqual(1, snapshot.invoked)
        self.assertEqual(1, snapshot.body_read)
        self.assertEqual(1, snapshot.missing_dependencies)
        self.assertEqual(5, self.recorder.event_count)

    def test_unknown_digest_is_zero(self) -> None:
        self.assertEqual(
            SkillUsageSnapshot(),
            self.recorder.snapshot("b" * 64),
        )

    def test_per_digest_isolation(self) -> None:
        self.recorder.invoked(self.digest)
        self.recorder.surfaced("b" * 64)
        self.assertEqual(1, self.recorder.snapshot(self.digest).invoked)
        self.assertEqual(0, self.recorder.snapshot("b" * 64).invoked)
        self.assertEqual(1, self.recorder.snapshot("b" * 64).surfaced)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.recorder.record(
                SkillUsageEvent(digest=self.digest, kind="bogus")
            )


class PackageCaseRunnerTest(unittest.TestCase):
    def test_passing_case(self) -> None:
        case = PackageCase(
            name="review",
            input_text="Review architecture.",
            required_tools=("read_workspace_file",),
            output_contains=("responsibilities", "risks"),
        )
        result = run_package_case(
            case,
            tools_used=("read_workspace_file", "read_skill_file"),
            output="模块职责与风险如下：responsibilities; risks",
        )
        self.assertTrue(result.passed)
        self.assertEqual((), result.failures)

    def test_missing_required_tool_fails(self) -> None:
        case = PackageCase(
            name="review",
            input_text="Review.",
            required_tools=("search_workspace",),
        )
        result = run_package_case(
            case,
            tools_used=("read_workspace_file",),
            output="ok",
        )
        self.assertFalse(result.passed)
        self.assertEqual(("search_workspace",), result.missing_required_tools)

    def test_forbidden_tool_used_fails(self) -> None:
        case = PackageCase(
            name="safe",
            input_text="Do it safely.",
            forbidden_tools=("run_shell",),
        )
        result = run_package_case(
            case,
            tools_used=("run_shell",),
            output="ok",
        )
        self.assertFalse(result.passed)
        self.assertEqual(("run_shell",), result.forbidden_tools_used)

    def test_missing_output_fragment_fails(self) -> None:
        case = PackageCase(
            name="content",
            input_text="Summarize.",
            output_contains=("conclusion",),
        )
        result = run_package_case(
            case,
            tools_used=(),
            output="没有结论。",
        )
        self.assertFalse(result.passed)
        self.assertEqual(("conclusion",), result.missing_output_fragments)


class PackageCaseLoadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.package = Path(self._temporary_directory.name) / "pkg"
        (self.package / "tests").mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _write_cases(self, body: str) -> None:
        (self.package / "tests" / "cases.yaml").write_text(body, encoding="utf-8")

    def test_loads_cases(self) -> None:
        self._write_cases(
            "cases:\n"
            "  - name: review\n"
            "    input: Review this repo.\n"
            "    expected:\n"
            "      required_tools_used: [read_workspace_file]\n"
            "      forbidden_tools_used: [run_shell]\n"
            "      output_contains: [risks]\n"
        )
        suite = load_package_cases(self.package)
        self.assertEqual((), suite.diagnostics)
        self.assertEqual(1, len(suite.cases))
        case = suite.cases[0]
        self.assertEqual("review", case.name)
        self.assertEqual(("read_workspace_file",), case.required_tools)
        self.assertEqual(("run_shell",), case.forbidden_tools)
        self.assertEqual(("risks",), case.output_contains)

    def test_missing_cases_file_is_empty_suite(self) -> None:
        suite = load_package_cases(self.package)
        self.assertEqual((), suite.cases)
        self.assertEqual((), suite.diagnostics)

    def test_malformed_cases_file_diagnosed(self) -> None:
        self._write_cases("::: not yaml [")
        suite = load_package_cases(self.package)
        self.assertEqual(1, len(suite.diagnostics))

    def test_run_suite_end_to_end(self) -> None:
        self._write_cases(
            "cases:\n"
            "  - name: good\n"
            "    input: x\n"
            "    expected:\n"
            "      output_contains: [done]\n"
            "  - name: bad\n"
            "    input: y\n"
            "    expected:\n"
            "      required_tools_used: [search_workspace]\n"
        )
        results = run_package_suite(
            self.package,
            tools_used=("read_workspace_file",),
            output="task done",
        )
        by_name = {result.name: result for result in results}
        self.assertTrue(by_name["good"].passed)
        self.assertFalse(by_name["bad"].passed)


if __name__ == "__main__":
    unittest.main()
