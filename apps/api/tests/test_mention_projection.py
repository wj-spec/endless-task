"""@ 引用会话的**有界投影纯函数**单测（批次 C Task 3a 交付物 A）。

断言的是**逐字节**
的渲染结果与**截断点**，不是"大致包含"——投影是确定性的，所以可以逐字断言。
"""

from __future__ import annotations

import unittest

from endless_task.context_engine.planner import estimate_text_tokens
from endless_task.mentions.projection import (
    ASSISTANT_TURN_CHARS,
    DEFAULT_TOTAL_TOKEN_BUDGET,
    EARLIER_BULLET_CHARS,
    FULL_MODE_TOKEN_CEILING,
    MAX_EARLIER_BULLETS,
    MODE_FULL,
    MODE_RECENT,
    MODE_SUMMARY,
    MODE_TOKEN_LIMITS,
    RECENT_MODE_TURN_CHOICES,
    SUMMARY_TURN_WINDOW,
    USER_TURN_CHARS,
    ReferenceProjectionError,
    TurnView,
    project_reference,
    resolve_recent_turns,
)

BASE_KWARGS = dict(
    reference_id="conv_a",
    title="主线会话",
    kind="normal",
    lane_id="lane_main",
    updated_at="2026-09-15T12:00:00Z",
    turn_count=8,
)


def _turns(count: int, *, user: int = 6, assistant: int = 6) -> list[TurnView]:
    """确定性构造 count 轮（默认短文本）。"""

    return [
        TurnView(
            user_content=f"第{index}轮问题" + "问" * user,
            assistant_content=f"第{index}轮回答" + "答" * assistant,
        )
        for index in range(1, count + 1)
    ]


def _project(
    turns,
    *,
    mode=MODE_SUMMARY,
    recent_turns=None,
    max_tokens=None,
    artifacts=("report.md", "notes.txt"),
    **overrides,
):
    kwargs = dict(BASE_KWARGS)
    kwargs.update(overrides)
    if max_tokens is None:
        max_tokens = MODE_TOKEN_LIMITS.get(mode, MODE_TOKEN_LIMITS[MODE_SUMMARY])
    return project_reference(
        turns=turns,
        artifacts=artifacts,
        mode=mode,
        recent_turns=recent_turns,
        max_tokens=max_tokens,
        **kwargs,
    )


