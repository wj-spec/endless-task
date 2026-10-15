from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from endless_task.runtime_v2.run_budget import (
    DEFAULT_BUDGETS,
    INTERACTIVE_BUDGET,
    LONG_BUDGET,
    TASK_BUDGET,
    RunBudget,
)


class RunBudgetValidationTest(unittest.TestCase):
    def test_rejects_non_positive_limits(self) -> None:
        with self.assertRaises(ValueError):
            RunBudget(soft_seconds=0)
        with self.assertRaises(ValueError):
            RunBudget(hard_seconds=-1)
        with self.assertRaises(ValueError):
            RunBudget(cost_cap_usd=-0.1)
        with self.assertRaises(ValueError):
            RunBudget(per_turn_seconds=0)

    def test_soft_must_be_smaller_than_hard(self) -> None:
        with self.assertRaises(ValueError):
            RunBudget(soft_seconds=900, hard_seconds=900)
        with self.assertRaises(ValueError):
            RunBudget(soft_seconds=901, hard_seconds=900)
        # 只给其中一个也合法
        RunBudget(soft_seconds=10)
        RunBudget(hard_seconds=10)

    def test_is_frozen(self) -> None:
        budget = RunBudget(hard_seconds=10)
        with self.assertRaises(FrozenInstanceError):
            budget.hard_seconds = 20  # type: ignore[misc]


class RunBudgetArithmeticTest(unittest.TestCase):
    def test_reached_soft(self) -> None:
        budget = RunBudget(soft_seconds=180, hard_seconds=900)
        self.assertFalse(budget.reached_soft(179.9))
        self.assertTrue(budget.reached_soft(180))
        self.assertFalse(RunBudget(hard_seconds=900).reached_soft(10_000))

    def test_remaining_hard(self) -> None:
        budget = RunBudget(soft_seconds=10, hard_seconds=900)
        self.assertEqual(600.0, budget.remaining_hard(300))
        self.assertIsNone(RunBudget(soft_seconds=10).remaining_hard(300))

    def test_turn_timeout_is_capped_by_per_turn(self) -> None:
        budget = RunBudget(soft_seconds=10, hard_seconds=900, per_turn_seconds=60)
        # 剩余硬预算远大于单轮上限 → 取单轮上限（防止一次挂死的调用吃掉整段预算）
        self.assertEqual(60.0, budget.turn_timeout(0))
        # 剩余硬预算小于单轮上限 → 取剩余（不能超墙）
        self.assertEqual(5.0, budget.turn_timeout(895))
        # 已超墙 → 0（调用方据此收尾）
        self.assertEqual(0.0, budget.turn_timeout(901))

    def test_turn_timeout_without_hard_uses_per_turn(self) -> None:
        budget = RunBudget(per_turn_seconds=30)
        self.assertEqual(30.0, budget.turn_timeout(10_000))


class RunBudgetDefaultsTest(unittest.TestCase):
    def test_defaults_match_the_agreed_tiers(self) -> None:
        self.assertEqual((180.0, 900.0, 1.0), (
            INTERACTIVE_BUDGET.soft_seconds,
            INTERACTIVE_BUDGET.hard_seconds,
            INTERACTIVE_BUDGET.cost_cap_usd,
        ))
        self.assertEqual((600.0, 3600.0, 3.0), (
            TASK_BUDGET.soft_seconds,
            TASK_BUDGET.hard_seconds,
            TASK_BUDGET.cost_cap_usd,
        ))
        self.assertEqual((3600.0, 14400.0, 10.0), (
            LONG_BUDGET.soft_seconds,
            LONG_BUDGET.hard_seconds,
            LONG_BUDGET.cost_cap_usd,
        ))
        self.assertEqual(
            {"interactive", "task", "long"}, set(DEFAULT_BUDGETS)
        )

    def test_every_default_has_a_per_turn_cap(self) -> None:
        for name, budget in DEFAULT_BUDGETS.items():
            self.assertGreater(budget.per_turn_seconds, 0, name)
            self.assertLess(
                budget.soft_seconds, budget.hard_seconds, name  # type: ignore[arg-type]
            )


