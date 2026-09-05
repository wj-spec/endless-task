"""Versioned pricing catalog and cost computation (M6 OE-2).

08 §6: pricing is separated from the runtime; the catalog is versioned
data; every cost record keeps the price revision that produced it;
unknown models record usage only, never a guessed price; auxiliary calls
(compaction/embedding/title/review) are classified separately.

This module is data-driven and pure:

- :class:`PriceEntry` pins per-model token prices for one revision,
- :class:`PricingCatalog` is an immutable-by-revision map, versioned,
- :func:`compute_cost` returns a :class:`CostRecord` for one
  :class:`CanonicalUsage`; unknown models yield a record with no price
  fields (usage-only), never a fabricated estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Tuple

from .protocol import CanonicalUsage


class UsageCategory(str, Enum):
    PRIMARY = "primary"
    COMPACTION = "compaction"
    EMBEDDING = "embedding"
    TITLE = "title"
    REVIEW = "review"


@dataclass(frozen=True)
class PriceEntry:
    """Per-model token prices in USD per 1M tokens for one revision."""

    input_per_million: float
    output_per_million: float
    cached_input_per_million: float = 0.0

    def __post_init__(self) -> None:
        for value in (
            self.input_per_million,
            self.output_per_million,
            self.cached_input_per_million,
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError("prices must be non-negative numbers")


@dataclass(frozen=True)
class PricingCatalog:
    """Versioned map from provider/model to price entry."""

    revision: str
    prices: Mapping[str, PriceEntry]  # key: "provider/model"

    def __post_init__(self) -> None:
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("pricing revision must be non-empty")

    def price_for(self, provider: str, model: str) -> Optional[PriceEntry]:
        return self.prices.get(f"{provider}/{model}")

    def entry_count(self) -> int:
        return len(self.prices)


@dataclass(frozen=True)
class CostRecord:
    """Cost outcome for one usage record (may be usage-only)."""

    provider: str
    model: str
    usage_id: Optional[int]
    price_revision: Optional[str]
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    cost_usd: Optional[float]
    category: UsageCategory

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None


def compute_cost(
    usage: CanonicalUsage,
    catalog: PricingCatalog,
    *,
    category: UsageCategory = UsageCategory.PRIMARY,
    usage_id: Optional[int] = None,
) -> CostRecord:
    """Compute one cost record; unknown models stay usage-only."""
    entry = catalog.price_for(usage.provider, usage.model)
    if entry is None:
        return CostRecord(
            provider=usage.provider,
            model=usage.model,
            usage_id=usage_id,
            price_revision=None,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cost_usd=None,
            category=category,
        )
    input_cost = usage.input_tokens / 1_000_000 * entry.input_per_million
    output_cost = usage.output_tokens / 1_000_000 * entry.output_per_million
    cached_cost = usage.cached_input_tokens / 1_000_000 * entry.cached_input_per_million
    return CostRecord(
        provider=usage.provider,
        model=usage.model,
        usage_id=usage_id,
        price_revision=catalog.revision,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cost_usd=round(input_cost + output_cost + cached_cost, 8),
        category=category,
    )


def make_default_catalog(revision: str = "2026-09-05") -> PricingCatalog:
    """Built-in catalog calibrated to the providers this repo talks to.

    Prices are the public list prices at the catalog date; usage-only
    handling covers anything not listed, so an unlisted model never gets a
    fabricated estimate (08 §6).
    """
    return PricingCatalog(
        revision=revision,
        prices={
            "deepseek/deepseek-chat": PriceEntry(
                input_per_million=0.27,
                output_per_million=1.10,
                cached_input_per_million=0.07,
            ),
            "openai/gpt-4.1-mini": PriceEntry(
                input_per_million=0.40,
                output_per_million=1.60,
                cached_input_per_million=0.10,
            ),
        },
    )


__all__ = [
    "CostRecord",
    "PriceEntry",
    "PricingCatalog",
    "UsageCategory",
    "compute_cost",
    "make_default_catalog",
]