class SummaryModeStructureTest(unittest.TestCase):
    """摘要档（默认）的**结构**：逐字断言全文 + 三个固定小节。"""

    def test_renders_documented_block_byte_for_byte(self) -> None:
        projection = _project(_turns(4))
        self.assertEqual(
            projection.text,
            '<referenced_conversation id="conv_a" lane="lane_main"'
            ' kind="normal" title="主线会话" origin=""'
            ' updated="2026-09-15T12:00:00Z" turns="8">\n'
            "[最近 2 轮]\n"
            "  <user>第3轮问题问问问问问问</user>\n"
            "  <assistant>第3轮回答答答答答答答</assistant>\n"
            "  <user>第4轮问题问问问问问问</user>\n"
            "  <assistant>第4轮回答答答答答答答</assistant>\n"
            "[更早的要点]\n"
            "- 第1轮问题问问问问问问\n"
            "- 第2轮问题问问问问问问\n"
            "[触及的产物/文件]\n"
            "- report.md\n"
            "- notes.txt\n"
            "</referenced_conversation>",
        )
        self.assertFalse(projection.truncated)
        self.assertEqual(projection.rendered_turns, SUMMARY_TURN_WINDOW)
        self.assertEqual(projection.rendered_earlier_bullets, 2)
        self.assertEqual(projection.rendered_artifacts, 2)

    def test_same_input_yields_identical_bytes(self) -> None:
        first = _project(_turns(6))
        second = _project(_turns(6))
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.token_estimate, second.token_estimate)

    def test_token_estimate_is_the_existing_heuristic(self) -> None:
        # 单位如实：复用 context_engine.planner.estimate_text_tokens，不新造估算器。
        projection = _project(_turns(3))
        self.assertEqual(
            projection.token_estimate, estimate_text_tokens(projection.text)
        )

    def test_earlier_bullets_only_include_user_messages(self) -> None:
        projection = _project(_turns(4))
        self.assertNotIn("第1轮回答", projection.text)
        self.assertNotIn("第2轮回答", projection.text)

    def test_earlier_bullets_keep_the_eight_nearest_user_messages(self) -> None:
        projection = _project(_turns(14))
        bullets = [
            line
            for line in projection.text.splitlines()
            if line.startswith("- 第")
        ]
        self.assertEqual(len(bullets), MAX_EARLIER_BULLETS)
        self.assertTrue(bullets[0].startswith("- 第5轮问题"))
        self.assertTrue(bullets[-1].startswith("- 第12轮问题"))

    def test_long_user_and_assistant_are_clipped_to_the_documented_limits(self) -> None:
        turns = [TurnView(user_content="问" * 900, assistant_content="答" * 1500)]
        projection = _project(turns)
        user_line = next(
            line for line in projection.text.splitlines() if "<user>" in line
        )
        assistant_line = next(
            line for line in projection.text.splitlines() if "<assistant>" in line
        )
        user_text = user_line.split("<user>", 1)[1].split("</user>", 1)[0]
        assistant_text = assistant_line.split("<assistant>", 1)[1].split(
            "</assistant>", 1
        )[0]
        self.assertEqual(len(user_text), USER_TURN_CHARS)
        self.assertEqual(len(assistant_text), ASSISTANT_TURN_CHARS)
        self.assertTrue(user_text.endswith("…"))
        self.assertTrue(assistant_text.endswith("…"))

    def test_empty_history_renders_fixed_markers(self) -> None:
        projection = _project([], artifacts=())
        self.assertEqual(
            projection.text,
            '<referenced_conversation id="conv_a" lane="lane_main"'
            ' kind="normal" title="主线会话" origin=""'
            ' updated="2026-09-15T12:00:00Z" turns="8">\n'
            "[最近 0 轮]\n"
            "[更早的要点]\n"
            "（无）\n"
            "[触及的产物/文件]\n"
            "（无）\n"
            "</referenced_conversation>",
        )
        self.assertFalse(projection.truncated)

    def test_artifact_names_are_deduplicated_and_single_line(self) -> None:
        projection = _project(
            _turns(1),
            artifacts=("a.md", "a.md", "b\nc.md", ""),
        )
        # 整节精确比对（不是 assertIn——重复项会让子串断言假绿）。
        section = projection.text.split("[触及的产物/文件]\n", 1)[1].split(
            "\n</referenced_conversation>", 1
        )[0]
        self.assertEqual(section, "- a.md\n- b c.md")

    def test_quotes_and_newlines_cannot_break_the_open_tag(self) -> None:
        projection = _project(_turns(0), title='带"引号"\n与换行')
        first_line = projection.text.splitlines()[0]
        self.assertEqual(first_line.count('"'), 14)
        self.assertIn('title="带\'引号\' 与换行"', first_line)

    def test_ephemeral_block_carries_origin_and_memory_notice(self) -> None:
        projection = _project(
            _turns(1),
            kind="ephemeral",
            origin="临时 · 源自《主线会话》",
        )
        self.assertIn('kind="ephemeral"', projection.text)
        self.assertIn('origin="临时 · 源自《主线会话》"', projection.text)
        self.assertIn(
            "[注意] 临时对话（来源：临时 · 源自《主线会话》）。"
            "显式引用后其内容可能被确认为长期记忆。",
            projection.text,
        )

    def test_non_ephemeral_block_has_no_memory_notice(self) -> None:
        for kind in ("normal", "branch"):
            with self.subTest(kind=kind):
                self.assertNotIn("长期记忆", _project(_turns(1), kind=kind).text)


