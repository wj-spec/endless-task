"""Deterministic context budgeting and segment planning (M2 AP-202).

Pure planning over the frozen ``ContextBudget``/``ContextSegment`` types
(``protocol.py``): given candidate segments and a budget, decide what enters
the model context. Implementation stays on the protocol layer — no runtime
store, no provider — so the future ``DefaultContextEngine.assemble`` can
consume it without duplicating policy.

Hard rules implemented here (doc 04 §4.3):

1. ``system`` and ``current_user`` segments are mandatory and never dropped;
   if they alone exceed the input budget the plan still keeps them and
   reports ``over_budget_mandatory`` so the caller can spill/compact.
2. Optional segments are kept in deterministic order: higher ``priority``
   first, then a fixed kind order, then original order.
3. Dropped optional segments become ``omitted`` with a budget reason.
4. A fingerprint is derived from messages and planned segments so snapshots
   can be cached/traced without replaying content.

Rules that need runtime semantics (paired tool call/result, checkpoint
exclusivity, single-result share, spill) belong to AP-203/AP-204/wiring and
are not guessed here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    plain_json,
)
from endless_task.runtime.provider import ProviderMessage

from .protocol import (
    CONTEXT_PROTOCOL_VERSION,
    ContextBudget,
    ContextSegment,
    ContextSegmentKind,
    ContextTransform,
)

CONTEXT_PLANNER_SCHEMA_VERSION = 1

#: Segments that must always reach the model.
_MANDATORY_KINDS = frozenset(
    {ContextSegmentKind.SYSTEM, ContextSegmentKind.CURRENT_USER}
)

#: Deterministic fallback order for equal-priority optional segments.
_KIND_ORDER = tuple(
    kind
    for kind in ContextSegmentKind
    if kind not in _MANDATORY_KINDS
)

_CHARS_PER_TOKEN_DEFAULT = 3.0

def estimate_text_tokens(
    text: str,
    *,
    characters_per_token: float = _CHARS_PER_TOKEN_DEFAULT,
) -> int:
    """Deterministic token estimate for planning signals.

    A ~1-token-per-N-characters heuristic for mixed ASCII/CJK text; exact
    provider counts belong to provider-usage calibration (M2/M6). Empty text
    costs nothing; any non-empty text costs at least one token.
    """
    normalized = text if isinstance(text, str) else ""
    if not isinstance(text, str):
        raise AgentPlatformError(
            "invalid_context_value",
            "context_text must be text",
        )
    if not isinstance(characters_per_token, (int, float)) or isinstance(
        characters_per_token, bool
    ):
        raise AgentPlatformError(
            "invalid_context_value",
            "characters_per_token must be numeric",
        )
    ratio = float(characters_per_token)
    if not math.isfinite(ratio) or ratio <= 0:
        raise AgentPlatformError(
            "invalid_context_value",
            "characters_per_token must be positive",
        )
    if not normalized:
        return 0
    return max(1, math.ceil(len(normalized) / ratio))

@dataclass(frozen=True)
class ContextPlan:
    """Result of budgeting candidate segments under a ``ContextBudget``."""

    included: tuple[ContextSegment, ...]
    omitted: tuple[ContextSegment, ...]
    diagnostics: tuple[SafeDiagnostic, ...]
    input_budget: int
    estimated_tokens: int
    over_budget: bool
    schema_version: int = CONTEXT_PLANNER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("included", "omitted"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(segment, ContextSegment) for segment in values
            ):
                raise AgentPlatformError(
                    "invalid_context_plan",
                    f"{field_name} must be a tuple of ContextSegment values",
                )
        if not isinstance(self.over_budget, bool) or not isinstance(
            self.estimated_tokens, int
        ):
            raise AgentPlatformError(
                "invalid_context_plan",
                "Context plan flags and token totals must be integers/booleans",
            )
        if self.estimated_tokens < 0:
            raise AgentPlatformError(
                "invalid_context_plan",
                "Context plan token total cannot be negative",
            )

def plan_context_segments(
    candidates: Sequence[ContextSegment],
    budget: ContextBudget,
) -> ContextPlan:
    """Choose which candidate segments enter the model under ``budget``."""
    if isinstance(candidates, (str, bytes)):
        raise AgentPlatformError(
            "invalid_context_plan",
            "Candidates must be a sequence of ContextSegment values",
        )
    candidate_tuple = tuple(candidates)
    if any(not isinstance(segment, ContextSegment) for segment in candidate_tuple):
        raise AgentPlatformError(
            "invalid_context_plan",
            "Candidates must be ContextSegment values",
        )
    if not isinstance(budget, ContextBudget):
        raise AgentPlatformError(
            "invalid_context_value",
            "Context planning requires a ContextBudget",
        )
    input_budget = budget.input_budget
    mandatory: list[ContextSegment] = []
    optional: list[ContextSegment] = []
    for segment in candidate_tuple:
        # Only raw candidates compete for budget; pre-transformed segments
        # (pruned/spilled/compacted/omitted) never re-enter the model here.
        if segment.transform is not ContextTransform.INCLUDED:
            continue
        if segment.kind in _MANDATORY_KINDS:
            mandatory.append(segment)
        else:
            optional.append(segment)
    kind_rank = {kind.value: index for index, kind in enumerate(_KIND_ORDER)}

    def rank(segment: ContextSegment) -> tuple[int, int, int]:
        return (-segment.priority, kind_rank.get(segment.kind.value, 0), 0)

    ordered_optional = sorted(optional, key=rank)
    included: list[ContextSegment] = list(mandatory)
    omitted: list[ContextSegment] = []
    diagnostics: list[SafeDiagnostic] = []
    mandatory_tokens = sum(segment.estimated_tokens for segment in mandatory)
    if mandatory_tokens > input_budget:
        diagnostics.append(
            SafeDiagnostic(
                code="over_budget_mandatory",
                safe_message=(
                    "系统与当前用户内容超过输入预算，已全部保留；"
                    "需要 spill 或 compact 才能恢复预算余量。"
                ),
                retryable=False,
                details={
                    "mandatory_tokens": mandatory_tokens,
                    "input_budget": input_budget,
                },
            )
        )
    remaining = input_budget - mandatory_tokens
    for segment in ordered_optional:
        if segment.estimated_tokens <= remaining:
            included.append(segment)
            remaining -= segment.estimated_tokens
        else:
            omitted.append(
                replace(
                    segment,
                    transform=ContextTransform.OMITTED,
                    transform_reason="exceeds_input_budget",
                )
            )
    included_tuple = tuple(included)
    total = sum(segment.estimated_tokens for segment in included_tuple)
    return ContextPlan(
        included=included_tuple,
        omitted=tuple(omitted),
        diagnostics=tuple(diagnostics),
        input_budget=input_budget,
        estimated_tokens=total,
        over_budget=mandatory_tokens > input_budget,
    )

def fingerprint_context(
    messages: Iterable[ProviderMessage],
    segments: Iterable[ContextSegment],
    *,
    estimated_tokens: int,
    input_budget: int,
) -> str:
    """Stable sha256 fingerprint of one planned context projection."""
    if not isinstance(estimated_tokens, int) or not isinstance(input_budget, int):
        raise AgentPlatformError(
            "invalid_context_value",
            "Fingerprint token counts must be integers",
        )
    message_rows = [
        {
            "role": message.role,
            "content": message.content,
            "tool_call_ids": [
                call.id for call in getattr(message, "tool_calls", ()) or ()
            ],
        }
        for message in messages
    ]
    segment_rows = [
        {
            "kind": segment.kind.value,
            "source_ids": list(segment.source_ids),
            "transform": segment.transform.value,
            "reason": segment.transform_reason,
            "estimated_tokens": segment.estimated_tokens,
        }
        for segment in sorted(
            segments,
            key=lambda segment: (
                segment.kind.value,
                segment.source_ids,
            ),
        )
    ]
    payload = {
        "protocol_version": CONTEXT_PROTOCOL_VERSION,
        "estimated_tokens": estimated_tokens,
        "input_budget": input_budget,
        "messages": message_rows,
        "segments": segment_rows,
    }
    try:
        canonical = json.dumps(
            plain_json(payload),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise AgentPlatformError(
            "invalid_context_value",
            "Context fingerprint input must be JSON compatible",
        ) from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

__all__ = [
    "CONTEXT_PLANNER_SCHEMA_VERSION",
    "ContextPlan",
    "estimate_text_tokens",
    "fingerprint_context",
    "plan_context_segments",
]
