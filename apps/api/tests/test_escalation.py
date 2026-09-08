"""C4 终止与升级：把"卡住/预算将尽"变成一次人工决策。"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Optional

from endless_task.runtime_v2.escalation import (
    BUDGET_REASON,
    DEFAULT_BUDGET_RATIO,
    NO_PROGRESS_REASON,
    OPTION_CHANGE_APPROACH,
    OPTION_CONTINUE,
    OPTION_TAKE_OVER,
    EscalationBudget,
    EscalationProgress,
    budget_exhausted,
    budget_ratio,
    build_escalation_report,
)
from endless_task.runtime_v2.failure_memory import build_failure_memory


@dataclass
class _Execution:
    tool_name: str = "read_file"
    status: str = "failed"
    error_code: Optional[str] = "temporary_unavailable"
    safe_message: Optional[str] = "工具暂时不可用。"
    retryable: Optional[bool] = True
    model_turn_id: Optional[str] = "turn_1"
    id: str = "exec_1"
    arguments_hash: str = "hash_1"


def _repeating_memory():
    return build_failure_memory(
        (
            _Execution(id="exec_1"),
            _Execution(id="exec_2", model_turn_id="turn_2"),
            _Execution(id="exec_3", model_turn_id="turn_3"),
        )
    )


class BudgetTest(unittest.TestCase):
    def test_ratio_is_none_without_limit(self) -> None:
        self.assertIsNone(budget_ratio(used_tokens=10, limit_tokens=None))
        self.assertIsNone(budget_ratio(used_tokens=10, limit_tokens=0))

    def test_ratio_is_clamped_to_one(self) -> None:
        self.assertEqual(1.0, budget_ratio(used_tokens=200, limit_tokens=100))
        self.assertAlmostEqual(0.5, budget_ratio(used_tokens=50, limit_tokens=100))

    def test_exhausted_uses_threshold(self) -> None:
        self.assertFalse(
            budget_exhausted(used_tokens=84, limit_tokens=100, ratio=0.85)
        )
        self.assertTrue(
            budget_exhausted(used_tokens=85, limit_tokens=100, ratio=0.85)
        )

    def test_exhausted_is_false_without_limit(self) -> None:
        self.assertFalse(budget_exhausted(used_tokens=10_000, limit_tokens=None))

    def test_default_ratio_is_reasonable(self) -> None:
        self.assertGreater(DEFAULT_BUDGET_RATIO, 0.5)
        self.assertLess(DEFAULT_BUDGET_RATIO, 1.0)


class ProgressTest(unittest.TestCase):
    def test_progress_json_round_trip(self) -> None:
        progress = EscalationProgress(
            model_turns=3,
            tool_calls=5,
            tool_failures=3,
            produced_characters=120,
            input_tokens=800,
            output_tokens=200,
        )
        payload = progress.as_json()
        self.assertEqual(3, payload["modelTurns"])
        self.assertEqual(5, payload["toolCalls"])
        self.assertEqual(3, payload["toolFailures"])
        self.assertEqual(120, payload["producedCharacters"])
        json.dumps(payload, ensure_ascii=False)


class EscalationReportTest(unittest.TestCase):
    def _report(self, reason: str, **kwargs):
        progress = EscalationProgress(
            model_turns=4,
            tool_calls=6,
            tool_failures=3,
            produced_characters=42,
            input_tokens=900,
            output_tokens=100,
        )
        budget = EscalationBudget(
            used_tokens=1000,
            limit_tokens=2000,
            used_ratio=0.5,
        )
        return build_escalation_report(
            reason=reason,
            progress=progress,
            budget=budget,
            **kwargs,
        )

    def test_no_progress_report_lists_failures_and_options(self) -> None:
        report = self._report(NO_PROGRESS_REASON, memory=_repeating_memory())
        self.assertEqual(NO_PROGRESS_REASON, report.reason)
        self.assertIn("没有实质进展", report.summary)
        self.assertEqual(
            (OPTION_CONTINUE, OPTION_CHANGE_APPROACH, OPTION_TAKE_OVER),
            report.options,
        )
        self.assertEqual(1, len(report.repeated_failures))
        self.assertEqual(3, report.repeated_failures[0]["count"])
        self.assertIn("read_file", report.guidance)
        self.assertIn("不要原样重复", report.guidance)

    def test_budget_report_mentions_ratio(self) -> None:
        budget = EscalationBudget(
            used_tokens=1700,
            limit_tokens=2000,
            used_ratio=0.85,
        )
        report = build_escalation_report(
            reason=BUDGET_REASON,
            progress=EscalationProgress(model_turns=2),
            budget=budget,
        )
        self.assertEqual(BUDGET_REASON, report.reason)
        self.assertIn("85%", report.summary)
        self.assertIn("1700", report.summary)

    def test_report_without_limit_has_no_ratio_text(self) -> None:
        report = build_escalation_report(
            reason=BUDGET_REASON,
            progress=EscalationProgress(),
            budget=EscalationBudget(used_tokens=10, limit_tokens=None),
        )
        self.assertIn("预算", report.summary)

    def test_report_is_json_serializable(self) -> None:
        report = self._report(NO_PROGRESS_REASON, memory=_repeating_memory())
        payload = report.as_json()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("summary", encoded)
        self.assertEqual("no_progress", payload["reason"])
        self.assertIn("options", payload)

    def test_will_stop_is_carried_through(self) -> None:
        report = self._report(NO_PROGRESS_REASON, will_stop=True)
        self.assertTrue(report.will_stop)
        self.assertTrue(report.as_json()["willStop"])

    def test_unknown_reason_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self._report("something_else")


if __name__ == "__main__":
    unittest.main()