class ModeWindowTest(unittest.TestCase):
    """三档的窗口口径与参数校验。"""

    def test_recent_mode_uses_five_turns_by_default(self) -> None:
        projection = _project(_turns(7), mode=MODE_RECENT)
        self.assertEqual(projection.rendered_turns, RECENT_MODE_TURN_CHOICES[0])
        self.assertIn("[最近 5 轮]", projection.text)

    def test_recent_mode_accepts_ten_turns(self) -> None:
        projection = _project(_turns(12), mode=MODE_RECENT, recent_turns=10)
        self.assertEqual(projection.rendered_turns, 10)
        self.assertIn("[最近 10 轮]", projection.text)

    def test_summary_mode_accepts_only_two(self) -> None:
        self.assertEqual(resolve_recent_turns(mode=MODE_SUMMARY, recent_turns=2), 2)
        with self.assertRaises(ReferenceProjectionError) as captured:
            resolve_recent_turns(mode=MODE_SUMMARY, recent_turns=5)
        self.assertEqual(captured.exception.field, "recentTurns")

    def test_recent_mode_rejects_out_of_range_windows(self) -> None:
        for illegal in (0, 3, 4, 11, 100, -5):
            with self.subTest(recent_turns=illegal):
                with self.assertRaises(ReferenceProjectionError) as captured:
                    resolve_recent_turns(mode=MODE_RECENT, recent_turns=illegal)
                self.assertEqual(captured.exception.field, "recentTurns")

    def test_full_mode_rejects_recent_turns(self) -> None:
        with self.assertRaises(ReferenceProjectionError) as captured:
            resolve_recent_turns(mode=MODE_FULL, recent_turns=5)
        self.assertEqual(captured.exception.field, "recentTurns")

    def test_full_mode_projects_every_turn(self) -> None:
        projection = _project(_turns(9), mode=MODE_FULL)
        self.assertEqual(projection.rendered_turns, 9)
        self.assertIn("[最近 9 轮]", projection.text)
        self.assertEqual(projection.rendered_earlier_bullets, 0)

    def test_unknown_mode_names_the_field(self) -> None:
        with self.assertRaises(ReferenceProjectionError) as captured:
            _project(_turns(1), mode="everything")
        self.assertEqual(captured.exception.field, "mode")

    def test_unknown_kind_names_the_field(self) -> None:
        with self.assertRaises(ReferenceProjectionError) as captured:
            _project(_turns(1), kind="workspace")
        self.assertEqual(captured.exception.field, "kind")

    def test_max_tokens_must_be_positive_integer(self) -> None:
        for illegal in (0, -1, 1.5, True):
            with self.subTest(max_tokens=illegal):
                with self.assertRaises(ReferenceProjectionError) as captured:
                    _project(_turns(1), max_tokens=illegal)
                self.assertEqual(captured.exception.field, "maxTokens")


class TurnCountAttributeTest(unittest.TestCase):
    """I-1：块的 ``turns=`` 属性语义是**轮次数**（属性名不改，喂的值必须是轮次）。"""

    def test_open_tag_turns_attribute_is_the_turn_count(self) -> None:
        for turn_count in (1, 3, 6):
            with self.subTest(turn_count=turn_count):
                first_line = _project(_turns(3), turn_count=turn_count).text.splitlines()[0]
                self.assertIn(f'turns="{turn_count}"', first_line)
                self.assertEqual(1, first_line.count('turns="'))

    def test_signature_names_the_parameter_turn_count(self) -> None:
        import inspect

        parameters = inspect.signature(project_reference).parameters
        self.assertIn("turn_count", parameters)
        self.assertNotIn("entry_count", parameters)


