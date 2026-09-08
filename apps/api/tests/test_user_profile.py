"""B5 用户画像：渲染稳定性、版本与刷新节流（纯逻辑）。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from endless_task.runtime_v2.user_profile import (
    DEFAULT_MAX_CHARACTERS,
    DEFAULT_MAX_LINES,
    PROFILE_HEADER,
    ProfileFact,
    UserProfileBlock,
    facts_from_memories,
    next_version,
    profile_signature,
    render_profile_block,
    render_profile_lines,
    should_refresh,
)


@dataclass
class _Memory:
    id: str
    content: str
    kind: str = "preference"
    importance: float = 0.5


class RenderLinesTest(unittest.TestCase):
    def test_rendering_is_order_independent(self) -> None:
        facts = (
            ProfileFact("喜欢简洁回答", importance=0.8),
            ProfileFact("主要用中文", importance=0.6),
        )
        first = render_profile_lines(facts)
        second = render_profile_lines(tuple(reversed(facts)))
        self.assertEqual(first, second)
        self.assertEqual(
            ("- 喜欢简洁回答", "- 主要用中文"), first
        )

    def test_duplicates_keep_higher_importance(self) -> None:
        lines = render_profile_lines(
            (
                ProfileFact("喜欢简洁回答", importance=0.2),
                ProfileFact("喜欢简洁回答", importance=0.9),
            )
        )
        self.assertEqual(("- 喜欢简洁回答",), lines)

    def test_whitespace_variants_dedupe(self) -> None:
        lines = render_profile_lines(
            (
                ProfileFact("喜欢  简洁 回答"),
                ProfileFact("喜欢 简洁 回答"),
            )
        )
        self.assertEqual(1, len(lines))

    def test_line_limit_is_respected(self) -> None:
        facts = tuple(
            ProfileFact(f"偏好 {index}", importance=1 - index / 100)
            for index in range(20)
        )
        self.assertEqual(
            DEFAULT_MAX_LINES, len(render_profile_lines(facts))
        )

    def test_character_limit_is_respected(self) -> None:
        facts = (ProfileFact("很长的一段偏好" * 50, importance=0.9),)
        self.assertEqual((), render_profile_lines(facts, max_characters=50))

    def test_empty_input_is_empty(self) -> None:
        self.assertEqual((), render_profile_lines(()))

    def test_invalid_limits_rejected(self) -> None:
        with self.assertRaises(Exception):
            render_profile_lines((), max_lines=0)


class BlockAndSignatureTest(unittest.TestCase):
    def test_block_has_fixed_header(self) -> None:
        block = render_profile_block(("- 喜欢简洁回答",))
        self.assertTrue(block.startswith(PROFILE_HEADER))
        self.assertIn("- 喜欢简洁回答", block)

    def test_empty_lines_render_empty_block(self) -> None:
        self.assertEqual("", render_profile_block(()))

    def test_signature_is_stable_and_order_sensitive(self) -> None:
        self.assertEqual(
            profile_signature(("- a", "- b")), profile_signature(("- a", "- b"))
        )
        self.assertNotEqual(
            profile_signature(("- a", "- b")), profile_signature(("- b", "- a"))
        )

    def test_block_json_shape(self) -> None:
        block = UserProfileBlock(
            content="画像内容", version=3, signature="sig", lines=("- a",)
        )
        payload = block.as_json()
        self.assertEqual(3, payload["version"])
        self.assertEqual(4, payload["characters"])
        self.assertFalse(payload["manual"])
        self.assertFalse(block.empty)


class VersionTest(unittest.TestCase):
    def test_same_signature_keeps_version(self) -> None:
        self.assertEqual(
            3,
            next_version(
                current_version=3, current_signature="sig", new_signature="sig"
            ),
        )

    def test_changed_signature_bumps_version(self) -> None:
        self.assertEqual(
            4,
            next_version(
                current_version=3, current_signature="old", new_signature="new"
            ),
        )

    def test_no_previous_profile_starts_at_one(self) -> None:
        self.assertEqual(
            1,
            next_version(
                current_version=0, current_signature=None, new_signature="new"
            ),
        )


class ShouldRefreshTest(unittest.TestCase):
    def test_unchanged_signature_never_refreshes(self) -> None:
        self.assertFalse(
            should_refresh(
                last_updated_at="2026-01-01T00:00:00+00:00",
                now="2026-01-02T00:00:00+00:00",
                signature_changed=False,
            )
        )

    def test_interval_not_elapsed_skips(self) -> None:
        self.assertFalse(
            should_refresh(
                last_updated_at="2026-01-01T00:00:00+00:00",
                now="2026-01-01T00:01:00+00:00",
                min_interval_seconds=300,
            )
        )

    def test_interval_elapsed_refreshes(self) -> None:
        self.assertTrue(
            should_refresh(
                last_updated_at="2026-01-01T00:00:00+00:00",
                now="2026-01-01T01:00:00+00:00",
                min_interval_seconds=300,
            )
        )

    def test_force_always_refreshes(self) -> None:
        self.assertTrue(
            should_refresh(
                last_updated_at="2026-01-01T00:00:00+00:00",
                now="2026-01-01T00:00:10+00:00",
                signature_changed=False,
                force=True,
            )
        )

    def test_missing_timestamp_refreshes(self) -> None:
        self.assertTrue(should_refresh(last_updated_at=None))


class FactsFromMemoriesTest(unittest.TestCase):
    def test_maps_memory_fields(self) -> None:
        facts = facts_from_memories(
            (_Memory("mem_1", "喜欢简洁回答", kind="preference", importance=0.9),)
        )
        self.assertEqual(1, len(facts))
        self.assertEqual("喜欢简洁回答", facts[0].content)
        self.assertEqual("preference", facts[0].kind)
        self.assertEqual(0.9, facts[0].importance)
        self.assertEqual("mem_1", facts[0].source)

    def test_blank_content_is_skipped(self) -> None:
        self.assertEqual((), facts_from_memories((_Memory("m", "   "),)))

    def test_default_limits_are_small(self) -> None:
        self.assertLessEqual(DEFAULT_MAX_LINES, 10)
        self.assertLessEqual(DEFAULT_MAX_CHARACTERS, 800)


if __name__ == "__main__":
    unittest.main()
