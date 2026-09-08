"""C5 成本/延迟可见：把一次运行的"花了多少、用了多久"算清楚。

《Agentic AI Guide》第 27 章把成本管理/延迟优化列为生产关注点；仪表盘要能看到
token 消耗、调用计数、延迟与**成本估算**。

三条原则（沿用 08 §6 的定价约定）：

* **定价来自定价表**（`runtime_ledger.pricing`），不在这里硬编码第二份；
* **未定价的模型只报用量、不给假估算**（``cost_usd=None``）；
* 本模块纯计算，不认识 provider/数据库，耗时也由调用方传入的时间戳决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from endless_task.agent_platform import AgentPlatformError
from endless_task.runtime_ledger.pricing import PricingCatalog

USAGE_COST_PROTOCOL_VERSION = 1


def estimate_cost_usd(
    catalog: PricingCatalog,
    *,
    provider: Optional[str],
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> Optional[float]:
    """按定价表估算一次调用成本；未定价返回 None（绝不猜）。"""
    for value, field_name in (
        (input_tokens, "input_tokens"),
        (output_tokens, "output_tokens"),
        (cached_input_tokens, "cached_input_tokens"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AgentPlatformError(
                "invalid_usage_cost_input",
                f"{field_name} must be a non-negative integer",
            )
    if not provider or not model:
        return None
    entry = catalog.price_for(provider, model)
    if entry is None:
        return None
    return round(
        input_tokens / 1_000_000 * entry.input_per_million
        + output_tokens / 1_000_000 * entry.output_per_million
        + cached_input_tokens / 1_000_000 * entry.cached_input_per_million,
        8,
    )


def cost_cap_exceeded(
    *,
    cost_usd: Optional[float],
    cap_usd: float,
) -> bool:
    """是否触达成本上限（未定价或未配置上限时永不触发）。"""
    if cap_usd is None or cap_usd <= 0:
        return False
    if cost_usd is None:
        return False
    return cost_usd >= cap_usd


def format_cost_usd(cost_usd: Optional[float]) -> str:
    """成本文案：小额保留 4 位，未定价明确标注。"""
    if cost_usd is None:
        return "未定价"
    if cost_usd < 1:
        return f"~${cost_usd:.4f}"
    return f"~${cost_usd:.2f}"


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _duration_ms(
    started_at: Optional[str],
    finished_at: Optional[str],
    *,
    now: Optional[str],
) -> Optional[int]:
    start = _parse_timestamp(started_at)
    if start is None:
        return None
    end = _parse_timestamp(finished_at) or _parse_timestamp(now)
    if end is None:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


@dataclass(frozen=True)
class RunUsageSummary:
    """一次运行的用量/成本/耗时汇总。"""

    run_id: str
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: Optional[float] = None
    cost_priced: bool = False
    unpriced_turns: int = 0
    price_revision: Optional[str] = None
    models: tuple[str, ...] = ()
    duration_ms: Optional[int] = None
    first_token_latency_ms: Optional[int] = None
    schema_version: int = USAGE_COST_PROTOCOL_VERSION

    def as_json(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "turns": self.turns,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "costUsd": self.cost_usd,
            "costPriced": self.cost_priced,
            "unpricedTurns": self.unpriced_turns,
            "priceRevision": self.price_revision,
            "models": list(self.models),
            "durationMs": self.duration_ms,
            "firstTokenLatencyMs": self.first_token_latency_ms,
        }


def build_run_usage_summary(
    *,
    run: object,
    model_turns: Sequence[object],
    catalog: PricingCatalog,
    first_token_latency_ms: Optional[int] = None,
    now: Optional[str] = None,
) -> RunUsageSummary:
    """从 run 与它的模型轮次汇总用量/成本/耗时（纯计算）。"""
    input_tokens = 0
    output_tokens = 0
    cost_total = 0.0
    priced_turns = 0
    unpriced_turns = 0
    models: list[str] = []
    for turn in model_turns:
        turn_input = getattr(turn, "input_tokens", None) or 0
        turn_output = getattr(turn, "output_tokens", None) or 0
        input_tokens += turn_input
        output_tokens += turn_output
        provider = getattr(turn, "provider", None)
        model = getattr(turn, "model", None)
        if model:
            key = f"{provider}/{model}" if provider else str(model)
            if key not in models:
                models.append(key)
        cost = estimate_cost_usd(
            catalog,
            provider=provider,
            model=model,
            input_tokens=turn_input,
            output_tokens=turn_output,
        )
        if cost is None:
            unpriced_turns += 1
        else:
            cost_total += cost
            priced_turns += 1
    return RunUsageSummary(
        run_id=str(getattr(run, "id", "")),
        turns=len(model_turns),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cost_usd=round(cost_total, 8) if priced_turns else None,
        cost_priced=bool(priced_turns) and unpriced_turns == 0,
        unpriced_turns=unpriced_turns,
        price_revision=catalog.revision if priced_turns else None,
        models=tuple(models),
        duration_ms=_duration_ms(
            getattr(run, "started_at", None),
            getattr(run, "finished_at", None),
            now=now,
        ),
        first_token_latency_ms=first_token_latency_ms,
    )


__all__ = [
    "RunUsageSummary",
    "USAGE_COST_PROTOCOL_VERSION",
    "build_run_usage_summary",
    "cost_cap_exceeded",
    "estimate_cost_usd",
    "format_cost_usd",
]
