"""C5 成本/延迟可见：单 run 用量与成本估算（纯逻辑）。"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Optional

from endless_task.runtime_ledger.pricing import make_default_catalog
from endless_task.runtime_v2.usage_cost import (
    build_run_usage_summary,
    cost_cap_exceeded,
    estimate_cost_usd,
    format_cost_usd,
)

CATALOG = make_default_catalog()


@dataclass
class _Turn:
    provider: Optional[str] = "deepseek"
    model: Optional[str] = "deepseek-chat"
    input_tokens: Optional[int] = 1000
    output_tokens: Optional[int] = 500
    started_at: Optional[str] = "2026-01-01T00:00:00+00:00"
    finished_at: Optional[str] = "2026-01-01T00:00:02+00:00"


@dataclass
class _Run:
    id: str = "run_1"
    started_at: Optional[str] = "2026-01-01T00:00:00+00:00"
    finished_at: Optional[str] = "2026-01-01T00:00:08+00:00"


class EstimateCostTest(unittest.TestCase):
    def test_known_model_is_priced(self) -> None:
        cost = estimate_cost_usd(
            CATALOG,
            provider="deepseek",
            model="deepseek-chat",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        )
        self.assertIsNotNone(cost)
        assert cost is not None
        self.assertAlmostEqual(0.27 + 1.10, cost, places=8)

    def test_unknown_model_is_not_priced(self) -> None:
        self.assertIsNone(
            estimate_cost_usd(
                CATALOG,
                provider="deepseek",
                model="some-unlisted-model",
                input_tokens=1000,
                output_tokens=1000,
            )
        )

    def test_missing_identity_is_not_priced(self) -> None:
        self.assertIsNone(
            estimate_cost_usd(
                CATALOG,
                provider=None,
                model=None,
                input_tokens=10,
                output_tokens=10,
            )
        )

    def test_negative_tokens_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            estimate_cost_usd(
                CATALOG,
                provider="deepseek",
                model="deepseek-chat",
                input_tokens=-1,
                output_tokens=0,
            )


class RunUsageSummaryTest(unittest.TestCase):
    def test_totals_turns_and_models(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(),
            model_turns=(
                _Turn(),
                _Turn(input_tokens=2000, output_tokens=0),
            ),
            catalog=CATALOG,
        )
        self.assertEqual(2, summary.turns)
        self.assertEqual(3000, summary.input_tokens)
        self.assertEqual(500, summary.output_tokens)
        self.assertEqual(3500, summary.total_tokens)
        self.assertEqual(("deepseek/deepseek-chat",), summary.models)
        self.assertTrue(summary.cost_priced)
        self.assertEqual(0, summary.unpriced_turns)
        assert summary.cost_usd is not None
        expected = (3000 / 1_000_000 * 0.27) + (500 / 1_000_000 * 1.10)
        self.assertAlmostEqual(expected, summary.cost_usd, places=8)
        self.assertEqual("2026-09-05", summary.price_revision)
        self.assertEqual(8000, summary.duration_ms)

    def test_unpriced_model_keeps_usage_only(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(),
            model_turns=(_Turn(model="unlisted-model"),),
            catalog=CATALOG,
        )
        self.assertIsNone(summary.cost_usd)
        self.assertFalse(summary.cost_priced)
        self.assertEqual(1, summary.unpriced_turns)

    def test_mixed_pricing_reports_partial(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(),
            model_turns=(
                _Turn(),
                _Turn(model="unlisted-model", input_tokens=10, output_tokens=10),
            ),
            catalog=CATALOG,
        )
        self.assertFalse(summary.cost_priced)
        self.assertEqual(1, summary.unpriced_turns)
        self.assertIsNotNone(summary.cost_usd)

    def test_running_run_duration_uses_now(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(finished_at=None),
            model_turns=(_Turn(),),
            catalog=CATALOG,
            now="2026-01-01T00:00:05+00:00",
        )
        self.assertEqual(5000, summary.duration_ms)

    def test_first_token_latency_is_carried_through(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(),
            model_turns=(_Turn(),),
            catalog=CATALOG,
            first_token_latency_ms=321,
        )
        self.assertEqual(321, summary.first_token_latency_ms)

    def test_empty_run_has_zero_usage(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(finished_at=None),
            model_turns=(),
            catalog=CATALOG,
            now="2026-01-01T00:00:01+00:00",
        )
        self.assertEqual(0, summary.total_tokens)
        self.assertIsNone(summary.cost_usd)
        self.assertFalse(summary.cost_priced)

    def test_json_round_trip(self) -> None:
        summary = build_run_usage_summary(
            run=_Run(),
            model_turns=(_Turn(),),
            catalog=CATALOG,
        )
        payload = summary.as_json()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("totalTokens", encoded)
        self.assertEqual("run_1", payload["runId"])
        self.assertEqual(1500, payload["totalTokens"])
        self.assertTrue(payload["costPriced"])


class CostCapTest(unittest.TestCase):
    def test_cap_disabled_when_zero_or_negative(self) -> None:
        self.assertFalse(cost_cap_exceeded(cost_usd=1.0, cap_usd=0))
        self.assertFalse(cost_cap_exceeded(cost_usd=1.0, cap_usd=-1))

    def test_cap_compares_estimate(self) -> None:
        self.assertFalse(cost_cap_exceeded(cost_usd=0.04, cap_usd=0.05))
        self.assertTrue(cost_cap_exceeded(cost_usd=0.05, cap_usd=0.05))
        self.assertTrue(cost_cap_exceeded(cost_usd=0.06, cap_usd=0.05))

    def test_unpriced_cost_never_exceeds_cap(self) -> None:
        self.assertFalse(cost_cap_exceeded(cost_usd=None, cap_usd=0.01))


class FormatCostTest(unittest.TestCase):
    def test_formats_small_amounts(self) -> None:
        self.assertEqual("~$0.0040", format_cost_usd(0.004))
        self.assertEqual("~$1.23", format_cost_usd(1.2345))

    def test_unpriced_is_marked(self) -> None:
        self.assertEqual("未定价", format_cost_usd(None))


if __name__ == "__main__":
    unittest.main()
