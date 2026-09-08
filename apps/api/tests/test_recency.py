"""B1 记忆时间衰减：近期性打分与混合（纯逻辑）。"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone

from endless_task.runtime.recency import (
    DEFAULT_DECAY_TAU_DAYS,
    DEFAULT_RECENCY_WEIGHT,
    blend_score,
    decay_score,
    parse_timestamp,
)

NOW = "2026-01-31T00:00:00+00:00"


def _ago(days: float) -> str:
    base = datetime(2026, 1, 31, tzinfo=timezone.utc)
    return (base - timedelta(days=days)).isoformat()


class ParseTimestampTest(unittest.TestCase):
    def test_parses_iso_with_offset_and_z(self) -> None:
        self.assertIsNotNone(parse_timestamp("2026-01-31T00:00:00+00:00"))
        self.assertIsNotNone(parse_timestamp("2026-01-31T00:00:00Z"))

    def test_naive_timestamp_is_utc(self) -> None:
        parsed = parse_timestamp("2026-01-31T00:00:00")
        assert parsed is not None
        self.assertEqual(timezone.utc, parsed.tzinfo)

    def test_invalid_inputs_return_none(self) -> None:
        for value in (None, "", "   ", "not-a-date", 123, object()):
            self.assertIsNone(parse_timestamp(value))

    def test_datetime_passthrough(self) -> None:
        value = datetime(2026, 1, 31, tzinfo=timezone.utc)
        self.assertEqual(value, parse_timestamp(value))


class DecayScoreTest(unittest.TestCase):
    def test_fresh_memory_scores_one(self) -> None:
        self.assertEqual(1.0, decay_score(now=NOW, timestamp=NOW))

    def test_future_timestamp_is_clamped_to_one(self) -> None:
        self.assertEqual(
            1.0,
            decay_score(now=NOW, timestamp="2026-02-10T00:00:00+00:00"),
        )

    def test_one_tau_elapsed_is_about_exp_minus_one(self) -> None:
        score = decay_score(now=NOW, timestamp=_ago(30), tau_days=30)
        assert score is not None
        self.assertAlmostEqual(math.exp(-1), score, places=6)

    def test_score_decreases_monotonically_with_age(self) -> None:
        scores = [
            decay_score(now=NOW, timestamp=_ago(days), tau_days=30)
            for days in (0, 1, 7, 30, 90, 365)
        ]
        self.assertTrue(all(score is not None for score in scores))
        numeric = [score for score in scores if score is not None]
        self.assertEqual(numeric, sorted(numeric, reverse=True))

    def test_larger_tau_decays_slower(self) -> None:
        fast = decay_score(now=NOW, timestamp=_ago(30), tau_days=7)
        slow = decay_score(now=NOW, timestamp=_ago(30), tau_days=365)
        assert fast is not None and slow is not None
        self.assertLess(fast, slow)

    def test_missing_timestamp_returns_none(self) -> None:
        self.assertIsNone(decay_score(now=NOW, timestamp=None))
        self.assertIsNone(decay_score(now=NOW, timestamp="bogus"))

    def test_invalid_tau_is_rejected(self) -> None:
        for tau in (0, -1, "x", None):
            with self.assertRaises(Exception):
                decay_score(now=NOW, timestamp=NOW, tau_days=tau)  # type: ignore[arg-type]


class BlendScoreTest(unittest.TestCase):
    def test_zero_weight_is_pure_relevance(self) -> None:
        self.assertEqual(
            0.42,
            blend_score(relevance=0.42, recency=1.0, recency_weight=0),
        )
        self.assertEqual(DEFAULT_RECENCY_WEIGHT, 0.0)

    def test_full_weight_is_pure_recency(self) -> None:
        self.assertEqual(
            1.0,
            blend_score(relevance=0.1, recency=1.0, recency_weight=1),
        )

    def test_half_weight_is_average(self) -> None:
        self.assertAlmostEqual(
            0.6,
            blend_score(relevance=0.2, recency=1.0, recency_weight=0.5),
        )

    def test_missing_recency_falls_back_to_relevance(self) -> None:
        self.assertEqual(
            0.42,
            blend_score(relevance=0.42, recency=None, recency_weight=0.8),
        )

    def test_invalid_weight_is_rejected(self) -> None:
        for weight in (-0.1, 1.1, "x"):
            with self.assertRaises(Exception):
                blend_score(
                    relevance=0.5,
                    recency=0.5,
                    recency_weight=weight,  # type: ignore[arg-type]
                )

    def test_default_tau_is_thirty_days(self) -> None:
        self.assertEqual(30.0, DEFAULT_DECAY_TAU_DAYS)


if __name__ == "__main__":
    unittest.main()
