"""<delegated-children> 注入块的筛选与渲染（纯函数）。"""

import unittest
from dataclasses import replace
from enum import Enum
from unittest.mock import patch

from endless_task.delegation import child_context
from endless_task.delegation.child_context import render_delegated_children_block
from endless_task.delegation.child_run_store import ChildRunRow, ChildRunState


def _row(
    child_run_id: str,
    *,
    state: ChildRunState = ChildRunState.RUNNING,
    injected_at=None,
    summary=None,
    started_at: str = "2026-09-11T10:00:00Z",
    task: str = "总结仓库测试现状",
) -> ChildRunRow:
    return ChildRunRow(
        child_run_id=child_run_id,
        parent_run_id="run_parent",
        parent_tool_call_id=f"call_{child_run_id}",
        parent_conversation_id="conv_parent",
        child_workspace_id=None,
        depth=1,
        capability_profile="subagent_readonly",
        workspace_mode="read_only_shared",
        timeout_seconds=120.0,
        task=task,
        state=state,
        summary=summary,
        error_code=None,
        safe_message=None,
        input_tokens=0,
        output_tokens=0,
        model_turns=0,
        tool_calls=0,
        created_at="2026-09-11T09:59:00Z",
        started_at=started_at,
        terminal_at=None,
        updated_at="2026-09-11T10:00:00Z",
        injected_at=injected_at,
        reported_at=None,
    )


