"""Data-driven Eval topic suites and change gates (M6 OE-4).

08 §10.1/10.2: evaluation is organized as **topic suites**, and platform
changes are gated to the suites whose scenarios they can regress:

- a suite declares the metric keys its scenario holds (not evaluator
  classes: the same metric vocabulary is produced by repo-run evaluators
  and by the trajectory evaluator, so a suite can gate either source),
- the change-gate map binds each platform area (tool platform, context
  engine, execution backend, delegation, skills) to the suites that must
  run before that area's change can merge,
- the catalog is validated against the known metric universe at import
  time: an unknown key raises, so a suite can never silently run with a
  key no evaluator produces (fail closed, no dead config).

Only suites that have real metric coverage today are registered; topics
whose evaluators do not exist yet (e.g. sandbox_escape) stay out of the
catalog and are documented as pending in 08 §21.x rather than stubbed as
empty suites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

#: The known metric universe: keys produced by the repo-run evaluators
#: (eval/evaluators.py) plus the trajectory evaluator
#: (eval/trajectory_eval.py). A suite may only reference these keys.
KNOWN_METRIC_KEYS: frozenset[str] = frozenset(
    {
        # repo-run evaluators (deterministic, over v2 replay)
        "completion",
        "tool_correctness",
        "tool_call_count",
        "tool_failure_count",
        "approval_gate",
        "ungated_unknown_tool_count",
        "robustness",
        "replay_warning_count",
        "input_tokens",
        "output_tokens",
        "model_turn_count",
        "tool_execution_count",
        "duration_ms",
        "loop_detected",
        # D1：记忆/引用/反思质量（确定性、可回归）
        "citation_correctness",
        "citation_unresolved_count",
        "memory_quality",
        "memory_write_count",
        "reflection_quality",
        "reflection_count",
        # checkpoint/restore + context compaction suites (G1 open list 6)
        "failed_run",
        "auto_restored",
        "context_compacted",
        # trajectory evaluator (deterministic, over exported bundles)
        "trajectory_completion",
        "trajectory_unknown_effect_count",
        "trajectory_effect_count",
        "trajectory_committed_effect_count",
        "trajectory_consistency_note_count",
        "trajectory_cost_usd_total",
        "trajectory_request_count",
    }
)


@dataclass(frozen=True)
class EvalSuite:
    """One named topic suite: a validated set of metric keys."""

    name: str
    description: str
    metric_keys: Tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.strip())
        if not self.name:
            raise ValueError("suite name must be non-empty")
        unknown = [key for key in self.metric_keys if key not in KNOWN_METRIC_KEYS]
        if unknown:
            raise ValueError(
                f"suite {self.name!r} references unknown metric keys: "
                + ", ".join(sorted(unknown))
            )


SUITE_CATALOG: Mapping[str, EvalSuite] = {
    suite.name: suite
    for suite in (
        EvalSuite(
            name="core_loop",
            description="main agent loop: completion, termination hygiene, robustness",
            metric_keys=(
                "completion",
                "loop_detected",
                "robustness",
                "tool_failure_count",
                "trajectory_completion",
                "trajectory_consistency_note_count",
            ),
        ),
        EvalSuite(
            name="checkpoint_restore",
            description=(
                "failed-run population + auto-restore coverage: a FAILED run "
                "must carry run_auto_restored (rollback executed), else the "
                "crash-window repair regressed"
            ),
            metric_keys=("failed_run", "auto_restored"),
        ),
        EvalSuite(
            name="context_compaction",
            description=(
                "context compaction observability: a lane that compacted is "
                "flagged (informational) for compaction-quality topics"
            ),
            metric_keys=("context_compacted",),
        ),
        EvalSuite(
            name="memory_and_citations",
            description=(
                "memory and citation quality: writes stay sourced and active, "
                "reflection insights stay sourced, and every [K] marker in an "
                "answer resolves to a citation actually injected for that turn"
            ),
            metric_keys=(
                "citation_correctness",
                "citation_unresolved_count",
                "memory_quality",
                "memory_write_count",
                "reflection_quality",
                "reflection_count",
            ),
        ),
        EvalSuite(
            name="schema_and_tool_use",
            description="tool calling contract: correctness and failure accounting",
            metric_keys=(
                "tool_correctness",
                "tool_call_count",
                "tool_failure_count",
                "trajectory_effect_count",
            ),
        ),
        EvalSuite(
            name="safety_and_approval",
            description="approval gates and side-effect visibility",
            metric_keys=(
                "approval_gate",
                "ungated_unknown_tool_count",
                "trajectory_unknown_effect_count",
            ),
        ),
        EvalSuite(
            name="delegation",
            description="child-run delegation health (recorded outcomes level)",
            metric_keys=(
                "trajectory_completion",
                "trajectory_unknown_effect_count",
                "trajectory_consistency_note_count",
            ),
        ),
        EvalSuite(
            name="skill_selection",
            description="skill-driven runs keep the core loop and tool use sound",
            metric_keys=(
                "completion",
                "tool_correctness",
                "robustness",
            ),
        ),
    )
}


@dataclass(frozen=True)
class ChangeGate:
    """Which suites must run before one platform area can merge."""

    area: str
    description: str
    suite_names: Tuple[str, ...]

    def __post_init__(self) -> None:
        missing = [name for name in self.suite_names if name not in SUITE_CATALOG]
        if missing:
            raise ValueError(
                f"change gate {self.area!r} references unregistered suites: "
                + ", ".join(sorted(missing))
            )


CHANGE_GATES: Mapping[str, ChangeGate] = {
    gate.area: gate
    for gate in (
        ChangeGate(
            area="tool_platform",
            description="Tool Platform changes run core loop + tool use + safety",
            suite_names=("core_loop", "schema_and_tool_use", "safety_and_approval"),
        ),
        ChangeGate(
            area="delegation",
            description="Delegation changes run recorded-outcome and safety suites",
            suite_names=("delegation", "safety_and_approval"),
        ),
        ChangeGate(
            area="memory",
            description=(
                "Memory/knowledge changes run the memory-and-citations suite "
                "plus the core loop"
            ),
            suite_names=("memory_and_citations", "core_loop"),
        ),
        ChangeGate(
            area="skills",
            description="Skill changes run selection + core loop suites",
            suite_names=("skill_selection", "core_loop"),
        ),
    )
}

#: Topic suites called out in 08 §10.1 that still lack metric coverage
#: (their evaluators land with the topic work) — catalog stays without
#: them rather than registering empty suites.
#: Topics with no evaluator yet (08 §21.x). checkpoint_restore shipped its
#: evaluator (06 G1 open list 6) and left this list.
PENDING_SUITE_NAMES: Tuple[str, ...] = (
    "memory_retrieval",
    "sandbox_escape",
    "product_visible_semantics",
)


def suite_names_for_change(area: str) -> Tuple[str, ...]:
    """Suites that must run for one platform area (08 §10.2)."""
    gate = CHANGE_GATES.get(area)
    if gate is None:
        raise ValueError(
            f"unknown change area {area!r}; known: "
            + ", ".join(sorted(CHANGE_GATES))
        )
    return gate.suite_names


def suites_for_change(area: str) -> Tuple[EvalSuite, ...]:
    return tuple(SUITE_CATALOG[name] for name in suite_names_for_change(area))


__all__ = [
    "CHANGE_GATES",
    "ChangeGate",
    "EvalSuite",
    "KNOWN_METRIC_KEYS",
    "PENDING_SUITE_NAMES",
    "SUITE_CATALOG",
    "suite_names_for_change",
    "suites_for_change",
]
