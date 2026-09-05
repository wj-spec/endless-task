"""M6 OE-2: versioned pricing catalog + cost computation."""

from __future__ import annotations

import unittest

from endless_task.runtime_ledger.pricing import (
    CostRecord,
    PriceEntry,
    PricingCatalog,
    UsageCategory,
    compute_cost,
    make_default_catalog,
)
from endless_task.runtime_ledger.protocol import CanonicalUsage
from endless_task.runtime_ledger.protocol import TraceContext


def usage(provider: str = "deepseek", model: str = "deepseek-chat") -> CanonicalUsage:
    return CanonicalUsage(
        provider=provider,
        model=model,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cached_input_tokens=1_000_000,
        reasoning_tokens=0,
        request_count=1,
        occurred_at="2026-09-05T00:00:00Z",
        trace=TraceContext(
            trace_id="trace_1",
            run_id="run_1",
            correlation_id="corr_1",
        ),
    )


class PricingCatalogTest(unittest.TestCase):
    def test_default_catalog_covers_known_providers(self) -> None:
        catalog = make_default_catalog()
        self.assertTrue(catalog.price_for("deepseek", "deepseek-chat") is not None)
        self.assertTrue(catalog.price_for("openai", "gpt-4.1-mini") is not None)
        self.assertIsNone(catalog.price_for("deepseek", "unknown-model"))

    def test_catalog_is_versioned(self) -> None:
        catalog = make_default_catalog(revision="2026-09-01")
        self.assertEqual("2026-09-01", catalog.revision)


class CostComputationTest(unittest.TestCase):
    def test_known_model_cost(self) -> None:
        catalog = make_default_catalog()
        record = compute_cost(usage(), catalog)
        self.assertTrue(record.priced)
        self.assertIsNotNone(record.cost_usd)
        self.assertEqual(catalog.revision, record.price_revision)
        # 1M in * 0.27 + 1M out * 1.10 + 1M cached * 0.07 = 1.44
        self.assertAlmostEqual(1.44, record.cost_usd, places=6)

    def test_unknown_model_is_usage_only(self) -> None:
        catalog = make_default_catalog()
        record = compute_cost(
            usage(provider="deepseek", model="future-model"),
            catalog,
        )
        self.assertFalse(record.priced)
        self.assertIsNone(record.cost_usd)
        self.assertIsNone(record.price_revision)
        # Usage is still recorded faithfully.
        self.assertEqual(1_000_000, record.input_tokens)
        self.assertEqual("future-model", record.model)

    def test_category_attached(self) -> None:
        catalog = make_default_catalog()
        record = compute_cost(
            usage(),
            catalog,
            category=UsageCategory.COMPACTION,
            usage_id=7,
        )
        self.assertEqual(UsageCategory.COMPACTION, record.category)
        self.assertEqual(7, record.usage_id)

    def test_zero_usage_zero_cost(self) -> None:
        catalog = make_default_catalog()
        zero = CanonicalUsage(
            provider="deepseek",
            model="deepseek-chat",
            input_tokens=0,
            output_tokens=0,
            cached_input_tokens=0,
            reasoning_tokens=0,
            request_count=0,
            occurred_at="2026-09-05T00:00:00Z",
            trace=usage().trace,
        )
        record = compute_cost(zero, catalog)
        self.assertAlmostEqual(0.0, record.cost_usd, places=6)

    def test_cached_input_priced_separately(self) -> None:
        catalog = PricingCatalog(
            revision="r1",
            prices={
                "p/m": PriceEntry(
                    input_per_million=1.0,
                    output_per_million=2.0,
                    cached_input_per_million=0.5,
                )
            },
        )
        rec = CanonicalUsage(
            provider="p",
            model="m",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cached_input_tokens=1_000_000,
            reasoning_tokens=0,
            request_count=1,
            occurred_at="2026-09-05T00:00:00Z",
            trace=usage().trace,
        )
        record = compute_cost(rec, catalog)
        self.assertAlmostEqual(3.5, record.cost_usd, places=6)

    def test_negative_price_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PriceEntry(input_per_million=-1.0, output_per_million=1.0)


if __name__ == "__main__":
    unittest.main()
