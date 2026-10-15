"""切片 10 T1 Step 4：`long` 预算档**可达**，且只影响交互 run。

背景（验收 ④）：`resolve_run_budget("long")` 与 `LONG_BUDGET` 此前**全仓没有调用方**
——`ENDLESS_TASK_LONG_*` 改的是没人读的值，文档承诺的"仅 env 可启用"是假的。
本切片新增档位选择器 `ENDLESS_TASK_RUN_BUDGET_PROFILE`（默认 `interactive`），
把它接到交互网关那一处；任务 run 与委派子运行**各自照旧**（CE-5 硬契约：
Task/Unattended 使用独立 budget profile，不得并回交互档）。

为什么在装配层测：`wiring.py` 那一行是唯一的选择点。选错/不选的后果不是报错，
而是"运行拿到的墙不对"——只有把装配出来的网关与执行器拿出来看才能钉死。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.runtime_v2 import AgentRunExecutor
from endless_task.runtime_v2.run_budget import DEFAULT_BUDGETS


def _settings(directory: str, *, profile: str) -> AppSettings:
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        run_budget_profile=profile,
    )


class RunBudgetProfileWiringTest(unittest.IsolatedAsyncioTestCase):
    async def _container(self, *, profile: str):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(
            settings=_settings(directory.name, profile=profile),
            provider=FakeProvider(chunks=("ok",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        return app.state.container

    async def test_default_profile_keeps_the_interactive_tier(self) -> None:
        """默认（不设）仍是 `interactive`：呼叫序列与档位对象都逐字不变。"""
        calls: list[str] = []
        real = AppSettings.resolve_run_budget

        def _spy(self, run_class: str):
            calls.append(run_class)
            return real(self, run_class)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(AppSettings, "resolve_run_budget", _spy):
            app = create_app(
                settings=_settings(directory.name, profile="interactive"),
                provider=FakeProvider(chunks=("ok",)),
            )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        container = app.state.container

        self.assertEqual("interactive", container.settings.run_budget_profile)
        # 装配期只有两处取档：交互网关与任务执行器（顺序即代码顺序）。
        self.assertEqual(["interactive", "task"], calls)
        self.assertIs(
            container.settings.interactive_budget,
            container.runtime_v2_gateway._run_budget,
        )
        # 任务/无人值守执行器（经 TaskWorker 持有）拿到的是**任务档**，不是交互档。
        self.assertIs(
            container.settings.task_budget,
            container.task_worker._task_executor._run_budget,
        )

    async def test_long_profile_is_consumed_by_the_interactive_gateway(self) -> None:
        """选 `long` → 交互网关拿到 long 档；任务执行器**照旧** task 档（CE-5）。"""
        calls: list[str] = []
        real = AppSettings.resolve_run_budget

        def _spy(self, run_class: str):
            calls.append(run_class)
            return real(self, run_class)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch.object(AppSettings, "resolve_run_budget", _spy):
            app = create_app(
                settings=_settings(directory.name, profile="long"),
                provider=FakeProvider(chunks=("ok",)),
            )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        container = app.state.container

        self.assertEqual("long", container.settings.run_budget_profile)
        self.assertEqual(["long", "task"], calls)
        # `long` 档真的被消费了（这就是"死档接活"的装配期证据）。
        long_budget = container.runtime_v2_gateway._run_budget
        self.assertIs(container.settings.long_budget, long_budget)
        self.assertEqual(14_400.0, long_budget.hard_seconds)
        self.assertEqual(3_600.0, long_budget.soft_seconds)
        self.assertEqual(10.0, long_budget.cost_cap_usd)
        # CE-5：任务/无人值守档没有被并进交互档。
        # 任务/无人值守执行器（经 TaskWorker 持有）拿到的是**任务档**，不是交互档。
        self.assertIs(
            container.settings.task_budget,
            container.task_worker._task_executor._run_budget,
        )

    async def test_task_tier_is_independent_of_every_profile(self) -> None:
        """三档选择器都不动任务 run 的档（只影响交互 run 是硬契约）。"""
        for profile in ("interactive", "task", "long"):
            with self.subTest(profile=profile):
                container = await self._container(profile=profile)
                self.assertIs(
                    container.settings.task_budget,
                    container.task_worker._task_executor._run_budget,
                )

    async def test_interactive_run_gets_the_profile_tier(self) -> None:
        """行为断言：选 `long` 后，一次**真实交互 run** 的执行器拿到 long 档。

        为什么绕道执行器：网关把 `self._run_budget` 交给每个交互 run 新构造的执行器
        （`gateway_impl/runs.py`），所以"运行真正拿到哪一档"只有看执行器才知道。
        这里让 spy 调用真正的 `__init__`，跑一条真 run（fake provider），
        读它构造出来后挂着的 `_run_budget`。
        """
        container = await self._container(profile="long")
        gateway = container.runtime_v2_gateway
        conversation = container.chat_repository.create_conversation()

        captured: list[object] = []
        real_init = AgentRunExecutor.__init__

        def _spy(self, **kwargs):
            real_init(self, **kwargs)
            captured.append(self._run_budget)

        with patch.object(AgentRunExecutor, "__init__", _spy):
            handle = await gateway.send(conversation.id, "跑一次长活")
            self.assertIsNotNone(handle.run_id)

        # 交互 run 的执行器确实是用 long 档构造的。
        self.assertTrue(captured, "交互 run 应该构造了一个执行器")
        self.assertIs(container.settings.long_budget, captured[0])

        # 收尾：等运行结束（fake provider 立刻返回），不留下悬挂任务。
        for _ in range(200):
            if not gateway.has_active_run(conversation.id):
                break
            await asyncio.sleep(0.01)
        self.assertFalse(gateway.has_active_run(conversation.id))


class RunBudgetProfileConstantsTest(unittest.TestCase):
    def test_profiles_are_exactly_the_resolvable_run_classes(self) -> None:
        from endless_task.api.app_settings import RUN_BUDGET_PROFILES

        self.assertEqual(("interactive", "task", "long"), RUN_BUDGET_PROFILES)
        self.assertEqual(set(RUN_BUDGET_PROFILES), set(DEFAULT_BUDGETS))

    def test_illegal_profile_cannot_even_be_constructed(self) -> None:
        """非法值在**构造期**就抛（装配根连错误的 settings 都拿不到）。"""
        with self.assertRaises(ValueError) as raised:
            AppSettings(
                database_path=Path("/tmp/run-budget-profile.db"),
                run_budget_profile="marathon",
            )
        self.assertIn("ENDLESS_TASK_RUN_BUDGET_PROFILE", str(raised.exception))

    def test_resolve_run_budget_still_rejects_unknown_classes(self) -> None:
        settings = AppSettings(database_path=Path("/tmp/run-budget-profile-2.db"))
        with self.assertRaises(ValueError):
            settings.resolve_run_budget("marathon")


if __name__ == "__main__":
    unittest.main()