class TruncationTest(unittest.TestCase):
    """三档各自的截断点：**不许静默丢**（必须标 truncated 且不超上限）。"""

    def test_summary_shrinks_earlier_bullets_first_then_window(self) -> None:
        turns = _turns(10, user=200, assistant=200)
        projection = _project(turns, max_tokens=400)
        self.assertTrue(projection.truncated)
        self.assertLessEqual(projection.token_estimate, 400)
        # 阶梯前半段：要点先掉光，**窗口还完整**（2 轮）。
        self.assertEqual(projection.rendered_earlier_bullets, 0)
        self.assertEqual(projection.rendered_turns, SUMMARY_TURN_WINDOW)
        self.assertEqual(projection.rendered_artifacts, 2)
        self.assertEqual(projection.token_estimate, estimate_text_tokens(projection.text))
        # 阶梯后半段：预算再收紧 → 窗口才开始缩。
        tighter = _project(turns, max_tokens=300)
        self.assertEqual(tighter.rendered_turns, 1)
        self.assertLessEqual(tighter.token_estimate, 300)

    def test_recent_ten_turns_hits_the_four_k_limit_and_is_flagged(self) -> None:
        turns = _turns(12, user=USER_TURN_CHARS, assistant=ASSISTANT_TURN_CHARS)
        projection = _project(turns, mode=MODE_RECENT, recent_turns=10)
        self.assertGreater(projection.token_estimate, 0)
        self.assertTrue(projection.truncated)
        self.assertLessEqual(projection.token_estimate, MODE_TOKEN_LIMITS[MODE_RECENT])
        # 10 轮 × (400+800) 字 ≈ 4K token：要点与产物名先掉光，窗口才从 10 缩到 9。
        self.assertEqual(projection.rendered_turns, 9)
        self.assertEqual(projection.rendered_earlier_bullets, 0)
        self.assertEqual(projection.rendered_artifacts, 0)
        self.assertEqual(
            projection.token_estimate, estimate_text_tokens(projection.text)
        )

    def test_full_mode_is_capped_by_the_explicit_ceiling(self) -> None:
        turns = _turns(40, user=USER_TURN_CHARS, assistant=ASSISTANT_TURN_CHARS)
        projection = _project(turns, mode=MODE_FULL)
        self.assertTrue(projection.truncated)
        self.assertLessEqual(projection.token_estimate, FULL_MODE_TOKEN_CEILING)
        self.assertLess(projection.rendered_turns, 40)

    def test_never_exceeds_max_tokens_even_for_degenerate_budgets(self) -> None:
        turns = _turns(5, user=300, assistant=300)
        for budget in (1, 2, 5, 13, 40, 120):
            with self.subTest(max_tokens=budget):
                projection = _project(turns, max_tokens=budget)
                self.assertLessEqual(projection.token_estimate, budget)
                self.assertTrue(projection.truncated)

    def test_budget_above_structure_never_flags_truncation(self) -> None:
        projection = _project(_turns(4), max_tokens=DEFAULT_TOTAL_TOKEN_BUDGET)
        self.assertFalse(projection.truncated)

    def test_artifacts_are_dropped_from_the_tail_before_the_window_shrinks(self) -> None:
        projection = _project(
            [TurnView(f"第{i}轮问题", f"第{i}轮回答") for i in range(1, 5)],
            artifacts=tuple(
                f"artifacts/very-long-name-{index}.md" for index in range(1, 5)
            ),
            max_tokens=110,
        )
        self.assertTrue(projection.truncated)
        self.assertEqual(projection.rendered_earlier_bullets, 0)
        self.assertEqual(projection.rendered_artifacts, 1)
        # 产物名掉了 3 条，窗口仍是完整的 2 轮 ⇒ 阶梯顺序（要点→产物→窗口）成立。
        self.assertEqual(projection.rendered_turns, SUMMARY_TURN_WINDOW)
        self.assertLessEqual(projection.token_estimate, 110)


if __name__ == "__main__":
    unittest.main()
