"""AppSettings 的三类运行预算：默认值、新 env、老 env 兼容与单轮兜底。

为什么单独开一个文件：`AppSettings` 的 env 用例此前散在
`test_provider_configuration.py` 里，而那些用例测的是 provider；本文件测的是
**运行预算**（时长/成本按类别），混在一起两边都不清楚。
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.agent_platform import AgentPlatformError
from endless_task.api.app_settings import AppSettings
# 复用既有 helper（它是 test_provider_configuration.py 内部定义的，不是 fixture 模块）。
# 跨测试模块 import 在本仓库已有先例：test_run_escalation.py 就从
# test_runtime_v2_execution 里 import ScriptedProvider。
from tests.test_provider_configuration import isolated_cwd

#: `database_path` 是 AppSettings 唯一的必填字段（没有默认值），而本文件的断言
#: 全部关于预算默认值、与数据库路径无关；给一个不会落盘的占位路径即可。
_PLACEHOLDER_DB = Path("/tmp/endless-task-app-settings-test.db")


def _default_settings() -> AppSettings:
    return AppSettings(database_path=_PLACEHOLDER_DB)


class RunBudgetSettingsTest(unittest.TestCase):
    def test_per_class_budgets_and_defaults(self) -> None:
        settings = _default_settings()
        self.assertEqual(180.0, settings.interactive_budget.soft_seconds)
        self.assertEqual(900.0, settings.interactive_budget.hard_seconds)
        self.assertEqual(1.0, settings.interactive_budget.cost_cap_usd)
        self.assertEqual(600.0, settings.task_budget.soft_seconds)
        self.assertEqual(3600.0, settings.task_budget.hard_seconds)
        self.assertEqual(3.0, settings.task_budget.cost_cap_usd)
        self.assertEqual(14400.0, settings.long_budget.hard_seconds)
        # 单轮上限是"高于 provider 最坏合法耗时（含其内部重试）"的兜底（R13），
        # 因此它**不等于** provider 超时；精确关系由
        # test_per_turn_backstop_covers_the_provider_worst_case 钉住。
        self.assertGreater(
            settings.task_budget.per_turn_seconds,
            settings.provider_timeout_seconds,
        )

    def test_defaults_do_not_drift_from_the_run_budget_constants(self) -> None:
        """AppSettings 的字段默认值必须与 Task 1 的常量一致。

        dataclass 的字段默认值只能是字面量（from_environment 要能覆盖它），
        所以这里必然存在"同一组数字写两处"。用这条断言把两处钉在一起：
        任何一处被改而另一处没跟上，测试立刻红——而不是让它们悄悄漂移。
        """
        from endless_task.runtime_v2.run_budget import (
            INTERACTIVE_BUDGET,
            LONG_BUDGET,
            TASK_BUDGET,
        )

        settings = _default_settings()
        for actual, expected in (
            (settings.interactive_budget, INTERACTIVE_BUDGET),
            (settings.task_budget, TASK_BUDGET),
            (settings.long_budget, LONG_BUDGET),
        ):
            self.assertEqual(expected.soft_seconds, actual.soft_seconds)
            self.assertEqual(expected.hard_seconds, actual.hard_seconds)
            self.assertEqual(expected.cost_cap_usd, actual.cost_cap_usd)

    def test_per_turn_backstop_covers_the_provider_worst_case(self) -> None:
        from endless_task.runtime.openai_compatible_provider import (
            DEFAULT_PROVIDER_MAX_RETRIES,
        )

        settings = _default_settings()
        worst_case = settings.provider_timeout_seconds * (
            DEFAULT_PROVIDER_MAX_RETRIES + 1
        )
        for budget in (
            settings.interactive_budget,
            settings.task_budget,
            settings.long_budget,
        ):
            self.assertGreater(
                budget.per_turn_seconds,
                worst_case,
                "单轮兜底必须高于 provider 的最坏合法耗时（含其内部重试）",
            )

    def test_legacy_env_only_overrides_interactive(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_AGENT_TIMEOUT_SECONDS": "300",
                "ENDLESS_TASK_COST_CAP_USD": "2.5",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()
        # 老变量的含义原样保留：只覆盖交互式档
        self.assertEqual(300.0, settings.interactive_budget.hard_seconds)
        self.assertEqual(2.5, settings.interactive_budget.cost_cap_usd)
        # 任务档不受老变量影响（这一条正是"按类别"的意义）
        self.assertEqual(3600.0, settings.task_budget.hard_seconds)
        self.assertEqual(3.0, settings.task_budget.cost_cap_usd)

    def test_new_env_overrides_task_class(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_TASK_HARD_SECONDS": "7200",
                "ENDLESS_TASK_TASK_SOFT_SECONDS": "1200",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(7200.0, settings.task_budget.hard_seconds)
        self.assertEqual(1200.0, settings.task_budget.soft_seconds)

    def test_resolve_run_budget_by_class(self) -> None:
        settings = _default_settings()
        self.assertIs(settings.task_budget, settings.resolve_run_budget("task"))
        self.assertIs(
            settings.interactive_budget, settings.resolve_run_budget("interactive")
        )
        self.assertIs(settings.long_budget, settings.resolve_run_budget("long"))
        with self.assertRaises(ValueError):
            settings.resolve_run_budget("nope")

    def test_soft_is_clamped_below_hard_when_legacy_overrides_it(self) -> None:
        # 老配置把 hard 压到 soft 之下时不能让 RunBudget 抛错——夹取保证合法。
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_AGENT_TIMEOUT_SECONDS": "60"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(60.0, settings.interactive_budget.hard_seconds)
        self.assertLessEqual(
            settings.interactive_budget.soft_seconds,
            settings.interactive_budget.hard_seconds,
        )

    def test_soft_is_halved_when_legacy_hard_equals_the_default_soft(self) -> None:
        # 夹取的**相等分支**：hard 恰好等于默认 soft（180）时 soft 必须严格更小，
        # 否则 RunBudget 的 soft < hard 校验会拒绝。
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_AGENT_TIMEOUT_SECONDS": "180"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(180.0, settings.interactive_budget.hard_seconds)
        self.assertEqual(90.0, settings.interactive_budget.soft_seconds)
        self.assertLess(
            settings.interactive_budget.soft_seconds,
            settings.interactive_budget.hard_seconds,
        )

    def test_new_env_overrides_long_class(self) -> None:
        # 三档都要有 env 覆盖用例（交互式/任务已有），长跑档在这里补齐。
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_LONG_HARD_SECONDS": "28800",
                "ENDLESS_TASK_LONG_SOFT_SECONDS": "7200",
                "ENDLESS_TASK_LONG_COST_CAP_USD": "12.5",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(28800.0, settings.long_budget.hard_seconds)
        self.assertEqual(7200.0, settings.long_budget.soft_seconds)
        self.assertEqual(12.5, settings.long_budget.cost_cap_usd)

    def test_explicit_soft_above_hard_is_rejected_not_rewritten(self) -> None:
        # 显式写进新 env 的 soft 不能被静默改写（夹取只该作用于旧配置覆盖路径）：
        # 要么被采纳，要么指名报错。
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS": "2000",
                "ENDLESS_TASK_INTERACTIVE_HARD_SECONDS": "900",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                AppSettings.from_environment()
        message = str(raised.exception)
        self.assertIn("ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS", message)
        self.assertIn("ENDLESS_TASK_INTERACTIVE_HARD_SECONDS", message)

    def test_new_env_soft_above_hard_is_rejected_for_every_tier(self) -> None:
        # 三档一把尺子：任务/长跑档同样指名报错，而不是只有交互式档被区别对待。
        for soft, hard in (
            ("ENDLESS_TASK_TASK_SOFT_SECONDS", "ENDLESS_TASK_TASK_HARD_SECONDS"),
            ("ENDLESS_TASK_LONG_SOFT_SECONDS", "ENDLESS_TASK_LONG_HARD_SECONDS"),
        ):
            with self.subTest(soft=soft), isolated_cwd(), patch.dict(
                os.environ,
                {soft: "9999", hard: "10"},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
            self.assertIn(soft, str(raised.exception))
            self.assertIn(hard, str(raised.exception))

    def test_illegal_tier_seconds_names_the_variable(self) -> None:
        # 非数字必须指名变量（bare float() 的 "could not convert string to float"
        # 不告诉用户改哪一行）。
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_TASK_HARD_SECONDS": "abc"},
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                AppSettings.from_environment()
        self.assertIn("ENDLESS_TASK_TASK_HARD_SECONDS", str(raised.exception))

    def test_zero_tier_seconds_is_rejected_and_names_the_variable(self) -> None:
        # 0 秒的 hard 是非法的，且报错必须指名 hard 这个变量（而不是在 RunBudget
        # 里以 "soft_seconds must be positive" 指错字段）。
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_INTERACTIVE_HARD_SECONDS": "0"},
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                AppSettings.from_environment()
        self.assertIn("ENDLESS_TASK_INTERACTIVE_HARD_SECONDS", str(raised.exception))

    def test_non_finite_tier_seconds_is_rejected_and_names_the_variable(self) -> None:
        # nan/inf 会同时绕过 `<= 0` 与 `>= hard` 两类比较（任何与 nan 的比较都是
        # False）：hard=inf 让硬墙静默消失（只剩 24 小时看门狗），hard=nan 让单轮
        # 超时塌成 0 秒（每次运行立刻"单次模型调用超时"）。这类值必须和其它非法值
        # 一样**指名变量**报错。
        for name, raw in (
            ("ENDLESS_TASK_TASK_HARD_SECONDS", "inf"),
            ("ENDLESS_TASK_TASK_HARD_SECONDS", "nan"),
            ("ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS", "nan"),
            ("ENDLESS_TASK_LONG_SOFT_SECONDS", "inf"),
            ("ENDLESS_TASK_LONG_HARD_SECONDS", "nan"),
        ):
            with self.subTest(name=name, raw=raw), isolated_cwd(), patch.dict(
                os.environ, {name: raw}, clear=True
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(name, str(raised.exception))

    def test_non_finite_cost_cap_is_rejected_and_names_the_variable(self) -> None:
        # cost cap = nan/inf 会让成本断路器静默失效（`cost > cap` 恒为 False）。
        for name, raw in (
            ("ENDLESS_TASK_TASK_COST_CAP_USD", "nan"),
            ("ENDLESS_TASK_INTERACTIVE_COST_CAP_USD", "inf"),
            ("ENDLESS_TASK_LONG_COST_CAP_USD", "nan"),
            ("ENDLESS_TASK_COST_CAP_USD", "nan"),
        ):
            with self.subTest(name=name, raw=raw), isolated_cwd(), patch.dict(
                os.environ, {name: raw}, clear=True
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(name, str(raised.exception))

    def test_illegal_legacy_agent_timeout_names_the_variable(self) -> None:
        # 旧变量此前是裸 float()：非数字只说 "could not convert string to float"，
        # nan 更是一路放行（`__post_init__` 的 "Agent timeout must be positive"
        # 也不指名变量）。它现在也要走带名解析。
        for raw in ("abc", "nan", "inf", "0"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_AGENT_TIMEOUT_SECONDS": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_AGENT_TIMEOUT_SECONDS", str(raised.exception)
                )

    def test_zero_class_cost_cap_stays_legal(self) -> None:
        # R8 语义表第三行：把该类 cap 设为 0 是**合法**的（= 关闭断路器），
        # 不能被"正数"校验误伤。行为面另见 test_run_usage_cost。
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_TASK_COST_CAP_USD": "0"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(0.0, settings.task_budget.cost_cap_usd)


class ChildWakeupSettingsTest(unittest.TestCase):
    """切片 9 M2：唤醒链路的开关 / 合并窗口 / 在飞上限。

    为什么单独一组：默认值必须是"开着"，而两个数值旋钮都带"非法值启动即失败"
    的门禁——关掉唤醒是显式开关，不是把窗口或上限写成 0 来达成的副作用。
    """

    def test_defaults_are_on_with_two_second_window_and_one_inflight(self) -> None:
        settings = _default_settings()
        self.assertTrue(settings.child_wakeup_enabled)
        self.assertEqual(2.0, settings.child_wakeup_merge_window_seconds)
        self.assertEqual(1, settings.child_wakeup_max_inflight_conversations)

    def test_env_overrides_the_three_knobs(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {
                "ENDLESS_TASK_CHILD_WAKEUP": "0",
                "ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS": "0.5",
                "ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT": "4",
            },
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertFalse(settings.child_wakeup_enabled)
        self.assertEqual(0.5, settings.child_wakeup_merge_window_seconds)
        self.assertEqual(4, settings.child_wakeup_max_inflight_conversations)

    def test_illegal_values_fail_loudly_and_name_the_variable(self) -> None:
        for name, raw in (
            ("ENDLESS_TASK_CHILD_WAKEUP", "maybe"),
            ("ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS", "0"),
            ("ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS", "nan"),
            ("ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS", "inf"),
            ("ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS", "abc"),
            ("ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT", "0"),
            ("ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT", "1.5"),
            ("ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT", "abc"),
        ):
            with self.subTest(name=name, raw=raw), isolated_cwd(), patch.dict(
                os.environ, {name: raw}, clear=True
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(name, str(raised.exception))

    # -- M3-9：每会话投递节流 ----------------------------------------------

    def test_throttle_defaults_to_sixty_seconds_in_production(self) -> None:
        """生产默认 60 秒（服务层默认 0 = 不节流，见 wakeup.py 的 docstring）。"""
        self.assertEqual(60.0, _default_settings().child_wakeup_throttle_seconds)

    def test_env_overrides_the_throttle_including_zero(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS": "0"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        # 0 是合法值：语义是"关掉节流"，不是"非法配置"。
        self.assertEqual(0.0, settings.child_wakeup_throttle_seconds)

        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS": "2.5"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(2.5, settings.child_wakeup_throttle_seconds)

    def test_illegal_throttle_values_fail_loudly_and_name_the_variable(
        self,
    ) -> None:
        for raw in ("-1", "-0.5", "nan", "inf", "abc", ""):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS",
                    str(raised.exception),
                )

    def test_direct_construction_also_guards_the_throttle(self) -> None:
        """手工构造 AppSettings 的路径同样被兜住（env 路径是带名解析器兜的）。"""
        for raw in (-1.0, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB,
                        child_wakeup_throttle_seconds=raw,
                    )
                self.assertIn(
                    "ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS",
                    str(raised.exception),
                )
        self.assertEqual(
            0.0,
            AppSettings(
                database_path=_PLACEHOLDER_DB,
                child_wakeup_throttle_seconds=0.0,
            ).child_wakeup_throttle_seconds,
        )


class MaxChildHardSecondsSettingsTest(unittest.TestCase):
    """M3-1：子运行墙上限设置（钳制模型声明的 timeoutSeconds）。

    为什么带"非法值启动即失败"的门禁：这个值就是**上界**。写成 0 会把每个子运行
    压到地板（1 秒收尾）、写成 nan/inf 则让上界名存实亡——两者都不是"关掉上限"的
    表达方式，必须指名报错，而不是静默降级。
    """

    def test_default_is_the_task_tier_order(self) -> None:
        settings = _default_settings()
        self.assertEqual(3600.0, settings.max_child_hard_seconds)

    def test_default_does_not_drift_from_the_run_budget_constant(self) -> None:
        from endless_task.runtime_v2.run_budget import (
            CHILD_DEFAULT_MAX_HARD_SECONDS,
        )

        self.assertEqual(
            CHILD_DEFAULT_MAX_HARD_SECONDS,
            _default_settings().max_child_hard_seconds,
        )

    def test_env_overrides_the_ceiling(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_MAX_CHILD_HARD_SECONDS": "120"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(120.0, settings.max_child_hard_seconds)

    def test_illegal_values_fail_loudly_and_name_the_variable(self) -> None:
        for raw in ("abc", "0", "-1", "nan", "inf"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_MAX_CHILD_HARD_SECONDS": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_MAX_CHILD_HARD_SECONDS", str(raised.exception)
                )


class MaxChildCostUsdSettingsTest(unittest.TestCase):
    """M3-3：子运行成本上限设置（默认 3.0 = 任务档同价）。

    **如实前提**：这道闸要**模型可定价**才会触发——`estimate_cost_usd` 对未定价的
    provider/model 返回 None，`cost_cap_exceeded` 于是恒为 False。也就是说配置位
    存在不等于一定拦得住（未定价时"配了也拦不住"），这里不假装它一定生效。

    与墙上限（`max_child_hard_seconds`）的差别：**0 是合法值**，语义是"关闭子运行
    成本上界"（cap=0 时 `cost_cap_exceeded` 恒 False），而不是把子运行压到地板。
    """

    def test_default_is_the_task_tier_price(self) -> None:
        self.assertEqual(3.0, _default_settings().max_child_cost_usd)

    def test_default_does_not_drift_from_the_run_budget_constant(self) -> None:
        from endless_task.runtime_v2.run_budget import (
            CHILD_DEFAULT_COST_CAP_USD,
        )

        self.assertEqual(
            CHILD_DEFAULT_COST_CAP_USD,
            _default_settings().max_child_cost_usd,
        )

    def test_env_overrides_the_cap(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_MAX_CHILD_COST_USD": "1.25"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(1.25, settings.max_child_cost_usd)

    def test_zero_is_legal_and_disables_the_child_cost_bound(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_MAX_CHILD_COST_USD": "0"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(0.0, settings.max_child_cost_usd)

    def test_illegal_env_values_fail_loudly_and_name_the_variable(self) -> None:
        # 负数 / nan / inf：都不是"关掉上界"的表达方式（0 才是），必须指名报错。
        for raw in ("abc", "-1", "nan", "inf"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_MAX_CHILD_COST_USD": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_MAX_CHILD_COST_USD", str(raised.exception)
                )

    def test_direct_construction_also_rejects_illegal_values(self) -> None:
        # env 路径由带名解析器兜住；这里兜"代码里手写构造 AppSettings"的路径。
        for raw in (-1.0, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB, max_child_cost_usd=raw
                    )
                self.assertIn(
                    "ENDLESS_TASK_MAX_CHILD_COST_USD", str(raised.exception)
                )


class MaxQueuedChildrenSettingsTest(unittest.TestCase):
    """M3 Task 7：在册（含排队）子运行的**绝对**上限设置。

    为什么必须有它：在册容量从"拒绝"改成"排队"之后，没有绝对上限的排队就是
    无界队列。默认 32 = 在册容量 8 的四倍，既容得下排队，又拦得住无界。
    """

    def test_default_does_not_drift_from_the_scheduler_constant(self) -> None:
        from endless_task.delegation.scheduler import DEFAULT_MAX_QUEUED_CHILDREN

        self.assertEqual(32, DEFAULT_MAX_QUEUED_CHILDREN)
        self.assertEqual(
            DEFAULT_MAX_QUEUED_CHILDREN, _default_settings().max_queued_children
        )

    def test_env_overrides_the_cap(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_MAX_QUEUED_CHILDREN": "64"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(64, settings.max_queued_children)

    def test_illegal_env_values_fail_loudly_and_name_the_variable(self) -> None:
        # 0 / 负数 不是"关掉排队上限"的表达方式（那会退化成无界队列）。
        for raw in ("0", "-1", "1.5", "abc"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_MAX_QUEUED_CHILDREN": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_MAX_QUEUED_CHILDREN", str(raised.exception)
                )

    def test_direct_construction_also_rejects_illegal_values(self) -> None:
        for raw in (0, -1):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB, max_queued_children=raw
                    )
                self.assertIn(
                    "ENDLESS_TASK_MAX_QUEUED_CHILDREN", str(raised.exception)
                )

    def test_queued_cap_below_parent_book_capacity_is_rejected(self) -> None:
        """绝对上限不得小于在册容量：否则"排队"这条闸门自相矛盾（在册 8 > 队列 4）。"""
        from endless_task.delegation import ChildBatchLimits

        with self.assertRaises(AgentPlatformError) as caught:
            ChildBatchLimits(max_children_per_parent=9, max_queued_children=8)
        self.assertEqual("invalid_child_batch_limits", caught.exception.code)

    def test_queued_cap_below_the_book_capacity_names_the_variable(self) -> None:
        """低于在册容量（默认 8）时，settings 层先拒，且**指名**变量与下限。

        旧行为：settings 放过，直到装配层组装 ``ChildBatchLimits`` 才抛
        ``invalid_child_batch_limits``——那条消息里既没有变量名也没有下限，
        运维只能靠猜是哪条设置越界。``ChildBatchLimits`` 的关系校验保留为
        不变量（见上一条用例），这里只补"更早 + 指名"这一层。
        """
        from endless_task.delegation.scheduler import DEFAULT_MAX_CHILDREN_PER_PARENT

        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_MAX_QUEUED_CHILDREN": "4"},
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                AppSettings.from_environment()
        message = str(raised.exception)
        self.assertIn("ENDLESS_TASK_MAX_QUEUED_CHILDREN", message)
        self.assertIn(str(DEFAULT_MAX_CHILDREN_PER_PARENT), message)

        # 直接构造（代码里手写 AppSettings）同样指名报错。
        for raw in (1, 7):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as direct:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB, max_queued_children=raw
                    )
                self.assertIn(
                    "ENDLESS_TASK_MAX_QUEUED_CHILDREN", str(direct.exception)
                )

        # 下限是"不小于 8"：正好 8 合法（不变量是 parent ≤ queued，不是严格大于）。
        self.assertEqual(
            DEFAULT_MAX_CHILDREN_PER_PARENT,
            AppSettings(
                database_path=_PLACEHOLDER_DB,
                max_queued_children=DEFAULT_MAX_CHILDREN_PER_PARENT,
            ).max_queued_children,
        )


class ShellNoChangeWatchdogSettingsTest(unittest.TestCase):
    """切片 10 T1 Step 2：`run_shell` 的无输出看门狗阈值（默认 60 秒，逐字不变）。

    语义（与 `shell_runner.resolve_no_change_timeout_seconds` 对齐）：
    - 默认 60 秒；
    - 0 = **关闭**看门狗（合法显式 opt-out）；
    - (0, 1.0) 钳到 1.0 秒（不许退化成"一有输出延迟就杀"）；
    - 负数 / nan / inf / 非数字 → 装配期**指名报错**。

    注意它与**总时长墙**（`shell_timeout_seconds`）是两件事：这条是"安静多久算死"，
    那条是"最长允许跑多久"。
    """

    def test_default_is_sixty_seconds(self) -> None:
        self.assertEqual(60.0, _default_settings().shell_no_change_timeout_seconds)

    def test_env_overrides_the_threshold(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS": "120"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(120.0, settings.shell_no_change_timeout_seconds)

    def test_zero_is_legal_and_means_the_watchdog_is_off(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS": "0"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        # 0 原样保留（不是"钳到下限"，也不是"立刻杀"）：runner 见到 <=0 就关看门狗。
        self.assertEqual(0.0, settings.shell_no_change_timeout_seconds)

    def test_tiny_values_are_clamped_up_to_the_floor(self) -> None:
        from endless_task.workspace_runtime.shell_runner import (
            MIN_NO_CHANGE_TIMEOUT_SECONDS,
        )

        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS": "0.2"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(
            MIN_NO_CHANGE_TIMEOUT_SECONDS, settings.shell_no_change_timeout_seconds
        )
        # 手写构造的路径同样被钳（`__post_init__`），不只有 env 路径。
        self.assertEqual(
            MIN_NO_CHANGE_TIMEOUT_SECONDS,
            AppSettings(
                database_path=_PLACEHOLDER_DB,
                shell_no_change_timeout_seconds=0.2,
            ).shell_no_change_timeout_seconds,
        )

    def test_illegal_values_fail_loudly_and_name_the_variable(self) -> None:
        for raw in ("-1", "nan", "inf", "abc"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS",
                    str(raised.exception),
                )

    def test_direct_construction_also_rejects_illegal_values(self) -> None:
        for raw in (-1.0, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB,
                        shell_no_change_timeout_seconds=raw,
                    )
                self.assertIn(
                    "ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS",
                    str(raised.exception),
                )


class ChildDefaultTimeoutSettingsTest(unittest.TestCase):
    """切片 10 T1 Step 3：委派子运行的**默认墙**（模型不写 timeoutSeconds 时用它）。

    默认 120.0 = 今天写死在 `CoordinatorDelegationHandler.__init__` 默认参数里的值；
    这个设置只是把它接到装配根上（`wiring.py` 从此显式传参）。它仍受
    `max_child_hard_seconds` 在派生预算那一步封顶。
    """

    def test_default_is_the_historical_120(self) -> None:
        self.assertEqual(120.0, _default_settings().child_default_timeout_seconds)

    def test_env_overrides_the_default(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS": "300"},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual(300.0, settings.child_default_timeout_seconds)

    def test_illegal_values_fail_loudly_and_name_the_variable(self) -> None:
        for raw in ("0", "-1", "nan", "inf", "abc"):
            with self.subTest(raw=raw), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS": raw},
                clear=True,
            ):
                with self.assertRaises(ValueError) as raised:
                    AppSettings.from_environment()
                self.assertIn(
                    "ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS",
                    str(raised.exception),
                )

    def test_direct_construction_also_rejects_illegal_values(self) -> None:
        for raw in (0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as raised:
                    AppSettings(
                        database_path=_PLACEHOLDER_DB,
                        child_default_timeout_seconds=raw,
                    )
                self.assertIn(
                    "ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS",
                    str(raised.exception),
                )


class RunBudgetProfileSettingsTest(unittest.TestCase):
    """切片 10 T1 Step 4：交互 run 的档位选择器（默认 `interactive`）。

    为什么需要它：`long` 档此前**没有任何调用方**（`ENDLESS_TASK_LONG_*` 改的是没人
    读的值）。本设置是那条线唯一的选择器，且**只影响交互 run**——任务 run 照旧
    `resolve_run_budget("task")`，子运行照旧走 `child_run_budget`（CE-5）。
    """

    def test_default_is_interactive(self) -> None:
        self.assertEqual("interactive", _default_settings().run_budget_profile)

    def test_env_selects_every_known_tier(self) -> None:
        from endless_task.runtime_v2.run_budget import DEFAULT_BUDGETS

        for profile in ("interactive", "task", "long"):
            with self.subTest(profile=profile), isolated_cwd(), patch.dict(
                os.environ,
                {"ENDLESS_TASK_RUN_BUDGET_PROFILE": profile},
                clear=True,
            ):
                settings = AppSettings.from_environment()
            self.assertEqual(profile, settings.run_budget_profile)
            # 选谁就解析到谁（这就是"long 档可达"的最小闭环）。注意三档对象带
            # per_turn 兜底（210 秒），所以与裸常量不是同一个对象——比对设置里
            # 已合成的同名字段，并与常量对齐 soft/hard/成本。
            tier = settings.resolve_run_budget(settings.run_budget_profile)
            self.assertIs(tier, getattr(settings, f"{profile}_budget"))
            self.assertEqual(
                DEFAULT_BUDGETS[profile].hard_seconds, tier.hard_seconds
            )
            self.assertEqual(
                DEFAULT_BUDGETS[profile].soft_seconds, tier.soft_seconds
            )

    def test_env_value_is_case_and_whitespace_tolerant(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_RUN_BUDGET_PROFILE": "  LONG "},
            clear=True,
        ):
            settings = AppSettings.from_environment()
        self.assertEqual("long", settings.run_budget_profile)

    def test_illegal_env_value_fails_loudly_and_names_the_variable(self) -> None:
        with isolated_cwd(), patch.dict(
            os.environ,
            {"ENDLESS_TASK_RUN_BUDGET_PROFILE": "marathon"},
            clear=True,
        ):
            with self.assertRaises(ValueError) as raised:
                AppSettings.from_environment()
        message = str(raised.exception)
        self.assertIn("ENDLESS_TASK_RUN_BUDGET_PROFILE", message)
        # 不许静默回落到 interactive：报错要说清允许什么。
        self.assertIn("long", message)

    def test_direct_construction_also_rejects_illegal_values(self) -> None:
        with self.assertRaises(ValueError) as raised:
            AppSettings(database_path=_PLACEHOLDER_DB, run_budget_profile="marathon")
        self.assertIn(
            "ENDLESS_TASK_RUN_BUDGET_PROFILE", str(raised.exception)
        )


if __name__ == "__main__":
    unittest.main()