class ChildRunBudgetCeilingTest(unittest.TestCase):
    """M3-1：派生预算的墙上限（钳制模型声明的 timeoutSeconds）。

    `SpawnSpec.timeout_seconds` 收得到 86400 秒：不设上限时，一个模型声明"跑一天"
    的子运行就真能占着并发名额与在册配额一天。钳制发生在**派生时**，而不是执行器
    收到之后——排队时间也要算在墙里。
    """

    def test_ceiling_clamps_a_day_long_declared_timeout(self) -> None:
        from endless_task.runtime_v2.run_budget import child_run_budget

        budget = child_run_budget(
            deadline_monotonic=86_400.0, now=0.0, max_hard_seconds=3_600.0
        )

        self.assertEqual(3_600.0, budget.hard_seconds)

    def test_declared_timeout_below_the_ceiling_is_untouched(self) -> None:
        # 上限只是上界：模型（或调用方）声明更小的值时按剩余时间走。
        from endless_task.runtime_v2.run_budget import child_run_budget

        budget = child_run_budget(
            deadline_monotonic=300.0, now=0.0, max_hard_seconds=3_600.0
        )

        self.assertEqual(300.0, budget.hard_seconds)

    def test_floor_wins_over_a_ceiling_configured_below_it(self) -> None:
        # 下限优先于上限：上限被配得比 CHILD_MIN_HARD_SECONDS 还小（0.5）时，子运行
        # 仍要拿到一次收尾机会，而不是被压成 0（RunBudget 也不接受 0）。
        from endless_task.runtime_v2.run_budget import (
            CHILD_MIN_HARD_SECONDS,
            child_run_budget,
        )

        budget = child_run_budget(
            deadline_monotonic=86_400.0, now=0.0, max_hard_seconds=0.5
        )

        self.assertEqual(CHILD_MIN_HARD_SECONDS, budget.hard_seconds)

    def test_ceiling_does_not_raise_an_already_expired_deadline(self) -> None:
        # 已过期（remaining < 0）时 min(remaining, ceiling) 仍是负的 → 地板照样兜住。
        from endless_task.runtime_v2.run_budget import (
            CHILD_MIN_HARD_SECONDS,
            child_run_budget,
        )

        budget = child_run_budget(
            deadline_monotonic=1_000.0, now=1_000.5, max_hard_seconds=3_600.0
        )

        self.assertEqual(CHILD_MIN_HARD_SECONDS, budget.hard_seconds)

    def test_omitting_the_ceiling_reproduces_the_old_values_byte_for_byte(self) -> None:
        # 向后兼容：不传 max_hard_seconds 与显式传 None 必须逐字段等于旧行为
        # （86400 秒的声明照旧全额生效——默认配了上限的是接线程，不是这个纯函数）。
        from endless_task.runtime_v2.run_budget import (
            DEFAULT_PER_TURN_SECONDS,
            child_run_budget,
        )

        for kwargs in ({}, {"max_hard_seconds": None}):
            with self.subTest(kwargs=kwargs):
                budget = child_run_budget(
                    deadline_monotonic=86_400.0, now=0.0, **kwargs
                )
                self.assertEqual(86_400.0, budget.hard_seconds)
                self.assertIsNone(budget.soft_seconds)
                self.assertEqual(0.0, budget.cost_cap_usd)
                self.assertEqual(DEFAULT_PER_TURN_SECONDS, budget.per_turn_seconds)

    def test_default_ceiling_constant_matches_the_task_tier_wall(self) -> None:
        # 常量语义：默认上限与任务档硬墙同量级（3600 秒）。
        from endless_task.runtime_v2.run_budget import (
            CHILD_DEFAULT_MAX_HARD_SECONDS,
            TASK_BUDGET,
        )

        self.assertEqual(3_600.0, CHILD_DEFAULT_MAX_HARD_SECONDS)
        self.assertEqual(TASK_BUDGET.hard_seconds, CHILD_DEFAULT_MAX_HARD_SECONDS)


