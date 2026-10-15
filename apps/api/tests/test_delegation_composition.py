"""M4A DR-2 slice 3: composition-root delegation wiring (flag gated)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_ledger import TraceContext


def _settings(
    directory: str,
    *,
    delegation: bool,
    max_child_hard_seconds: float | None = None,
    max_child_cost_usd: float | None = None,
    max_queued_children: int | None = None,
    child_wakeup_throttle_seconds: float | None = None,
    child_default_timeout_seconds: float | None = None,
) -> AppSettings:
    overrides: dict[str, float] = {}
    if max_child_hard_seconds is not None:
        overrides["max_child_hard_seconds"] = max_child_hard_seconds
    if max_child_cost_usd is not None:
        overrides["max_child_cost_usd"] = max_child_cost_usd
    if max_queued_children is not None:
        overrides["max_queued_children"] = max_queued_children
    if child_wakeup_throttle_seconds is not None:
        overrides["child_wakeup_throttle_seconds"] = child_wakeup_throttle_seconds
    if child_default_timeout_seconds is not None:
        overrides["child_default_timeout_seconds"] = child_default_timeout_seconds
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        tool_platform_v2_enabled=True,
        delegation_mode=("readonly" if delegation else "0"),
        **overrides,
    )


def _delegation_request():
    """协调器只用请求上的 run/conversation 标识定位父运行（其余字段不碰）。"""
    return SimpleNamespace(
        run_id="run_parent",
        conversation_id="conv_parent",
        correlation_id="corr_parent",
        call_id="call_parent",
    )


def _spawn_spec(timeout_seconds: float) -> SpawnSpec:
    return SpawnSpec(
        task="看看仓库测试现状",
        expected_output=ChildOutputSchema(name="out", schema={"type": "object"}),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=timeout_seconds,
        parent_run_id="run_parent",
        parent_tool_call_id="call_parent",
    )


class DelegationCompositionTest(unittest.IsolatedAsyncioTestCase):
    async def _container_app(
        self, *, delegation: bool, throttle_seconds: float | None = None
    ):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=_settings(
                    directory,
                    delegation=delegation,
                    child_wakeup_throttle_seconds=throttle_seconds,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    yield client, app
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_flag_off_registers_no_delegation_tools(self) -> None:
        async for client, app in self._container_app(delegation=False):
            response = await client.get("/capabilities")
            payload = response.json()
            self.assertEqual(False, payload["delegation"]["enabled"])
            self.assertIsNone(payload["delegation"]["mode"])
            container = app.state.container
            definitions = {
                item.name for item in container.tool_registry.definitions()
            }
            self.assertNotIn("spawn_agent", definitions)
            self.assertNotIn("list_agents", definitions)
            self.assertNotIn("steer_agent", definitions)
            self.assertIsNone(container.delegation_handler)
            self.assertIsNone(container.child_wakeup_service)

    async def test_flag_on_registers_tools_and_echoes_capability(self) -> None:
        async for client, app in self._container_app(delegation=True):
            response = await client.get("/capabilities")
            payload = response.json()
            self.assertEqual(True, payload["delegation"]["enabled"])
            self.assertEqual("readonly", payload["delegation"]["mode"])
            container = app.state.container
            definitions = {
                item.name for item in container.tool_registry.definitions()
            }
            self.assertIn("spawn_agent", definitions)
            self.assertIn("query_agent", definitions)
            self.assertIn("cancel_agent", definitions)
            self.assertIn("list_agents", definitions)
            self.assertIn("steer_agent", definitions)
            self.assertIsNotNone(container.delegation_handler)
            # 切片 9 M2：唤醒服务是进程级单例，必须由装配根真正注进容器与协调器
            # ——漏注入不会报错，只会让"子运行结束自动回流"静默失效。
            wakeup = container.child_wakeup_service
            self.assertIsNotNone(wakeup)
            self.assertIs(
                wakeup,
                container.delegation_handler._child_wakeup,
            )
            self.assertTrue(wakeup._enabled)

    async def test_child_wakeup_throttle_setting_reaches_the_service(self) -> None:
        """M3-9：生产默认（60 秒）必须真走到服务实例——服务层默认是 0 = 不节流。

        这条是"生产默认开、服务层默认关"这个分工的唯一证据：装配根漏传
        `throttle_seconds` 不会报错，只会让节流静默失效（服务拿 0 默认值）。
        """
        async for client, app in self._container_app(delegation=True):
            container = app.state.container
            wakeup = container.child_wakeup_service
            self.assertIsNotNone(wakeup)
            self.assertEqual(
                60.0, container.settings.child_wakeup_throttle_seconds
            )
            self.assertEqual(
                container.settings.child_wakeup_throttle_seconds,
                wakeup._throttle_seconds,
            )

        async for client, app in self._container_app(
            delegation=True, throttle_seconds=0.25
        ):
            wakeup = app.state.container.child_wakeup_service
            self.assertIsNotNone(wakeup)
            self.assertEqual(0.25, wakeup._throttle_seconds)

    async def test_capability_provider_does_not_crash_on_v1_definitions(self) -> None:
        # Regression (real-provider E2E, 06 §27): the composition capability
        # provider read tool.definition.required_capabilities on v1 legacy
        # ToolDefinitions (no such field) and raised AttributeError on every
        # real spawn; it now adapts through LegacyToolAdapter like the v2
        # surface provider.
        async for client, app in self._container_app(delegation=True):
            container = app.state.container
            from endless_task.tool_platform import ToolExecutionRequest

            request = ToolExecutionRequest(
                tool_name="spawn_agent",
                call_id="call_1",
                arguments={"task": "x"},
                conversation_id="conv_unused",
                run_id="run_unused",
                model_turn_id="turn_1",
                correlation_id="corr_1",
                cancellation=CancellationToken(),
                created_at="2026-09-05T00:00:00Z",
            )
            granted = container.delegation_handler._capability_provider(request)
            self.assertIsInstance(granted, frozenset)
            # No crash on v1 definitions is the regression; an unbound
            # conversation correctly loses agent.delegate (fail-closed
            # delegate gate, AP-105a parity).
            self.assertNotIn("agent.delegate", granted)
            self.assertTrue(granted <= {"agent.delegate", "external.action", "session.query", "workspace.read", "workspace.write", "workspace.delete", "process.spawn", "process.signal"})

    async def test_delegation_executor_budget_is_capped_by_the_setting(self) -> None:
        """M3-1/M3-3：装配根必须把墙上限与成本上限都交给派生预算。

        为什么在装配层测而不是只测纯函数：`child_run_budget` 的钳制与 cap 已经有
        单测，但"设置值真的走到执行器"这一段此前没有覆盖——漏传 `max_hard_seconds`
        不会报错，只会让模型声明的 24 小时墙原样生效（正是 M3-1 要修的洞）；漏传
        `cost_cap_usd` 同样不会报错，只会让子运行退回"没有成本上界"（M3-3）。
        `stop_on_cost` 一并钉住：子档的成本是**上界**（到顶优雅停止），不是提示。
        """
        from endless_task.runtime_v2 import AgentRunExecutor

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=_settings(
                    directory,
                    delegation=True,
                    max_child_hard_seconds=120.0,
                    max_child_cost_usd=2.5,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                handler = app.state.container.delegation_handler
                # 走协调器拿到内核，再用协调器自己构造的 RunCommand 调执行器工厂
                # ——命令里的 deadline 与生产路径同源（monotonic() + timeoutSeconds）。
                coordinator = handler._coordinator_for(_delegation_request())
                built = []
                with patch.object(
                    AgentRunExecutor,
                    "__init__",
                    lambda self, **kwargs: built.append(kwargs),
                ):
                    for timeout_seconds in (86_400.0, 60.0):
                        command = coordinator._build_run_command(
                            _spawn_spec(timeout_seconds), "child_run_1"
                        )
                        coordinator._kernel._executor_builder(command)

                self.assertEqual(2, len(built))
                # 模型声明 24 小时 → 被设置钳到 120 秒（墙上限生效；钳制是精确值，
                # 与派生时的 monotonic 无关）
                self.assertEqual(120.0, built[0]["run_budget"].hard_seconds)
                # 模型声明 60 秒 < 上限 → 按"还剩多少"保留（命令构造到执行器取预算
                # 之间会流逝一点时间，所以用近似；关键是它没有被抬到上限）
                self.assertAlmostEqual(
                    60.0, built[1]["run_budget"].hard_seconds, delta=0.5
                )
                # M3-3：成本上限按设置进预算，且子档带"到顶真停"的策略位。
                for captured in built:
                    budget = captured["run_budget"]
                    self.assertEqual(2.5, budget.cost_cap_usd)
                    self.assertIs(True, budget.stop_on_cost)
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_child_default_timeout_setting_reaches_the_handler(self) -> None:
        """切片 10 T1 Step 3：设置 → 装配根 → handler 的默认墙（默认仍 120）。

        为什么在装配层测：`wiring.py` 过去**不传** `default_timeout_seconds`，
        handler 就用自己的构造默认 120 秒。漏传不会报错，只会让"模型忘了写
        timeoutSeconds"的子运行依旧只活两分钟——设置看起来可配、实际不生效。
        """
        for configured, expected in ((None, 120.0), (300.0, 300.0)):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                app = create_app(
                    settings=_settings(
                        directory,
                        delegation=True,
                        child_default_timeout_seconds=configured,
                    ),
                    provider=FakeProvider(chunks=("ok",)),
                )
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                try:
                    handler = app.state.container.delegation_handler
                    self.assertIsNotNone(handler)
                    self.assertEqual(expected, handler._default_timeout_seconds)
                finally:
                    await lifespan.__aexit__(None, None, None)

    async def test_child_default_timeout_is_still_capped_by_the_hard_ceiling(self) -> None:
        """两向都钉：默认墙调大 → 被 `max_child_hard_seconds` 截住；调小 → 立刻生效。

        这条与上一条合起来才是 Step 3 的完整验收：设置既"到得了"，
        也仍然"封得住"——上限那道闸在派生预算那一步，与默认值无关。
        """
        from endless_task.runtime_v2 import AgentRunExecutor

        for configured, ceiling, expected in (
            (7200.0, 3600.0, 3600.0),  # 调大 → 被上限截住
            (300.0, 3600.0, 300.0),    # 调小 → 生效
        ):
            with self.subTest(configured=configured), tempfile.TemporaryDirectory() as directory:
                app = create_app(
                    settings=_settings(
                        directory,
                        delegation=True,
                        max_child_hard_seconds=ceiling,
                        child_default_timeout_seconds=configured,
                    ),
                    provider=FakeProvider(chunks=("ok",)),
                )
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                try:
                    handler = app.state.container.delegation_handler
                    coordinator = handler._coordinator_for(_delegation_request())
                    command = coordinator._build_run_command(
                        # 模型没写 timeoutSeconds → handler 用设置里的默认墙。
                        _spawn_spec(handler._default_timeout_seconds),
                        "child_run_1",
                    )
                    built: list[dict] = []
                    with patch.object(
                        AgentRunExecutor,
                        "__init__",
                        lambda self, **kwargs: built.append(kwargs),
                    ):
                        coordinator._kernel._executor_builder(command)
                    self.assertEqual(1, len(built))
                    self.assertAlmostEqual(
                        expected,
                        built[0]["run_budget"].hard_seconds,
                        delta=0.5,
                    )
                finally:
                    await lifespan.__aexit__(None, None, None)

    async def test_delegation_queue_cap_is_reachable_from_the_setting(self) -> None:
        """M3 Task 7：设置里的绝对排队上限必须真的走到协调器的 ``_batch_limits``。

        为什么在装配层测：漏传 ``batch_limits`` 不会报错，只会让协调器退回默认
        ``ChildBatchLimits()``——设置看起来可配，实际不生效。
        """
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=_settings(
                    directory,
                    delegation=True,
                    max_queued_children=64,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                handler = app.state.container.delegation_handler
                coordinator = handler._coordinator_for(_delegation_request())
                self.assertEqual(64, coordinator._batch_limits.max_queued_children)
                # 其余两个容量位不受影响：这个设置只加绝对排队上限。
                self.assertEqual(4, coordinator._batch_limits.max_concurrent_children)
                self.assertEqual(8, coordinator._batch_limits.max_children_per_parent)
            finally:
                await lifespan.__aexit__(None, None, None)


class DelegationDefaultTest(unittest.TestCase):
    def test_default_is_readonly_after_real_provider_validation(self) -> None:
        # W6-9: real deepseek E2E passed (child run + concurrent batch +
        # delegate gate, 06 27-28) -> delegation_mode defaults to
        # "readonly" (documented flip per 02 11.4, 06 29).
        import tempfile
        from pathlib import Path

        settings = AppSettings(database_path=Path(tempfile.mkdtemp()) / "x.db")
        self.assertEqual("readonly", settings.delegation_mode)


class DelegationFlagParsingTest(unittest.TestCase):
    def test_strict_mode_parse(self) -> None:
        from endless_task.api.app_settings import _parse_delegation_mode

        self.assertEqual("0", _parse_delegation_mode("0"))
        self.assertEqual("readonly", _parse_delegation_mode("readonly"))
        self.assertEqual("readonly", _parse_delegation_mode("1"))
        # M4B P2a: isolated_write parses (startup additionally requires the
        # execution-backend enforcement seam).
        self.assertEqual("isolated_write", _parse_delegation_mode("isolated_write"))
        with self.assertRaises(ValueError):
            _parse_delegation_mode("bogus")


    def test_isolated_write_requires_enforcement_at_startup(self) -> None:
        # M4B P2a: no silent degradation — isolated_write without the
        # enforcement seam fails startup.
        from endless_task.api import AppSettings, create_app

        with self.assertRaises(ValueError):
            create_app(
                settings=AppSettings(
                    database_path=Path(tempfile.mkdtemp()) / "gate.db",
                    delegation_mode="isolated_write",
                    execution_backend_mode="",
                )
            )

    def test_isolated_write_builds_with_enforcement(self) -> None:
        from endless_task.api import AppSettings, create_app

        app = create_app(
            settings=AppSettings(
                database_path=Path(tempfile.mkdtemp()) / "gate2.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                delegation_mode="isolated_write",
                execution_backend_mode="local",
            )
        )
        self.assertIsNotNone(app.state.container)


if __name__ == "__main__":
    unittest.main()
