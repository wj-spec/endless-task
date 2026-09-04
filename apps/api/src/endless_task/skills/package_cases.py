"""Declarative skill package tests (M5 SK-5).

07 §8.1: a package may ship ``tests/cases.yaml`` declaring behavioral
expectations only - never arbitrary executable test scripts:

.. code-block:: yaml

    cases:
      - name: architecture-review
        input: Review this repository architecture.
        expected:
          required_tools_used: [search_workspace]
          forbidden_tools_used: []
          output_contains: [responsibilities, risks]

A case passes when the run trace used every required tool, used no
forbidden tool, and the output contains every expected substring. The
runner is a pure function over a recorded trace + output, so bundled
skills can gate releases without executing package code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import yaml

from .manifest import parse_skill_manifest


@dataclass(frozen=True)
class PackageCase:
    name: str
    input_text: str
    required_tools: Tuple[str, ...] = ()
    forbidden_tools: Tuple[str, ...] = ()
    output_contains: Tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return bool(self.name and self.input_text)


@dataclass(frozen=True)
class PackageCaseResult:
    name: str
    passed: bool
    missing_required_tools: Tuple[str, ...] = ()
    forbidden_tools_used: Tuple[str, ...] = ()
    missing_output_fragments: Tuple[str, ...] = ()

    @property
    def failures(self) -> Tuple[str, ...]:
        reasons: list[str] = []
        if self.missing_required_tools:
            reasons.append(
                "required tools not used: " + ", ".join(self.missing_required_tools)
            )
        if self.forbidden_tools_used:
            reasons.append("forbidden tools used: " + ", ".join(self.forbidden_tools_used))
        if self.missing_output_fragments:
            reasons.append(
                "output missing: " + ", ".join(self.missing_output_fragments)
            )
        return tuple(reasons)


@dataclass(frozen=True)
class PackageCaseSuite:
    cases: Tuple[PackageCase, ...] = ()
    diagnostics: Tuple[str, ...] = ()


def load_package_cases(package_dir: Path) -> PackageCaseSuite:
    """Load ``tests/cases.yaml`` from a skill package (safe YAML only)."""
    cases_file = package_dir / "tests" / "cases.yaml"
    if not cases_file.is_file():
        return PackageCaseSuite()
    try:
        text = cases_file.read_text(encoding="utf-8")
        loaded = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        return PackageCaseSuite(diagnostics=(f"cases.yaml 无法解析：{error}",))
    if not isinstance(loaded, Mapping) or not isinstance(loaded.get("cases"), list):
        return PackageCaseSuite(diagnostics=("cases.yaml 缺少 cases 列表。",))
    cases: list[PackageCase] = []
    for raw in loaded["cases"]:
        if not isinstance(raw, Mapping):
            continue
        expected = raw.get("expected")
        if not isinstance(expected, Mapping):
            expected = {}
        cases.append(
            PackageCase(
                name=str(raw.get("name", "")),
                input_text=str(raw.get("input", "")),
                required_tools=_string_list(expected.get("required_tools_used")),
                forbidden_tools=_string_list(expected.get("forbidden_tools_used")),
                output_contains=_string_list(expected.get("output_contains")),
            )
        )
    return PackageCaseSuite(cases=tuple(cases))


def run_package_case(
    case: PackageCase,
    *,
    tools_used: Sequence[str],
    output: str,
) -> PackageCaseResult:
    """Evaluate one case against a recorded tool trace and output."""
    missing_required = tuple(
        tool for tool in case.required_tools if tool not in set(tools_used)
    )
    forbidden_used = tuple(
        tool for tool in case.forbidden_tools if tool in set(tools_used)
    )
    missing_fragments = tuple(
        fragment
        for fragment in case.output_contains
        if fragment not in (output or "")
    )
    return PackageCaseResult(
        name=case.name,
        passed=not missing_required and not forbidden_used and not missing_fragments,
        missing_required_tools=missing_required,
        forbidden_tools_used=forbidden_used,
        missing_output_fragments=missing_fragments,
    )


def run_package_suite(
    package_dir: Path,
    *,
    tools_used: Sequence[str],
    output: str,
) -> Tuple[PackageCaseResult, ...]:
    """Run every declared case of a package (bundled regression gate)."""
    suite = load_package_cases(package_dir)
    return tuple(
        run_package_case(case, tools_used=tools_used, output=output)
        for case in suite.cases
    )


def _string_list(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


__all__ = [
    "PackageCase",
    "PackageCaseResult",
    "PackageCaseSuite",
    "load_package_cases",
    "run_package_case",
    "run_package_suite",
]
