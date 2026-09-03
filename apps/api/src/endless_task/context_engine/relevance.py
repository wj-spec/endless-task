"""Memory relevance scoring and ranking (M2 AP-205).

Doc 04 §4.6 models memory retrieval as candidate recall + ranking:

    score = semantic_relevance + lexical_relevance + scope_weight
          + recency_weight + confirmed_weight - conflict_penalty
          - token_cost_penalty

and requires that relevance ranking never widens the visible scope. This
module provides the pure protocol layer:

- :class:`MemoryCandidate` carries the text plus the numeric signals the
  runtime can attach (scope/recency weights, confirmation, penalties),
- :class:`MemoryScorer` is a pluggable scorer protocol; the default
  :class:`WeightedMemoryScorer` implements the doc formula with a
  deterministic lexical relevance term (semantic/embedding relevance is
  injected later through the same protocol),
- ``rank_memory_candidates`` returns stable, top-k ordered hits,
- ``memory_search`` is the protocol-layer search boundary fed by a caller
  that has already applied capability/scope filtering — the provider is the
  only source of candidates, so widening scope via arguments is impossible.

Scope filtering itself is a caller obligation (e.g. the capability mask from
the tool platform); this module never imports runtime memory stores.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)

MEMORY_RELEVANCE_SCHEMA_VERSION = 1
_MAX_QUERY_LENGTH = 500
_MAX_CANDIDATE_TEXT_LENGTH = 20_000
_CONFIRMED_BASE_WEIGHT = 1.0

_TOKEN_SPLIT = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    text: str
    scope_weight: float = 0.0
    recency_weight: float = 0.0
    confirmed: bool = False
    conflict_penalty: float = 0.0
    token_cost_penalty: float = 0.0
    schema_version: int = MEMORY_RELEVANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=MEMORY_RELEVANCE_SCHEMA_VERSION,
            protocol="memory_candidate",
        )
        object.__setattr__(
            self,
            "candidate_id",
            require_identifier(
                self.candidate_id,
                field_name="candidate_id",
                max_length=256,
            ),
        )
        object.__setattr__(
            self,
            "text",
            require_text(
                self.text,
                field_name="candidate_text",
                max_length=_MAX_CANDIDATE_TEXT_LENGTH,
            ),
        )
        for field_name in (
            "scope_weight",
            "recency_weight",
            "conflict_penalty",
            "token_cost_penalty",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AgentPlatformError(
                    "invalid_memory_candidate",
                    f"{field_name} must be numeric",
                )
            normalized = float(value)
            if not _finite(normalized) or normalized < 0:
                raise AgentPlatformError(
                    "invalid_memory_candidate",
                    f"{field_name} must be a finite non-negative number",
                )
            object.__setattr__(self, field_name, normalized)
        if not isinstance(self.confirmed, bool):
            raise AgentPlatformError(
                "invalid_memory_candidate",
                "confirmed must be boolean",
            )


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def tokenize(text: str) -> frozenset[str]:
    """Lowercased word tokens; CJK runs are treated as contiguous chunks."""
    tokens = [
        token.casefold()
        for token in _TOKEN_SPLIT.split(text)
        if token
    ]
    return frozenset(tokens)


class MemoryScorer(Protocol):
    def score(
        self,
        query_terms: frozenset[str],
        candidate: MemoryCandidate,
    ) -> float: ...


class WeightedMemoryScorer:
    """Default scorer following the doc 04 §4.6 formula.

    Lexical relevance = coverage of query terms in the candidate text
    (0..1), plus the candidate signals. Semantic relevance is zero in this
    deterministic default; wiring injects an embedding-backed scorer through
    the same protocol.
    """

    def __init__(self, *, confirmed_base_weight: float = _CONFIRMED_BASE_WEIGHT) -> None:
        if isinstance(confirmed_base_weight, bool) or not isinstance(
            confirmed_base_weight, (int, float)
        ):
            raise AgentPlatformError(
                "invalid_scorer_config",
                "confirmed_base_weight must be numeric",
            )
        self._confirmed = float(confirmed_base_weight)

    def score(
        self,
        query_terms: frozenset[str],
        candidate: MemoryCandidate,
    ) -> float:
        if not isinstance(query_terms, frozenset):
            raise AgentPlatformError(
                "invalid_scorer_input",
                "query_terms must be a frozenset of tokens",
            )
        if not isinstance(candidate, MemoryCandidate):
            raise AgentPlatformError(
                "invalid_scorer_input",
                "Memory scorer requires a MemoryCandidate",
            )
        candidate_terms = tokenize(candidate.text)
        if not query_terms:
            return 0.0
        overlap = len(query_terms & candidate_terms)
        lexical = overlap / len(query_terms)
        confirmed = self._confirmed if candidate.confirmed else 0.0
        return (
            lexical
            + candidate.scope_weight
            + candidate.recency_weight
            + confirmed
            - candidate.conflict_penalty
            - candidate.token_cost_penalty
        )


@dataclass(frozen=True)
class RankedMemoryHit:
    candidate_id: str
    score: float
    schema_version: int = MEMORY_RELEVANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=MEMORY_RELEVANCE_SCHEMA_VERSION,
            protocol="ranked_memory_hit",
        )
        object.__setattr__(
            self,
            "candidate_id",
            require_identifier(
                self.candidate_id,
                field_name="candidate_id",
                max_length=256,
            ),
        )
        if not _finite(float(self.score)):
            raise AgentPlatformError(
                "invalid_memory_hit",
                "Memory hit score must be finite",
            )


def rank_memory_candidates(
    query: str,
    candidates: Sequence[MemoryCandidate],
    *,
    scorer: Optional[MemoryScorer] = None,
    top_k: int = 20,
    min_score: float = 0.0,
) -> tuple[RankedMemoryHit, ...]:
    """Rank candidates by relevance; returns stable top-k hits (score>0)."""
    normalized_query = require_text(
        query,
        field_name="query",
        max_length=_MAX_QUERY_LENGTH,
    )
    if isinstance(candidates, (str, bytes)):
        raise AgentPlatformError(
            "invalid_memory_query",
            "candidates must be a sequence of MemoryCandidate values",
        )
    candidate_tuple = tuple(candidates)
    if any(not isinstance(candidate, MemoryCandidate) for candidate in candidate_tuple):
        raise AgentPlatformError(
            "invalid_memory_query",
            "candidates must be MemoryCandidate values",
        )
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise AgentPlatformError(
            "invalid_memory_query",
            "top_k must be a positive integer",
        )
    if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise AgentPlatformError(
            "invalid_memory_query",
            "min_score must be numeric",
        )
    resolver = scorer if scorer is not None else WeightedMemoryScorer()
    if not callable(getattr(resolver, "score", None)):
        raise AgentPlatformError(
            "invalid_memory_query",
            "Memory ranking requires a MemoryScorer implementation",
        )
    query_terms = tokenize(normalized_query)
    scored: list[RankedMemoryHit] = []
    for candidate in candidate_tuple:
        score = float(resolver.score(query_terms, candidate))
        if not _finite(score):
            raise AgentPlatformError(
                "invalid_memory_query",
                "Memory scorer returned a non-finite score",
            )
        if score > float(min_score):
            scored.append(RankedMemoryHit(candidate_id=candidate.candidate_id, score=score))
    scored.sort(key=lambda hit: (-hit.score, hit.candidate_id))
    return tuple(scored[:top_k])


def memory_search(
    query: str,
    candidates_provider: Callable[[], Sequence[MemoryCandidate]],
    *,
    scorer: Optional[MemoryScorer] = None,
    top_k: int = 20,
) -> tuple[RankedMemoryHit, ...]:
    """Protocol-layer ``search_memory`` boundary.

    ``candidates_provider`` must already apply capability/scope filtering —
    it is the only source of candidates, so no query argument can widen the
    visible scope.
    """
    if not callable(candidates_provider):
        raise AgentPlatformError(
            "invalid_memory_query",
            "memory_search requires a candidates provider",
        )
    try:
        candidates = candidates_provider()
    except Exception as error:
        raise AgentPlatformError(
            "memory_search_unavailable",
            "当前记忆候选源不可用，无法检索。",
            retryable=True,
            details={"error_type": type(error).__name__},
        ) from error
    return rank_memory_candidates(
        query,
        candidates,
        scorer=scorer,
        top_k=top_k,
    )


__all__ = [
    "MEMORY_RELEVANCE_SCHEMA_VERSION",
    "MemoryCandidate",
    "MemoryScorer",
    "RankedMemoryHit",
    "WeightedMemoryScorer",
    "memory_search",
    "rank_memory_candidates",
    "tokenize",
]