class ChildRunBudgetCostCapTest(unittest.TestCase):
    """M3-3：子运行的成本上限——真上界（到顶**停**），不是"只提请"。

    `RunBudget.stop_on_cost` 就是这条策略位：父档（interactive/task/long）恒为
    假 = 今天的行为（提请一次、继续跑）；子运行跑在无人值守的隐藏会话里，
    "请人工决策"对它没有意义（同 `soft_seconds=None` 的理由），所以派生预算
    恒为真。cap 为 0 时 `cost_cap_exceeded` 恒 False，这个策略位是惰性的。
    """

    def test_explicit_cost_cap_sets_the_cap_and_the_stop_policy(self) -> None:
        from endless_task.runtime_v2.run_budget import child_run_budget

        budget = child_run_budget(
            deadline_monotonic=1_000.0, now=940.0, cost_cap_usd=3.0
        )

        self.assertEqual(3.0, budget.cost_cap_usd)
        self.assertIs(True, budget.stop_on_cost)

    def test_omitting_the_cost_cap_stays_backward_compatible(self) -> None:
        # 不传 → cap 保持 0.0（Task 2 的既有断言不破）；策略位仍是真——cap=0 时
        # `cost_cap_exceeded` 恒 False，所以"真"在这里是惰性的，不是新上界。
        from endless_task.runtime_v2.run_budget import child_run_budget

        budget = child_run_budget(deadline_monotonic=1_000.0, now=940.0)

        self.assertEqual(0.0, budget.cost_cap_usd)
        self.assertIs(True, budget.stop_on_cost)

    def test_default_cost_cap_constant_matches_the_task_tier_price(self) -> None:
        from endless_task.runtime_v2.run_budget import (
            CHILD_DEFAULT_COST_CAP_USD,
            TASK_BUDGET,
        )

        self.assertEqual(3.0, CHILD_DEFAULT_COST_CAP_USD)
        self.assertEqual(TASK_BUDGET.cost_cap_usd, CHILD_DEFAULT_COST_CAP_USD)

    def test_stop_on_cost_defaults_to_false(self) -> None:
        # 手写 RunBudget（不改策略位）必须逐字节等于加字段之前的行为：只提请。
        self.assertIs(False, RunBudget(hard_seconds=10.0, cost_cap_usd=1.0).stop_on_cost)

    def test_parent_tiers_keep_the_advise_only_policy(self) -> None:
        for name, budget in DEFAULT_BUDGETS.items():
            with self.subTest(tier=name):
                self.assertIs(False, budget.stop_on_cost)


class ChildRunBudgetTest(unittest.TestCase):
    def test_child_run_budget_is_derived_from_the_remaining_deadline(self) -> None:
        from endless_task.runtime_v2.run_budget import child_run_budget

        budget = child_run_budget(deadline_monotonic=1_000.0, now=940.0)

        self.assertEqual(60.0, budget.hard_seconds)
        # 子运行是无人值守的隐藏会话：不设软阈值（"请人工决策"没有意义）。
        self.assertIsNone(budget.soft_seconds)
        self.assertFalse(budget.reached_soft(59.0))

    def test_child_run_budget_falls_back_to_the_task_tier(self) -> None:
        from endless_task.runtime_v2.run_budget import TASK_BUDGET, child_run_budget

        self.assertIs(TASK_BUDGET, child_run_budget(None))

    def test_child_run_budget_floors_an_already_expired_deadline(self) -> None:
        from endless_task.runtime_v2.run_budget import (
            CHILD_MIN_HARD_SECONDS,
            child_run_budget,
        )

        budget = child_run_budget(deadline_monotonic=1_000.0, now=1_000.5)

        self.assertEqual(CHILD_MIN_HARD_SECONDS, budget.hard_seconds)
