"""AP-202: deterministic context budget planning over segments."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.context_engine import (
    ContextBudget,
    ContextSegment,
    ContextSegmentKind,
    ContextTransform,
    estimate_text_tokens,
    fingerprint_context,
    plan_context_segments,
)
from endless_task.runtime.provider import ProviderMessage

MESSAGE_SYSTEM = ProviderMessage(role="system", content="You are Endless Task.")
MESSAGE_USER = ProviderMessage(role="user", content="请总结进展。")


def segment(
    kind: ContextSegmentKind,
    *,
    tokens: int,
    priority: int,
    source_id: str = "src_1",
    transform: ContextTransform = ContextTransform.INCLUDED,
) -> ContextSegment:
    return ContextSegment(
        kind=kind,
        source_ids=(source_id,),
        trust_level="trusted",
        priority=priority,
        estimated_tokens=tokens,
        transform=transform,
        transform_reason=None if transform is ContextTransform.INCLUDED else "carrier",
    )


def budget(
    *,
    window: int = 1_000,
    reserved: int = 200,
    safety: int = 50,
) -> ContextBudget:
    return ContextBudget(
        window_tokens=window,
        reserved_output_tokens=reserved,
        safety_margin_tokens=safety,
    )


class TextEstimateTest(unittest.TestCase):
    def test_estimate_is_deterministic_and_positive(self) -> None:
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertEqual(estimate_text_tokens("a"), 1)
        first = estimate_text_tokens("hello world " * 100)
        second = estimate_text_tokens("hello world " * 100)
        self.assertEqual(first, second)
        self.assertGreater(first, 100)

    def test_estimate_is_monotone_in_text_length(self) -> None:
        short = estimate_text_tokens("abc" * 10)
        long = estimate_text_tokens("abc" * 100)
        self.assertGreater(long, short)

    def test_invalid_ratio_fails_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            estimate_text_tokens("text", characters_per_token=0)
        with self.assertRaises(AgentPlatformError):
            estimate_text_tokens("text", characters_per_token=-1)


class ContextPlannerTest(unittest.TestCase):
    def test_keeps_everything_within_a_generous_budget(self) -> None:
        candidates = (
            segment(ContextSegmentKind.SYSTEM, tokens=50, priority=100),
            segment(ContextSegmentKind.CURRENT_USER, tokens=30, priority=100),
            segment(ContextSegmentKind.RECENT_HISTORY, tokens=120, priority=40),
            segment(ContextSegmentKind.TOOL_RESULT, tokens=200, priority=20),
            segment(ContextSegmentKind.MEMORY, tokens=90, priority=60),
        )
        plan = plan_context_segments(candidates, budget())
        self.assertEqual(5, len(plan.included))
        self.assertEqual((), plan.omitted)
        self.assertFalse(plan.over_budget)
        self.assertEqual(490, plan.estimated_tokens)

    def test_mandatory_system_and_user_are_never_dropped(self) -> None:
        tight = budget(window=200, reserved=10, safety=10)
        plan = plan_context_segments(
            (
                segment(ContextSegmentKind.SYSTEM, tokens=60, priority=100),
                segment(ContextSegmentKind.CURRENT_USER, tokens=50, priority=100),
                segment(ContextSegmentKind.RECENT_HISTORY, tokens=500, priority=40),
            ),
            tight,
        )
        kinds = {item.kind for item in plan.included}
        self.assertIn(ContextSegmentKind.SYSTEM, kinds)
        self.assertIn(ContextSegmentKind.CURRENT_USER, kinds)
        self.assertNotIn(ContextSegmentKind.RECENT_HISTORY, kinds)
        self.assertFalse(plan.over_budget)

    def test_over_budget_mandatory_is_kept_with_diagnostic(self) -> None:
        tiny = budget(window=90, reserved=10, safety=10)
        plan = plan_context_segments(
            (
                segment(ContextSegmentKind.SYSTEM, tokens=80, priority=100),
                segment(ContextSegmentKind.CURRENT_USER, tokens=20, priority=100),
            ),
            tiny,
        )
        self.assertTrue(plan.over_budget)
        self.assertEqual(2, len(plan.included))
        self.assertEqual(
            ("over_budget_mandatory",),
            tuple(item.code for item in plan.diagnostics),
        )

    def test_optional_kept_by_priority_then_kind_order(self) -> None:
        tight = budget(window=300, reserved=10, safety=10)
        high = segment(ContextSegmentKind.RECENT_HISTORY, tokens=100, priority=50)
        low = segment(ContextSegmentKind.MEMORY, tokens=250, priority=1)
        plan = plan_context_segments((low, high), tight)
        self.assertEqual([ContextSegmentKind.RECENT_HISTORY], [s.kind for s in plan.included])
        self.assertEqual(1, len(plan.omitted))
        self.assertEqual(ContextSegmentKind.MEMORY, plan.omitted[0].kind)
        self.assertEqual(ContextTransform.OMITTED, plan.omitted[0].transform)
        self.assertEqual("exceeds_input_budget", plan.omitted[0].transform_reason)

    def test_pre_transformed_segments_never_reenter(self) -> None:
        candidates = (
            segment(ContextSegmentKind.TOOL_RESULT, tokens=10, priority=100),
            segment(
                ContextSegmentKind.TOOL_RESULT,
                tokens=10,
                priority=100,
                source_id="old_pruned",
                transform=ContextTransform.PRUNED,
            ),
            segment(
                ContextSegmentKind.CHECKPOINT,
                tokens=10,
                priority=100,
                transform=ContextTransform.COMPACTED,
            ),
        )
        plan = plan_context_segments(candidates, budget())
        self.assertEqual(1, len(plan.included))
        self.assertEqual("src_1", plan.included[0].source_ids[0])

    def test_planning_is_deterministic(self) -> None:
        candidates = (
            segment(ContextSegmentKind.MEMORY, tokens=120, priority=1),
            segment(ContextSegmentKind.TOOL_RESULT, tokens=90, priority=30),
            segment(ContextSegmentKind.RECENT_HISTORY, tokens=60, priority=50),
        )
        first = plan_context_segments(candidates, budget(window=300, reserved=10, safety=10))
        second = plan_context_segments(candidates, budget(window=300, reserved=10, safety=10))
        self.assertEqual(
            [item.source_ids for item in first.included],
            [item.source_ids for item in second.included],
        )
        self.assertEqual(first.estimated_tokens, second.estimated_tokens)

    def test_plan_validates_inputs(self) -> None:
        with self.assertRaises(AgentPlatformError):
            plan_context_segments("not-segments", budget())
        with self.assertRaises(AgentPlatformError):
            plan_context_segments((object(),), budget())
        with self.assertRaises(AgentPlatformError):
            plan_context_segments((), "not-a-budget")


class ContextFingerprintTest(unittest.TestCase):
    def test_fingerprint_is_stable_and_content_sensitive(self) -> None:
        segments = (
            segment(ContextSegmentKind.SYSTEM, tokens=50, priority=100),
            segment(ContextSegmentKind.RECENT_HISTORY, tokens=120, priority=40),
        )
        messages = (MESSAGE_SYSTEM, MESSAGE_USER)
        first = fingerprint_context(
            messages, segments, estimated_tokens=170, input_budget=750
        )
        second = fingerprint_context(
            messages, segments, estimated_tokens=170, input_budget=750
        )
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))
        changed = fingerprint_context(
            messages, segments, estimated_tokens=171, input_budget=750
        )
        self.assertNotEqual(first, changed)
        changed_user = fingerprint_context(
            (MESSAGE_SYSTEM, ProviderMessage(role="user", content="不同内容")),
            segments,
            estimated_tokens=170,
            input_budget=750,
        )
        self.assertNotEqual(first, changed_user)


if __name__ == "__main__":
    unittest.main()
