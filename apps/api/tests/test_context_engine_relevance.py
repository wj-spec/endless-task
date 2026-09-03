"""AP-205: memory relevance ranking (scorer/rank/search boundary)."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.context_engine import (
    MemoryCandidate,
    WeightedMemoryScorer,
    memory_search,
    rank_memory_candidates,
    tokenize,
)


def candidate(
    candidate_id: str,
    text: str,
    **overrides,
) -> MemoryCandidate:
    values = {"scope_weight": 0.0, "recency_weight": 0.0, "confirmed": False}
    values.update(overrides)
    return MemoryCandidate(candidate_id=candidate_id, text=text, **values)


class TokenizeTest(unittest.TestCase):
    def test_tokenize_lowercases_and_splits(self) -> None:
        self.assertEqual(
            frozenset({"user", "prefers", "python"}),
            tokenize("User prefers Python!"),
        )
        self.assertEqual(frozenset(), tokenize(""))

    def test_tokenize_keeps_cjk_chunks(self) -> None:
        self.assertIn("开发", tokenize("推进 开发"))


class WeightedScorerTest(unittest.TestCase):
    def test_lexical_coverage_dominates_ordering(self) -> None:
        scorer = WeightedMemoryScorer()
        terms = tokenize("file write plan")
        matching = candidate("match", "user asked to write a file and keep a plan")
        unrelated = candidate("other", "weather in beijing tomorrow")
        self.assertGreater(scorer.score(terms, matching), scorer.score(terms, unrelated))

    def test_signals_follow_doc_formula(self) -> None:
        scorer = WeightedMemoryScorer()
        terms = tokenize("unrelated")
        base = candidate("base", "unrelated text")
        boosted = candidate(
            "boosted",
            "unrelated text",
            scope_weight=0.5,
            recency_weight=0.3,
            confirmed=True,
        )
        penalized = candidate(
            "penalized",
            "unrelated text",
            conflict_penalty=0.5,
            token_cost_penalty=0.5,
        )
        base_score = scorer.score(terms, base)
        self.assertGreater(scorer.score(terms, boosted), base_score)
        self.assertLess(scorer.score(terms, penalized), base_score)

    def test_invalid_candidate_fields_fail_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            candidate("bad", "x", conflict_penalty=-1)
        with self.assertRaises(AgentPlatformError):
            candidate("bad", "x", confirmed="yes")
        with self.assertRaises(AgentPlatformError):
            candidate("", "x")


class RankTest(unittest.TestCase):
    def test_rank_orders_by_score_then_id(self) -> None:
        candidates = (
            candidate("a", "alpha file"),
            candidate("b", "write file plan"),
            candidate("c", "write file plan"),
        )
        hits = rank_memory_candidates("write file plan", candidates)
        self.assertEqual(["b", "c", "a"], [hit.candidate_id for hit in hits])
        self.assertGreater(hits[0].score, hits[-1].score)

    def test_top_k_slices_deterministically(self) -> None:
        candidates = tuple(
            candidate(str(index), "common topic alpha") for index in range(10)
        )
        hits = rank_memory_candidates("common topic", candidates, top_k=3)
        self.assertEqual(3, len(hits))
        self.assertEqual(["0", "1", "2"], [hit.candidate_id for hit in hits])

    def test_min_score_filters_irrelevant(self) -> None:
        hits = rank_memory_candidates(
            "python",
            (candidate("irrelevant", "weather"),),
            min_score=0.0,
        )
        self.assertEqual((), hits)

    def test_validation(self) -> None:
        with self.assertRaises(AgentPlatformError):
            rank_memory_candidates("", ())
        with self.assertRaises(AgentPlatformError):
            rank_memory_candidates("q", (object(),))
        with self.assertRaises(AgentPlatformError):
            rank_memory_candidates("q", (), top_k=0)
        with self.assertRaises(AgentPlatformError):
            rank_memory_candidates("q" * 600, ())


class MemorySearchBoundaryTest(unittest.TestCase):
    def test_search_uses_provider_candidates_only(self) -> None:
        def provider() -> tuple[MemoryCandidate, ...]:
            return (
                candidate("mem_1", "user prefers concise replies"),
                candidate("mem_2", "user dislikes verbose output"),
            )

        hits = memory_search(
            "concise replies",
            provider,
            scorer=WeightedMemoryScorer(),
            top_k=5,
        )
        self.assertEqual(["mem_1"], [hit.candidate_id for hit in hits])

    def test_provider_failure_is_structured(self) -> None:
        def broken() -> tuple[MemoryCandidate, ...]:
            raise RuntimeError("store down")

        with self.assertRaises(AgentPlatformError) as caught:
            memory_search("query", broken)
        self.assertEqual("memory_search_unavailable", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
