"""B4 反思：从失败/纠正的 Episode 归纳出可复用的洞见（纯逻辑）。"""

from __future__ import annotations

import json
import unittest

from endless_task.runtime_v2.reflection import (
    DEFAULT_INSIGHT_IMPORTANCE,
    ReflectionEpisode,
    derive_insights,
    insight_signature,
    should_reflect,
)


class ShouldReflectTest(unittest.TestCase):
    def test_failed_run_reflects(self) -> None:
        self.assertTrue(should_reflect(run_status="failed"))

    def test_completed_run_without_signals_does_not_reflect(self) -> None:
        self.assertFalse(should_reflect(run_status="completed"))

    def test_escalation_reason_reflects(self) -> None:
        self.assertTrue(
            should_reflect(run_status="completed", escalation_reasons=("no_progress",))
        )

    def test_repeated_failure_reflects(self) -> None:
        self.assertTrue(should_reflect(run_status="completed", failure_streak=2))
        self.assertFalse(should_reflect(run_status="completed", failure_streak=1))


class DeriveInsightsTest(unittest.TestCase):
    def test_repeated_tool_failure_becomes_an_insight(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="tool_failure",
                    tool_name="write_workspace_file",
                    error_code="permission_denied",
                    count=3,
                ),
            )
        )
        self.assertEqual(1, len(insights))
        insight = insights[0]
        self.assertEqual("tool_failure", insight.trigger)
        self.assertIn("write_workspace_file", insight.content)
        self.assertIn("permission_denied", insight.content)
        self.assertIn("不要原样重试", insight.content)
        self.assertIn("反思", insight.reason)
        self.assertEqual(DEFAULT_INSIGHT_IMPORTANCE, insight.importance)

    def test_single_failure_is_not_an_insight(self) -> None:
        self.assertEqual(
            (),
            derive_insights(
                (
                    ReflectionEpisode(
                        kind="tool_failure",
                        tool_name="read_workspace_file",
                        error_code="path_not_found",
                        count=1,
                    ),
                )
            ),
        )

    def test_no_progress_escalation_becomes_an_insight(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="escalation",
                    reason="no_progress",
                    refs={"runId": "run_1"},
                ),
            )
        )
        self.assertEqual(1, len(insights))
        self.assertIn("无进展", insights[0].content)

    def test_verification_failure_becomes_an_insight(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="escalation",
                    reason="verification_failed",
                    refs={"summary": "报告未写入"},
                ),
            )
        )
        self.assertIn("自检", insights[0].content)
        self.assertIn("报告未写入", insights[0].content)

    def test_budget_escalation_becomes_an_insight(self) -> None:
        for reason in ("budget_exhausted", "cost_cap_exceeded"):
            insights = derive_insights(
                (ReflectionEpisode(kind="escalation", reason=reason),)
            )
            self.assertEqual(1, len(insights), reason)
            self.assertIn("预算", insights[0].content)

    def test_run_failure_becomes_an_insight(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="run_failure",
                    error_code="provider_unavailable",
                ),
            )
        )
        self.assertIn("provider_unavailable", insights[0].content)

    def test_duplicate_episodes_merge_into_one_insight(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="tool_failure",
                    tool_name="run_shell",
                    error_code="tool_timeout",
                    count=2,
                ),
                ReflectionEpisode(
                    kind="tool_failure",
                    tool_name="run_shell",
                    error_code="tool_timeout",
                    count=3,
                ),
            )
        )
        self.assertEqual(1, len(insights))
        self.assertIn("5", insights[0].content)

    def test_unknown_episode_is_ignored(self) -> None:
        self.assertEqual((), derive_insights((ReflectionEpisode(kind="mystery"),)))

    def test_insights_are_json_serializable(self) -> None:
        insights = derive_insights(
            (
                ReflectionEpisode(
                    kind="tool_failure",
                    tool_name="run_shell",
                    error_code="tool_timeout",
                    count=2,
                    refs={"toolExecutionId": "tool_1"},
                ),
            )
        )
        payload = [insight.as_json() for insight in insights]
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("content", encoded)
        self.assertEqual("tool_failure", payload[0]["trigger"])


class SignatureTest(unittest.TestCase):
    def test_signature_is_stable_and_order_independent(self) -> None:
        first = (
            ReflectionEpisode(
                kind="tool_failure", tool_name="a", error_code="x", count=2
            ),
            ReflectionEpisode(
                kind="tool_failure", tool_name="b", error_code="y", count=2
            ),
        )
        second = tuple(reversed(first))
        self.assertEqual(insight_signature(first), insight_signature(second))

    def test_signature_changes_with_evidence(self) -> None:
        self.assertNotEqual(
            insight_signature(
                (ReflectionEpisode(kind="tool_failure", tool_name="a", count=2),)
            ),
            insight_signature(
                (ReflectionEpisode(kind="tool_failure", tool_name="b", count=2),)
            ),
        )


if __name__ == "__main__":
    unittest.main()
