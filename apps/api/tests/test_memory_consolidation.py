"""B2 记忆巩固：聚类与合并（纯逻辑）。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from endless_task.runtime.memory_consolidation import (
    DEFAULT_MAX_CLUSTER_SIZE,
    DEFAULT_MIN_CLUSTER_SIZE,
    DEFAULT_SIMILARITY_THRESHOLD,
    MemoryCluster,
    cluster_memories,
    cluster_signature,
    merge_contents,
    similarity,
)


@dataclass
class _Memory:
    id: str
    content: str
    kind: str = "fact"
    source_conversation_id: str = "conv_1"
    source_turn_id: str = "turn_1"
    importance: float = 0.5
    access_count: int = 0
    pinned: bool = False


class SimilarityTest(unittest.TestCase):
    def test_identical_texts_score_one(self) -> None:
        self.assertAlmostEqual(
            1.0, similarity("用户偏好周五发布", "用户偏好周五发布")
        )

    def test_unrelated_texts_score_zero(self) -> None:
        self.assertEqual(
            0.0, similarity("用户偏好周五发布", "天气预报明天有雨")
        )

    def test_partial_overlap_is_between_zero_and_one(self) -> None:
        score = similarity(
            "用户偏好周五发布，且喜欢简洁的回答",
            "用户偏好周五发布，并且讨厌冗长的解释",
        )
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_empty_text_scores_zero(self) -> None:
        self.assertEqual(0.0, similarity("", "任何内容"))
        self.assertEqual(0.0, similarity("任何内容", "   "))


class ClusterMemoriesTest(unittest.TestCase):
    def test_similar_same_kind_memories_cluster(self) -> None:
        records = (
            _Memory("m1", "用户偏好周五发布版本"),
            _Memory("m2", "用户偏好周五发布新版本"),
            _Memory("m3", "天气预报明天有雨"),
        )
        clusters = cluster_memories(records, threshold=0.5)
        self.assertEqual(1, len(clusters))
        self.assertEqual(("m1", "m2"), clusters[0].memory_ids)

    def test_different_kinds_never_mix(self) -> None:
        records = (
            _Memory("m1", "用户偏好周五发布版本", kind="preference"),
            _Memory("m2", "用户偏好周五发布新版本", kind="fact"),
        )
        self.assertEqual((), cluster_memories(records, threshold=0.5))

    def test_singletons_are_dropped(self) -> None:
        records = (_Memory("m1", "孤零零的一条记忆"),)
        self.assertEqual((), cluster_memories(records))

    def test_min_cluster_size_is_configurable(self) -> None:
        records = (
            _Memory("m1", "用户偏好周五发布版本"),
            _Memory("m2", "用户偏好周五发布新版本"),
        )
        self.assertEqual((), cluster_memories(records, min_cluster_size=3))
        self.assertEqual(1, len(cluster_memories(records, min_cluster_size=2)))

    def test_cluster_size_is_capped(self) -> None:
        records = tuple(
            _Memory(f"m{index}", f"用户偏好周五发布版本{index}")
            for index in range(6)
        )
        clusters = cluster_memories(records, max_cluster_size=3)
        self.assertEqual(3, len(clusters[0].records))

    def test_ordering_is_deterministic(self) -> None:
        records = (
            _Memory("m3", "天气预报明天有雨"),
            _Memory("m1", "用户偏好周五发布版本"),
            _Memory("m2", "用户偏好周五发布新版本"),
        )
        first = cluster_memories(records, threshold=0.5)
        second = cluster_memories(tuple(reversed(records)), threshold=0.5)
        self.assertEqual(
            [cluster.signature for cluster in first],
            [cluster.signature for cluster in second],
        )
        self.assertEqual(("m1", "m2"), first[0].memory_ids)

    def test_cluster_carries_provenance_and_kind(self) -> None:
        records = (
            _Memory(
                "m1",
                "用户偏好周五发布版本",
                source_conversation_id="conv_a",
                source_turn_id="turn_a",
            ),
            _Memory(
                "m2",
                "用户偏好周五发布新版本",
                source_conversation_id="conv_b",
                source_turn_id="turn_b",
            ),
        )
        cluster = cluster_memories(records, threshold=0.5)[0]
        self.assertEqual("fact", cluster.kind)
        self.assertEqual("conv_a", cluster.representative_conversation_id)
        self.assertEqual("turn_a", cluster.representative_turn_id)
        payload = cluster.as_json()
        self.assertEqual(["m1", "m2"], payload["memoryIds"])
        self.assertIn("mergedContent", payload)

    def test_threshold_defaults(self) -> None:
        self.assertEqual(0.5, DEFAULT_SIMILARITY_THRESHOLD)
        self.assertEqual(2, DEFAULT_MIN_CLUSTER_SIZE)
        self.assertEqual(5, DEFAULT_MAX_CLUSTER_SIZE)

    def test_invalid_threshold_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            cluster_memories((), threshold=1.5)
        with self.assertRaises(Exception):
            cluster_memories((), min_cluster_size=1)


class MergeContentsTest(unittest.TestCase):
    def test_duplicate_contents_are_dropped(self) -> None:
        merged = merge_contents(
            (
                _Memory("m1", "用户偏好周五发布"),
                _Memory("m2", "用户偏好周五发布"),
            )
        )
        self.assertEqual("用户偏好周五发布", merged)

    def test_substring_is_dropped(self) -> None:
        merged = merge_contents(
            (
                _Memory("m1", "用户偏好周五发布"),
                _Memory("m2", "用户偏好周五发布，且喜欢简洁回答"),
            )
        )
        self.assertEqual("用户偏好周五发布，且喜欢简洁回答", merged)

    def test_distinct_contents_are_joined(self) -> None:
        merged = merge_contents(
            (
                _Memory("m1", "用户偏好周五发布版本"),
                _Memory("m2", "用户偏好周五发布新版本"),
            )
        )
        self.assertIn("；", merged)
        self.assertIn("用户偏好周五发布版本", merged)

    def test_long_merge_is_truncated(self) -> None:
        records = tuple(
            _Memory(f"m{index}", f"第{index}条很长的记忆内容" + "补" * 200)
            for index in range(5)
        )
        merged = merge_contents(records, max_characters=120)
        self.assertLessEqual(len(merged), 121)
        self.assertTrue(merged.endswith("…"))

    def test_empty_input_is_empty(self) -> None:
        self.assertEqual("", merge_contents(()))


class ClusterSignatureTest(unittest.TestCase):
    def test_signature_is_order_independent(self) -> None:
        left = (_Memory("m1", "甲"), _Memory("m2", "乙"))
        right = (_Memory("m2", "乙"), _Memory("m1", "甲"))
        self.assertEqual(cluster_signature(left), cluster_signature(right))

    def test_signature_changes_with_members(self) -> None:
        self.assertNotEqual(
            cluster_signature((_Memory("m1", "甲"), _Memory("m2", "乙"))),
            cluster_signature((_Memory("m1", "甲"), _Memory("m3", "丙"))),
        )

    def test_signature_is_a_stable_hex_digest(self) -> None:
        signature = cluster_signature((_Memory("m1", "甲"),))
        self.assertEqual(64, len(signature))
        self.assertTrue(all(char in "0123456789abcdef" for char in signature))


if __name__ == "__main__":
    unittest.main()
