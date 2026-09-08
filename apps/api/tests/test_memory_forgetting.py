"""B3 重要性加权遗忘 / 间隔重复（纯计算）。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from endless_task.runtime.memory_forgetting import (
    DEFAULT_IMPORTANCE,
    DEFAULT_TAU_DAYS,
    FORGET_THRESHOLD,
    REVIEW_IMPORTANCE,
    clamp_importance,
    forget_probability,
    needs_review,
    next_expiry_days,
    retention_score,
    select_forgettable,
    select_needs_review,
)

NOW = datetime(2026, 1, 31, tzinfo=timezone.utc)


@dataclass
class _Memory:
    id: str
    importance: float = DEFAULT_IMPORTANCE
    access_count: int = 0
    pinned: bool = False
    updated_at: str = "2026-01-31T00:00:00+00:00"


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class ClampImportanceTest(unittest.TestCase):
    def test_clamps_to_unit_interval(self) -> None:
        self.assertEqual(0.0, clamp_importance(-1))
        self.assertEqual(1.0, clamp_importance(5))
        self.assertEqual(0.5, clamp_importance(0.5))

    def test_invalid_values_are_rejected(self) -> None:
        for value in ("x", None, True):
            with self.assertRaises(Exception):
                clamp_importance(value)  # type: ignore[arg-type]


class ForgetProbabilityTest(unittest.TestCase):
    def _p(self, **kwargs) -> float:
        base = dict(
            importance=DEFAULT_IMPORTANCE,
            access_count=0,
            age_days=30,
            tau_days=DEFAULT_TAU_DAYS,
        )
        base.update(kwargs)
        return forget_probability(**base)  # type: ignore[arg-type]

    def test_probability_is_bounded(self) -> None:
        for importance in (0.0, 0.5, 1.0):
            for access in (0, 1, 50):
                for age in (0, 1, 365):
                    value = self._p(
                        importance=importance, access_count=access, age_days=age
                    )
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)

    def test_higher_importance_is_less_likely_forgotten(self) -> None:
        low = self._p(importance=0.1)
        high = self._p(importance=0.9)
        self.assertGreater(low, high)

    def test_more_accesses_are_less_likely_forgotten(self) -> None:
        self.assertGreater(self._p(access_count=0), self._p(access_count=10))

    def test_older_memory_is_more_likely_forgotten(self) -> None:
        self.assertLess(self._p(age_days=1), self._p(age_days=365))

    def test_pinned_memory_never_forgotten(self) -> None:
        self.assertEqual(0.0, self._p(pinned=True, importance=0.0, age_days=9999))

    def test_fresh_memory_probability_is_zero(self) -> None:
        self.assertEqual(0.0, self._p(age_days=0))

    def test_larger_tau_lowers_probability(self) -> None:
        self.assertGreater(
            self._p(tau_days=7),
            self._p(tau_days=365),
        )

    def test_invalid_inputs_rejected(self) -> None:
        for kwargs in (
            {"tau_days": 0},
            {"access_count": -1},
            {"age_days": -1},
        ):
            with self.assertRaises(Exception):
                self._p(**kwargs)


class RetentionScoreTest(unittest.TestCase):
    def test_retention_is_complement_of_forget(self) -> None:
        probability = forget_probability(
            importance=0.3, access_count=2, age_days=45, tau_days=30
        )
        self.assertAlmostEqual(
            1.0 - probability,
            retention_score(
                importance=0.3, access_count=2, age_days=45, tau_days=30
            ),
        )

    def test_pinned_has_full_retention(self) -> None:
        self.assertEqual(
            1.0,
            retention_score(
                importance=0.0, access_count=0, age_days=999, pinned=True
            ),
        )


class SpacedRepetitionTest(unittest.TestCase):
    def test_expiry_grows_with_access_and_importance(self) -> None:
        base = next_expiry_days(importance=0.0, access_count=0, base_days=30)
        more_used = next_expiry_days(importance=0.0, access_count=3, base_days=30)
        more_important = next_expiry_days(
            importance=1.0, access_count=0, base_days=30
        )
        self.assertEqual(30.0, base)
        self.assertGreater(more_used, base)
        self.assertGreater(more_important, base)

    def test_invalid_base_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            next_expiry_days(importance=0.5, access_count=0, base_days=0)


class NeedsReviewTest(unittest.TestCase):
    def test_important_and_stale_memory_needs_review(self) -> None:
        self.assertTrue(
            needs_review(importance=REVIEW_IMPORTANCE, age_days=60, tau_days=30)
        )
        self.assertFalse(
            needs_review(importance=0.1, age_days=60, tau_days=30)
        )
        self.assertFalse(
            needs_review(importance=0.9, age_days=1, tau_days=30)
        )

    def test_pinned_important_memory_needs_no_review(self) -> None:
        self.assertFalse(
            needs_review(importance=0.9, age_days=999, pinned=True)
        )


class SelectForgettableTest(unittest.TestCase):
    def test_selects_low_value_memories_above_threshold(self) -> None:
        records = (
            _Memory("pinned", importance=0.0, pinned=True, updated_at=_iso(999)),
            _Memory("important", importance=0.95, updated_at=_iso(999)),
            _Memory("forgotten", importance=0.0, updated_at=_iso(999)),
            _Memory("fresh", importance=0.0, updated_at=_iso(0)),
        )
        selected = select_forgettable(records, now=NOW)
        ids = [item.record.id for item in selected]
        self.assertIn("forgotten", ids)
        self.assertNotIn("pinned", ids)
        self.assertNotIn("important", ids)
        self.assertNotIn("fresh", ids)

    def test_ordered_by_probability_desc(self) -> None:
        records = (
            _Memory("old", importance=0.0, updated_at=_iso(365)),
            _Memory("older", importance=0.0, access_count=0, updated_at=_iso(1000)),
        )
        selected = select_forgettable(records, now=NOW)
        self.assertEqual(["older", "old"], [item.record.id for item in selected])

    def test_threshold_is_configurable(self) -> None:
        records = (_Memory("x", importance=0.5, updated_at=_iso(30)),)
        self.assertEqual(
            (), select_forgettable(records, now=NOW, threshold=0.99)
        )
        self.assertEqual(
            1, len(select_forgettable(records, now=NOW, threshold=0.0))
        )

    def test_default_threshold_is_half(self) -> None:
        self.assertEqual(0.5, FORGET_THRESHOLD)

    def test_unparseable_timestamp_is_skipped(self) -> None:
        records = (_Memory("bad", updated_at="not-a-date"),)
        self.assertEqual((), select_forgettable(records, now=NOW))


class SelectNeedsReviewTest(unittest.TestCase):
    def test_selects_important_stale_unpinned_memories(self) -> None:
        records = (
            _Memory("important-stale", importance=0.9, updated_at=_iso(90)),
            _Memory("important-fresh", importance=0.9, updated_at=_iso(1)),
            _Memory("unimportant-stale", importance=0.1, updated_at=_iso(90)),
            _Memory("pinned", importance=0.9, pinned=True, updated_at=_iso(90)),
        )
        selected = select_needs_review(records, now=NOW, tau_days=30)
        self.assertEqual(
            ["important-stale"], [item.record.id for item in selected]
        )


if __name__ == "__main__":
    unittest.main()
