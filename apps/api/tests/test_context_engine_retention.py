"""AP-203: tool-result retention planning (prune/spill/pair preservation)."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.context_engine import (
    ContextSegment,
    ContextSegmentKind,
    ContextTransform,
    plan_tool_result_retention,
)

INCLUDED = ContextTransform.INCLUDED


def result(
    call_id: str,
    *,
    tokens: int,
    priority: int = 20,
) -> ContextSegment:
    return ContextSegment(
        kind=ContextSegmentKind.TOOL_RESULT,
        source_ids=(call_id,),
        trust_level="tool",
        priority=priority,
        estimated_tokens=tokens,
        transform=INCLUDED,
    )


class ToolResultRetentionTest(unittest.TestCase):
    def test_active_results_are_never_pruned_or_spilled(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_active", tokens=100),),
            active_call_ids={"call_active"},
            max_kept_results=1,
            input_budget=1_000,
        )
        self.assertEqual(1, len(retention.kept))
        self.assertEqual(("call_active",), retention.kept[0].source_ids)
        self.assertEqual((), retention.pruned)
        self.assertEqual((), retention.spilled)
        self.assertEqual((), retention.diagnostics)

    def test_completed_old_results_are_pruned_beyond_limit(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_1", tokens=10), result("call_2", tokens=10), result("call_3", tokens=10)),
            active_call_ids=set(),
            max_kept_results=2,
            input_budget=1_000,
        )
        self.assertEqual(2, len(retention.kept))
        self.assertEqual(["call_2", "call_3"], [s.source_ids[0] for s in retention.kept])
        self.assertEqual(1, len(retention.pruned))
        self.assertEqual("call_1", retention.pruned[0].source_ids[0])
        self.assertEqual(ContextTransform.PRUNED, retention.pruned[0].transform)
        self.assertEqual("tool_result_pruned_old", retention.pruned[0].transform_reason)

    def test_duplicate_source_keeps_only_the_newest(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_1", tokens=10), result("call_1", tokens=99)),
            active_call_ids=set(),
            max_kept_results=5,
            input_budget=1_000,
        )
        self.assertEqual(1, len(retention.kept))
        self.assertEqual(99, retention.kept[0].estimated_tokens)

    def test_completed_result_over_share_is_spilled(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_big", tokens=400),),
            active_call_ids=set(),
            max_kept_results=1,
            input_budget=1_000,
        )
        self.assertEqual((), retention.kept)
        self.assertEqual(1, len(retention.spilled))
        self.assertEqual(ContextTransform.SPILLED, retention.spilled[0].transform)
        self.assertEqual("tool_result_over_share", retention.spilled[0].transform_reason)

    def test_active_result_over_share_is_kept_with_diagnostic(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_active", tokens=400),),
            active_call_ids={"call_active"},
            max_kept_results=1,
            input_budget=1_000,
        )
        self.assertEqual(1, len(retention.kept))
        self.assertEqual(
            ("active_tool_result_over_share",),
            tuple(item.code for item in retention.diagnostics),
        )

    def test_share_threshold_respects_config(self) -> None:
        retention = plan_tool_result_retention(
            (result("call_mid", tokens=200),),
            active_call_ids=set(),
            max_kept_results=1,
            input_budget=1_000,
            max_result_share=0.5,
        )
        self.assertEqual(1, len(retention.kept))

    def test_retention_is_deterministic(self) -> None:
        candidates = (result("call_1", tokens=50), result("call_2", tokens=50), result("call_3", tokens=50))
        first = plan_tool_result_retention(candidates, active_call_ids=set(), max_kept_results=2, input_budget=1_000)
        second = plan_tool_result_retention(candidates, active_call_ids=set(), max_kept_results=2, input_budget=1_000)
        self.assertEqual([s.source_ids[0] for s in first.kept], [s.source_ids[0] for s in second.kept])

    def test_mixed_batch_active_survives_prune_of_completed(self) -> None:
        retention = plan_tool_result_retention(
            (
                result("call_old", tokens=10),
                result("call_active", tokens=10),
                result("call_new", tokens=10),
            ),
            active_call_ids={"call_active"},
            max_kept_results=1,
            input_budget=1_000,
        )
        kept_names = {s.source_ids[0] for s in retention.kept}
        self.assertIn("call_active", kept_names)
        # active is not counted against the completed-result limit
        self.assertGreaterEqual(len(retention.kept), 1)
        self.assertEqual(1, len(retention.pruned))

    def test_input_validation(self) -> None:
        with self.assertRaises(AgentPlatformError):
            plan_tool_result_retention(
                (result("call_1", tokens=10),),
                active_call_ids=set(),
                max_kept_results=0,
                input_budget=1_000,
            )
        with self.assertRaises(AgentPlatformError):
            plan_tool_result_retention(
                (result("call_1", tokens=10),),
                active_call_ids="call_1",
                max_kept_results=1,
                input_budget=1_000,
            )
        with self.assertRaises(AgentPlatformError):
            plan_tool_result_retention(
                (result("call_1", tokens=10),),
                active_call_ids=set(),
                max_kept_results=1,
                input_budget=1_000,
                max_result_share=1.5,
            )
        with self.assertRaises(AgentPlatformError):
            plan_tool_result_retention(
                "not-segments",
                active_call_ids=set(),
                max_kept_results=1,
                input_budget=1_000,
            )


if __name__ == "__main__":
    unittest.main()