class DelegatedChildrenBlockTest(unittest.TestCase):
    def test_no_rows_means_no_block(self) -> None:
        block, mark = render_delegated_children_block([], now="2026-09-11T10:03:00Z")
        self.assertIsNone(block)
        self.assertEqual((), mark)

    def test_running_child_is_listed_and_not_marked(self) -> None:
        block, mark = render_delegated_children_block(
            [_row("child_a")], now="2026-09-11T10:03:12Z"
        )
        self.assertIn("<delegated-children>", block)
        self.assertIn("child_a", block)
        self.assertIn("运行中", block)
        self.assertIn("3 分 12 秒", block)
        self.assertEqual((), mark)  # 未终态不打标记：它还要继续被列出

    def test_terminal_unreported_child_is_listed_and_marked(self) -> None:
        block, mark = render_delegated_children_block(
            [_row("child_b", state=ChildRunState.COMPLETED, summary="结论在此")],
            now="2026-09-11T10:03:00Z",
        )
        self.assertIn("child_b", block)
        self.assertIn("结论在此", block)
        self.assertEqual(("child_b",), mark)

    def test_terminal_reported_child_disappears(self) -> None:
        block, mark = render_delegated_children_block(
            [
                _row(
                    "child_c",
                    state=ChildRunState.COMPLETED,
                    summary="已经说过了",
                    injected_at="2026-09-11T10:02:00Z",
                )
            ],
            now="2026-09-11T10:03:00Z",
        )
        self.assertIsNone(block)
        self.assertEqual((), mark)

    def test_children_beyond_the_cap_are_folded(self) -> None:
        rows = [_row(f"child_{index:02d}") for index in range(5)]
        block, _ = render_delegated_children_block(
            rows, now="2026-09-11T10:03:00Z", max_children=3
        )
        self.assertIn("还有 2 个未列出", block)

    def test_summary_is_truncated(self) -> None:
        block, _ = render_delegated_children_block(
            [
                _row(
                    "child_d",
                    state=ChildRunState.COMPLETED,
                    summary="甲" * 500,
                )
            ],
            now="2026-09-11T10:03:00Z",
            max_summary_characters=10,
        )
        self.assertIn("甲" * 10, block)
        self.assertNotIn("甲" * 11, block)

    def test_queued_child_without_started_at_says_queued(self) -> None:
        """`started_at IS NULL` = 还在并发闸门外排队，不能报成"在跑"。"""
        block, mark = render_delegated_children_block(
            [_row("child_queue", started_at=None)], now="2026-09-11T10:03:00Z"
        )
        self.assertIn("排队中", block)
        self.assertNotIn("运行中", block)
        self.assertEqual((), mark)

    def test_a_future_state_that_is_terminal_is_suppressed_by_the_stamp(self) -> None:
        """反向对照：新状态**在**终态集合里时，盖过章就必须被抑制。

        `injected_at` 的语义是"终态只注入一次"，判据必须是**终态集合的成员资格**
        而不是写死的 `ChildRunState.RUNNING`：这里把模块看到的枚举换成"多了一个
        非终态状态"、并把该状态（连同 RUNNING）都放进**终态集合**的版本，喂一行
        `RUNNING` 且已盖章的记录。两种写法给出的答案相反：

        * 成员资格写法：RUNNING 在终态集合里 → 已盖章 → **不列**；
        * 写死 `== RUNNING`：无视终态集合，照样列 → **列出**。

        这条用例能杀掉两类变异（实测）：退回
        `row.state == ChildRunState.RUNNING`（成对的
        `test_a_future_non_terminal_state_is_listed_even_when_stamped` 也一起变红）；
        以及"不读 `TERMINAL_CHILD_RUN_STATES`、把四个终态写死"——这一类**只有它**抓得住，
        成对的那条会漏过去（矩阵见报告）。
        """

        class _FutureChildRunState(str, Enum):
            RUNNING = "running"
            QUEUED_PENDING = "queued_pending"

        with patch.object(child_context, "ChildRunState", _FutureChildRunState):
            with patch.object(
                child_context,
                "TERMINAL_CHILD_RUN_STATES",
                frozenset(
                    {
                        _FutureChildRunState.RUNNING,
                        _FutureChildRunState.QUEUED_PENDING,
                    }
                ),
            ):
                block, mark = render_delegated_children_block(
                    [
                        _row(
                            "child_future",
                            state=_FutureChildRunState.RUNNING,
                            injected_at="2026-09-11T10:02:00Z",
                        )
                    ],
                    now="2026-09-11T10:03:00Z",
                )

        self.assertIsNone(block, "终态且已盖章的行不该再列")
        self.assertEqual((), mark)

    def test_a_future_non_terminal_state_is_listed_even_when_stamped(self) -> None:
        """正向对照（与上一条 `..._that_is_terminal_is_suppressed_by_the_stamp` 成对）：
        非终态的行**永远不打**章，所以它每轮都露面，不会被"只注入一次"永久抑制。

        将来多一个**非终态**状态（这里叫 `QUEUED_PENDING`）时，它**不在**
        `TERMINAL_CHILD_RUN_STATES` 里：即便这条记录身上带着 `injected_at`（历史
        遗留或他处误写），也必须继续列在父 agent 的上下文里，且**不给它盖章**——
        盖章的语义只是"终态只注入一次"。

        判别手段：把模块看到的枚举换成"多了一个非终态状态"的版本，喂一行该状态且
        已盖章的记录。两种写法给出的答案相反：

        * 成员资格写法：不在终态集合里 → **照列，且 mark 为空**；
        * 写死 `!= ChildRunState.RUNNING`（旧实现）：既不等于 RUNNING、`injected_at`
          又不为空 → 虽然照列，却会**误盖章**（把非终态行也当成"只注入一次"）。

        两条用例合起来把两个方向都钉住（矩阵见报告）。
        """

        class _FutureChildRunState(str, Enum):
            RUNNING = "running"
            QUEUED_PENDING = "queued_pending"

        with patch.object(child_context, "ChildRunState", _FutureChildRunState):
            with patch.object(
                child_context,
                "TERMINAL_CHILD_RUN_STATES",
                frozenset({_FutureChildRunState.RUNNING}),
            ):
                block, mark = render_delegated_children_block(
                    [
                        _row(
                            "child_future_queued",
                            state=_FutureChildRunState.QUEUED_PENDING,
                            summary="还在闸门外排队",
                            injected_at="2026-09-11T10:02:00Z",
                        )
                    ],
                    now="2026-09-11T10:03:00Z",
                )

        self.assertIsNotNone(block, "非终态即便盖过章也必须照列，不能被永久抑制")
        self.assertIn("child_future_queued", block)
        self.assertIn("还在闸门外排队", block)
        self.assertEqual((), mark, "非终态行永远不打章，每一轮都要继续露面")

    def test_unparseable_started_at_degrades_without_raising(self) -> None:
        block, _ = render_delegated_children_block(
            [_row("child_bad", started_at="昨天")], now="2026-09-11T10:03:00Z"
        )
        self.assertIn("已用时未知", block)

    def test_failed_child_without_summary_shows_safe_message(self) -> None:
        block, mark = render_delegated_children_block(
            [
                replace(
                    _row("child_f", state=ChildRunState.FAILED),
                    safe_message="子运行在后台被取消。",
                )
            ],
            now="2026-09-11T10:03:00Z",
        )
        self.assertIn("已失败", block)
        self.assertIn("子运行在后台被取消。", block)
        self.assertEqual(("child_f",), mark)

    def test_total_cap_drops_tail_entries_and_marks_only_listed(self) -> None:
        """整块超限时丢尾部条目：没露面的终态绝不能被打上"已注入"标记。"""
        rows = [
            _row(f"child_{index:02d}", state=ChildRunState.COMPLETED, summary="结" * 40)
            for index in range(5)
        ]
        block, mark = render_delegated_children_block(
            rows, now="2026-09-11T10:03:00Z", max_total_characters=140
        )
        self.assertIsNotNone(block)
        self.assertLessEqual(len(block), 140)
        # 确实丢了尾部条目，且标记只覆盖真正露面了的那些。
        self.assertLess(len(mark), len(rows))
        self.assertEqual(block.count("- child_"), len(mark))
        self.assertIn(f"还有 {len(rows) - len(mark)} 个未列出", block)
        for child_run_id in mark:
            self.assertIn(child_run_id, block)


if __name__ == "__main__":
    unittest.main()
