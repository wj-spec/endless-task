"""委派子运行记录仓储：跨轮次、跨进程重启回答"子运行现在怎么样"。"""

import tempfile
import unittest
from pathlib import Path

from endless_task.delegation.child_run_store import (
    TERMINAL_CHILD_RUN_STATES,
    ChildRunState,
)
from endless_task.domain.repositories import InvalidStateError, NotFoundError
from endless_task.storage import Database, SqliteDelegationChildRunRepository
from endless_task.delegation.protocol import UsageSummary


def _reserve(repository, **overrides):
    payload = {
        "child_run_id": "child_a",
        "parent_run_id": "run_parent",
        "parent_tool_call_id": "call_1",
        "parent_conversation_id": "conv_parent",
        "depth": 1,
        "capability_profile": "subagent_readonly",
        "workspace_mode": "read_only_shared",
        "timeout_seconds": 120.0,
        "task": "总结仓库的测试现状",
    }
    payload.update(overrides)
    return repository.reserve_child_run(**payload)


class DelegationChildRunRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "delegation.db"
        )
        self.database.initialize()
        self.repository = SqliteDelegationChildRunRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_reserve_creates_running_row(self) -> None:
        row = _reserve(self.repository)
        self.assertEqual(ChildRunState.RUNNING, row.state)
        self.assertEqual("run_parent", row.parent_run_id)
        self.assertEqual("总结仓库的测试现状", row.task)
        self.assertIsNone(row.terminal_at)
        self.assertIsNotNone(row.created_at)

    def test_reserve_is_idempotent_per_spawn_key(self) -> None:
        _reserve(self.repository)
        # 同一个父工具调用再来一次：返回 None，而不是插第二行。
        self.assertIsNone(_reserve(self.repository, child_run_id="child_other"))
        self.assertEqual(1, len(self.repository.list_child_runs("run_parent")))

    def test_get_and_find_by_spawn_key(self) -> None:
        _reserve(self.repository)
        self.assertIsNotNone(self.repository.get_child_run("child_a"))
        self.assertIsNone(self.repository.get_child_run("child_missing"))
        found = self.repository.find_child_run_by_spawn_key(
            parent_run_id="run_parent", parent_tool_call_id="call_1"
        )
        self.assertIsNotNone(found)
        self.assertEqual("child_a", found.child_run_id)

    def test_mark_terminal_records_projection_and_usage(self) -> None:
        _reserve(self.repository)
        row = self.repository.mark_child_terminal(
            "child_a",
            state=ChildRunState.COMPLETED,
            summary="仓库有 215 个测试模块。",
            usage=UsageSummary(input_tokens=11, output_tokens=22, model_turns=3),
        )
        self.assertEqual(ChildRunState.COMPLETED, row.state)
        self.assertEqual("仓库有 215 个测试模块。", row.summary)
        self.assertEqual(11, row.input_tokens)
        self.assertEqual(3, row.model_turns)
        self.assertIsNotNone(row.terminal_at)

    def test_mark_terminal_twice_is_rejected(self) -> None:
        _reserve(self.repository)
        self.repository.mark_child_terminal(
            "child_a", state=ChildRunState.CANCELLED
        )
        with self.assertRaises(InvalidStateError):
            self.repository.mark_child_terminal(
                "child_a", state=ChildRunState.COMPLETED
            )

    def test_mark_terminal_rejects_a_bare_string_state(self) -> None:
        """入参校验：裸字符串 `"completed"` 必须在**调用点**被拒。

        旧实现零校验，裸字符串会一路走到下游的 ``state.value`` / 与枚举比较，在
        离调用点很远的地方炸成 ``AttributeError``。这里钉的是错误类型与位置。
        """
        _reserve(self.repository)
        with self.assertRaises(ValueError):
            self.repository.mark_child_terminal("child_a", state="completed")
        # 被拒之后行仍是 running：非法入参不留半写状态。
        self.assertEqual(
            ChildRunState.RUNNING, self.repository.get_child_run("child_a").state
        )

    def test_mark_child_started_records_the_first_moment_only(self) -> None:
        # 用可控时钟：两次调用必须落在不同时刻，"不覆盖已有值"才检验得出来。
        ticks = iter(
            [
                "2026-09-03T00:00:01.000Z",
                "2026-09-03T00:00:02.000Z",
                "2026-09-03T00:00:03.000Z",
            ]
        )
        repository = SqliteDelegationChildRunRepository(
            self.database, clock=lambda: next(ticks)
        )
        _reserve(repository)
        self.assertIsNone(repository.get_child_run("child_a").started_at)

        first = repository.mark_child_started("child_a")
        self.assertEqual("2026-09-03T00:00:02.000Z", first.started_at)
        # 重复调用幂等：第一次开始的时刻才是真相。
        self.assertEqual(
            "2026-09-03T00:00:02.000Z",
            repository.mark_child_started("child_a").started_at,
        )

    def test_mark_child_started_missing_row_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repository.mark_child_started("child_missing")

    def test_sweep_interrupted_marks_only_running_rows(self) -> None:
        _reserve(self.repository)
        _reserve(
            self.repository,
            child_run_id="child_b",
            parent_run_id="run_other",
            parent_tool_call_id="call_2",
        )
        self.repository.mark_child_terminal(
            "child_b", state=ChildRunState.COMPLETED, summary="done"
        )
        swept = self.repository.sweep_interrupted_child_runs()
        self.assertEqual(("child_a",), tuple(row.child_run_id for row in swept))
        # 返回值必须是改动后的行：状态已是 interrupted 且写上了终态时间，
        # 否则启动对账会把已中断的子运行误报成仍在跑。
        for row in swept:
            self.assertEqual(ChildRunState.INTERRUPTED, row.state)
            self.assertIsNotNone(row.terminal_at)
        self.assertEqual(
            ChildRunState.INTERRUPTED,
            self.repository.get_child_run("child_a").state,
        )
        self.assertEqual(
            ChildRunState.COMPLETED,
            self.repository.get_child_run("child_b").state,
        )
        # 对账是幂等的：再扫一次没有 running 行。
        self.assertEqual((), self.repository.sweep_interrupted_child_runs())

    def test_rows_survive_a_new_repository_instance(self) -> None:
        _reserve(self.repository)
        reloaded = SqliteDelegationChildRunRepository(self.database)
        row = reloaded.get_child_run("child_a")
        self.assertEqual(ChildRunState.RUNNING, row.state)

    def test_new_row_has_no_reporting_marks(self) -> None:
        row = _reserve(self.repository)
        self.assertIsNone(row.injected_at)
        self.assertIsNone(row.reported_at)

    def test_mark_child_injected_is_idempotent(self) -> None:
        _reserve(self.repository)
        first = self.repository.mark_child_injected("child_a")
        self.assertIsNotNone(first.injected_at)
        second = self.repository.mark_child_injected("child_a")
        self.assertEqual(first.injected_at, second.injected_at)

    def test_mark_child_reported_sets_both_marks_once(self) -> None:
        _reserve(self.repository)
        self.repository.mark_child_terminal(
            "child_a", state=ChildRunState.COMPLETED, summary="done"
        )
        reported = self.repository.mark_child_reported("child_a")
        self.assertIsNotNone(reported.reported_at)
        self.assertIsNotNone(reported.injected_at)
        again = self.repository.mark_child_reported("child_a")
        self.assertEqual(reported.reported_at, again.reported_at)

    def test_mark_child_reported_refuses_a_running_child(self) -> None:
        from endless_task.domain.repositories import InvalidStateError

        _reserve(self.repository)
        with self.assertRaises(InvalidStateError):
            self.repository.mark_child_reported("child_a")

    def test_marking_an_unknown_child_raises_not_found(self) -> None:
        from endless_task.domain.repositories import NotFoundError

        with self.assertRaises(NotFoundError):
            self.repository.mark_child_injected("child_missing")
        with self.assertRaises(NotFoundError):
            self.repository.mark_child_reported("child_missing")

    def test_list_child_runs_for_conversation_filters_and_orders(self) -> None:
        _reserve(self.repository, child_run_id="child_1", parent_tool_call_id="call_1")
        _reserve(
            self.repository,
            child_run_id="child_2",
            parent_tool_call_id="call_2",
            parent_conversation_id="conv_other",
        )
        _reserve(self.repository, child_run_id="child_3", parent_tool_call_id="call_3")

        found = self.repository.list_child_runs_for_conversation("conv_parent")

        self.assertEqual(("child_1", "child_3"), tuple(r.child_run_id for r in found))

    def test_unreported_terminal_runs_exclude_running_and_reported(self) -> None:
        _reserve(self.repository, child_run_id="child_running", parent_tool_call_id="c1")
        _reserve(self.repository, child_run_id="child_done", parent_tool_call_id="c2")
        _reserve(self.repository, child_run_id="child_reported", parent_tool_call_id="c3")
        self.repository.mark_child_terminal(
            "child_done", state=ChildRunState.COMPLETED, summary="done"
        )
        self.repository.mark_child_terminal(
            "child_reported", state=ChildRunState.FAILED, safe_message="boom"
        )
        self.repository.mark_child_reported("child_reported")

        pending = self.repository.list_unreported_terminal_child_runs(
            parent_conversation_id="conv_parent"
        )

        self.assertEqual(("child_done",), tuple(r.child_run_id for r in pending))

    def test_unreported_terminal_runs_without_conversation_filter_sees_all(self) -> None:
        _reserve(self.repository, child_run_id="child_a", parent_tool_call_id="c1")
        _reserve(
            self.repository,
            child_run_id="child_b",
            parent_tool_call_id="c2",
            parent_conversation_id="conv_other",
        )
        self.repository.mark_child_terminal(
            "child_a", state=ChildRunState.COMPLETED, summary="a"
        )
        self.repository.mark_child_terminal(
            "child_b", state=ChildRunState.CANCELLED
        )

        pending = self.repository.list_unreported_terminal_child_runs()

        self.assertEqual(("child_a", "child_b"), tuple(r.child_run_id for r in pending))

    def test_every_terminal_state_is_queued_for_wakeup(self) -> None:
        """终态集合只有一个真相源：`TERMINAL_CHILD_RUN_STATES` 里每个值都要被列出。

        旧实现在 SQL 里写死四个字符串字面量，将来多一个终态很容易漏改那行 ——
        漏了就等于"那一类终态永远不会被唤醒"。这里逐状态造一行，钉住"集合里的
        每一个都能被未汇报队列查出来"。
        """
        states = sorted(TERMINAL_CHILD_RUN_STATES, key=lambda item: item.value)
        for index, state in enumerate(states):
            _reserve(
                self.repository,
                child_run_id=f"child_{state.value}",
                parent_tool_call_id=f"call_{index}",
            )
            self.repository.mark_child_terminal(
                f"child_{state.value}", state=state, summary=f"{state.value} 收尾"
            )

        pending = self.repository.list_unreported_terminal_child_runs()

        self.assertEqual(
            tuple(sorted(row.child_run_id for row in pending)),
            tuple(sorted(f"child_{state.value}" for state in states)),
        )
        # 逐个状态也要能被会话过滤查到（终态参数在前、会话参数在后的顺序错位
        # 会让这条立刻红）。
        filtered = self.repository.list_unreported_terminal_child_runs(
            parent_conversation_id="conv_parent"
        )
        self.assertEqual(
            tuple(sorted(row.child_run_id for row in pending)),
            tuple(sorted(row.child_run_id for row in filtered)),
        )

    def test_reporting_marks_are_written_at_most_once(self) -> None:
        # 默认时钟下相邻两次调用撞同一毫秒的概率约两成（实测 63/300），
        # "不覆盖已有值"在那种情况下会漏检；这里用可控时钟把两次调用推到不同时刻。
        ticks = iter(
            [
                "2026-09-03T00:00:01.000Z",  # reserve
                "2026-09-03T00:00:02.000Z",  # mark_child_terminal
                "2026-09-03T00:00:03.000Z",  # 第一次 mark_child_reported
                "2026-09-03T00:00:04.000Z",  # 第二次 mark_child_reported
                "2026-09-03T00:00:05.000Z",  # 再单独 mark_child_injected
            ]
        )
        repository = SqliteDelegationChildRunRepository(
            self.database, clock=lambda: next(ticks)
        )
        _reserve(repository)
        repository.mark_child_terminal(
            "child_a", state=ChildRunState.COMPLETED, summary="done"
        )

        first = repository.mark_child_reported("child_a")
        self.assertEqual("2026-09-03T00:00:03.000Z", first.reported_at)
        self.assertEqual("2026-09-03T00:00:03.000Z", first.injected_at)

        again = repository.mark_child_reported("child_a")
        self.assertEqual("2026-09-03T00:00:03.000Z", again.reported_at)
        self.assertEqual("2026-09-03T00:00:03.000Z", again.injected_at)

        # 注入打标同样保留第一次的时刻（此时行上已有汇报写入的时间戳）。
        self.assertEqual(
            "2026-09-03T00:00:03.000Z",
            repository.mark_child_injected("child_a").injected_at,
        )
