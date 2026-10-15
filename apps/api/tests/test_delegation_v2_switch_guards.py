"""切片 9 第四批 Task 2：v2 切换后的**静默退化守卫**（规格 §4-4 的三条 + §4-3 的不对称）。

背景：Task 1（`ea2a315`）把生产注册对象换成了
:class:`endless_task.delegation.legacy_tools.V2DelegationToolAsRegistered`——一个
**v1 形状**（``definition`` + ``execute(call, token)``）的薄 shim，内层是
``delegation/agent_tools.py`` 的**原生 v2 对象**。多个消费者读的正是这个 v1 形状；
它们**今天没有一条红用例**，坏了只会静默退化。本模块逐条把耦合钉成断言。

守卫与规格的对应：

* **G1（effect 仍在 v1 词表）**：已由 Task 1 的
  ``tests/test_delegation_v2_wiring.py::ProductionDelegationRegistryTest
  ::test_registered_delegation_tools_wrap_the_native_v2_class`` 钉住——本模块**不重复**。
* **G2（§4-4②）**：``runtime_v2/execution_impl/runner.py:_side_effect_evidence``
  按名从 **v1 注册表**读 ``definition.effect``，而且外层是 ``except Exception: continue``
  （静默）→ 见 :class:`SideEffectEvidenceGuardTest`。
* **G3（§4-4③）**：``api/wiring.py`` 的 ``_v2_unattended_tool_filter`` /
  ``_delegation_child_tool_filter`` **无保护**读
  ``selected_tool_registry.resolve(name).definition.approval_mode``
  → 见 :class:`WiringToolFilterGuardTest`。
* **G4（§4-4④）**：``requires_explicit_confirmation`` 与切换前一致
  → 见 :class:`ExplicitConfirmationGuardTest`。
* **G5（§4-3）**：准入元数据的**故意不对称**——改
  ``tool_platform/legacy_policy.py`` 的表会改管线的准入，改 v2 定义里的同名字段**不会**
  → 见 :class:`AdmissionMetadataAsymmetryGuardTest`。

四条守卫归为同一主题，在本模块集中覆盖。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping
from unittest.mock import PropertyMock, patch

import httpx

from endless_task.agent_platform import AgentPlatformError
from endless_task.api import AppSettings, create_app
from endless_task.delegation import (
    CancelAgentTool,
    ListAgentsTool,
    QueryAgentTool,
    SpawnAgentTool,
    SteerAgentTool,
    V2DelegationToolAsRegistered,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import ProviderToolCall
from endless_task.runtime_v2.execution_impl.coordinator import ToolExecutionCoordinator
from endless_task.tool_platform import (
    BUILTIN_LEGACY_TOOL_POLICIES,
    ApprovalPolicy,
    DefaultToolPipeline,
    LegacyToolAdapter,
    ResolvedToolCall,
    ToolBatchRequest,
    ToolEffect as V2ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolScheduler,
    legacy_policy,
)
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolDefinition,
    ToolEffect,
)
from tests.fixtures.workspace_client import create_bound_conversation

#: 生产在册的委派五件（被 shim 持有的原生 v2 类）。
V2_TOOL_CLASSES = {
    "spawn_agent": SpawnAgentTool,
    "query_agent": QueryAgentTool,
    "cancel_agent": CancelAgentTool,
    "list_agents": ListAgentsTool,
    "steer_agent": SteerAgentTool,
}
DELEGATION_TOOL_NAMES = tuple(V2_TOOL_CLASSES)

#: **切换前的强制确认判定值**（冻结字面量）。
#: 切换前注册的五个 v1 ``*LegacyTool`` 类**没有覆写** ``requires_explicit_confirmation``，
#: 继承到的就是 ``RegisteredTool`` 协议的默认实现（``return False``），所以切换前的值
#: 恰好是协议默认值——Task 1 报告 §3.5 的两条独立测量（协议默认 + 五个旧类都没覆写）
#: 记录了这一点。五个旧类已在 Task 3 退役，这里不再构造它们：把"今天"的值固化成
#: 字面量，并以 ``RegisteredTool.requires_explicit_confirmation`` 本身为准。
PRE_SWITCH_CONFIRMATION_VALUE = False


@asynccontextmanager
async def production_app(database_path: Path) -> AsyncIterator[Any]:
    """真装配根（生产默认：delegation=readonly + v2 管线开），不跑任何 run。"""
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            delegation_mode="readonly",
            tool_platform_v2_enabled=True,
        ),
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client, app.state.container
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


_RUN = SimpleNamespace(
    id="run_parent_1", conversation_id="conversation_1", correlation_id=None
)
_MODEL_TURN = SimpleNamespace(
    id="turn_1", run_id="run_parent_1", created_at="2026-09-03T00:00:00Z"
)


def _production_tool_call(tool_name: str, arguments: Mapping[str, Any]):
    """用**生产代码本身**（``ToolExecutionCoordinator._tool_call``）造 v1 ``ToolCall``。"""
    item = SimpleNamespace(
        provider_call=SimpleNamespace(
            id="call_1", name=tool_name, arguments=dict(arguments)
        )
    )
    return ToolExecutionCoordinator._tool_call(item, _RUN, _MODEL_TURN)


class _MinimalHandler:
    """只满足装配期契约的 handler（``ProjectionBoundaryFailsLoudlyTest`` 构造用）。"""

    async def spawn_child(self, request: ToolExecutionRequest):  # pragma: no cover
        raise AssertionError("装配边界用例不应真的执行到 handler")


def _non_read_only_names(registry) -> list[str]:
    return sorted(
        definition.name
        for definition in registry.definitions()
        if definition.effect is not ToolEffect.READ_ONLY
    )


class SideEffectEvidenceGuardTest(unittest.IsolatedAsyncioTestCase):
    """G2（§4-4②）：``runner._side_effect_evidence`` 仍按名从 v1 注册表取到 effect。

    ``AgentRunExecutor._side_effect_evidence``（``runner.py``）的实现是::

        try:
            definition = self._tool_registry.resolve(call.name).definition
        except Exception:
            continue
        effect = getattr(definition.effect, "value", definition.effect)
        if effect and str(effect) != "read_only":
            evidence.append(f"{call.name}（{effect}）")

    工具一旦不在 v1 注册表里（或被换成另一族的对象），这条链**静默**少一条证据，
    没有任何既有用例会红。本用例走**真装配根的真执行器 + 真注册表**，证明：

    1. 同一个注册表上非 ``read_only`` 的工具**仍进证据**（函数是活的，
       ``except Exception: continue`` 没有把整条链吞掉）；
    2. 五个委派工具经注册表解析到的是 shim，其投影 effect 是 **v1** ``READ_ONLY``，
       所以按设计不产生副作用证据；
    3. 决定性一步：把**同一个注册对象**的定义临时换成非 ``read_only`` 的 v1 定义，
       证据必须逐字带上「工具名（effect）」——说明该 effect 确实是按名从注册表里读出来的。

    若把注册对象换成旧 v1 类或裸 v2 对象（= 回退/绕过这次切换），第 3 步的
    ``patch.object(V2DelegationToolAsRegistered, ...)`` 就不再作用于注册对象，
    证据会静默变空 → 本用例红。
    """

    async def test_a_real_delegation_tool_still_feeds_side_effect_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with production_app(Path(directory) / "g2.db") as (client, container):
                executor = container.task_worker._task_executor
                registry = executor._tool_registry

                # 控制组：同一注册表上的非 read_only 工具仍进证据（函数没被静默吞掉）。
                effectful = _non_read_only_names(registry)
                self.assertTrue(effectful, "注册表里应至少有一个非 read_only 工具")
                control = effectful[0]
                control_effect = registry.resolve(control).definition.effect.value
                self.assertEqual(
                    [f"{control}（{control_effect}）"],
                    executor._side_effect_evidence(
                        [ProviderToolCall(id="call_0", name=control, arguments={})]
                    ),
                )

                for name in DELEGATION_TOOL_NAMES:
                    with self.subTest(tool=name):
                        call = ProviderToolCall(id="call_1", name=name, arguments={})
                        # 决定性断言：把**同一个注册对象**的定义临时换成非 read_only 的
                        # v1 定义，证据必须逐字带上「工具名（effect）」——说明该 effect
                        # 确实是按名从注册表里读出来的。若注册对象不在 v1 注册表里、或
                        # 被换成另一族的对象，``except Exception: continue`` 会**静默**
                        # 吞掉，这里只会看到空列表；本用例红，而其它用例全绿。
                        fake = ToolDefinition(
                            name=name,
                            description="守卫用的定义：只改 effect，验证证据是活的。",
                            input_schema={"type": "object"},
                            effect=ToolEffect.LOCAL_WRITE,
                            approval_mode=ToolApprovalMode.AUTO,
                        )
                        with patch.object(
                            V2DelegationToolAsRegistered,
                            "definition",
                            new_callable=PropertyMock,
                            return_value=fake,
                        ):
                            self.assertEqual(
                                [f"{name}（local_write）"],
                                executor._side_effect_evidence([call]),
                                "切换后委派工具的 effect 没有被证据链按名取到",
                            )

                        # 身份/投影耦合（真代码下成立）：注册的就是 shim，投影 effect
                        # 是 v1 READ_ONLY，所以按设计不产生副作用证据。
                        registered = registry.resolve(name)
                        self.assertIsInstance(
                            registered, V2DelegationToolAsRegistered
                        )
                        self.assertIs(
                            ToolEffect.READ_ONLY, registered.definition.effect
                        )
                        self.assertEqual([], executor._side_effect_evidence([call]))


class WiringToolFilterGuardTest(unittest.IsolatedAsyncioTestCase):
    """G3（§4-4③）：``wiring`` 两个过滤器无保护读 ``definition.approval_mode``。

    ``api/wiring.py`` 的 ``_v2_unattended_tool_filter`` 与
    ``_delegation_child_tool_filter`` 都直接
    ``selected_tool_registry.resolve(name).definition.approval_mode``——**没有 try**。
    注册表面一旦不是 v1 形状（例如裸注册 v2 对象，v2 只有 ``.approval``），
    这里就是 ``AttributeError``；flag on（生产默认）时两个过滤器是惰性的，
    本地全绿、flag off 的 rollback 演练路径才炸。

    本用例在**切换后的真装配**下取到这两个真闭包（无人值守档执行器与子运行执行器各自的
    ``tool_filter_provider``），断言它们：① 对注册表里每个名字都不抛；
    ② 语义与 v1 定义一致（``REQUIRED`` 被挡、``AUTO`` 放行）；
    ③ 子运行的模型面仍**不含**那五个委派工具（与今天一致，深度=1 门）。
    """

    def _child_executor(self, container):
        """经真 handler 的 kernel provider 造一个真子运行执行器，取它的过滤器。

        ``_delegation_executor_builder`` 是 ``_build_container`` 里的闭包，只经
        ``RuntimeV2AgentKernel(executor_builder=...)`` 暴露；这里按生产的构造形状调一次
        （只构造对象，不跑 run）。
        """
        kernel = container.delegation_handler._kernel_provider(None)
        command = SimpleNamespace(deadline_monotonic=time.monotonic() + 60.0)
        return kernel._executor_builder(command)

    async def test_both_wiring_filters_do_not_raise_and_child_surface_is_unchanged(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with production_app(Path(directory) / "g3.db") as (client, container):
                registry = container.tool_registry
                conversation_id = (await create_bound_conversation(client))["id"]

                unattended_filter = (
                    container.task_worker._task_executor
                    ._tool_coordinator._tool_filter_provider
                )
                child_executor = self._child_executor(container)
                child_filter = (
                    child_executor._tool_coordinator._tool_filter_provider
                )

                names = [definition.name for definition in registry.definitions()]
                self.assertIn("spawn_agent", names)

                unattended_allows = unattended_filter(conversation_id)
                child_allows = child_filter(conversation_id)

                # ① 不抛：两个过滤器对每个注册名都可调用（v1 形状的 approval_mode 读得到）。
                #    这是本守卫的核心观察点——裸注册 v2 对象时，下面这一句会在
                #    ``wiring.py`` 的过滤器里直接 AttributeError（v2 只有 ``.approval``）。
                for name in names:
                    with self.subTest(filter="unattended", tool=name):
                        self.assertIsInstance(unattended_allows(name), bool)
                    with self.subTest(filter="child", tool=name):
                        self.assertIsInstance(child_allows(name), bool)

                required = sorted(
                    definition.name
                    for definition in registry.definitions()
                    if getattr(definition, "approval_mode", None)
                    is ToolApprovalMode.REQUIRED
                )
                self.assertTrue(required, "注册表里应至少有一个 REQUIRED 工具")

                # ② 语义与今天的 v1 定义一致：REQUIRED 被挡；委派五件是 AUTO。
                for name in required:
                    with self.subTest(filter="unattended", required=name):
                        self.assertIs(False, unattended_allows(name))
                for name in DELEGATION_TOOL_NAMES:
                    with self.subTest(tool=name):
                        self.assertIs(True, unattended_allows(name))
                        # ③ 子运行面仍排除委派五件（不改行为）。
                        self.assertIs(False, child_allows(name))

                # ③ 再走一遍真实的模型面投影（flag on 的 ``definitions``）：深度=1 门生效。
                projected = {
                    definition.name
                    for definition in child_executor._tool_coordinator.definitions(
                        conversation_id
                    )
                }
                self.assertEqual(
                    set(),
                    projected & set(DELEGATION_TOOL_NAMES),
                    "委派子运行的模型面里出现了委派工具（深度=1 门失效）",
                )
                self.assertTrue(projected, "子运行模型面不应为空（否则断言会空转）")


class ExplicitConfirmationGuardTest(unittest.IsolatedAsyncioTestCase):
    """G4（§4-4④）：``requires_explicit_confirmation`` 与切换前**逐值一致**。

    ``ToolExecutionCoordinator._prepare_tool_batch`` 的读法是::

        confirmation_judge = getattr(tool, "requires_explicit_confirmation", None)
        if callable(confirmation_judge):
            try:
                force_confirm = bool(confirmation_judge(call))
            except Exception:
                force_confirm = True          # ← 抛异常会**静默**变成"强制确认"

    切换前五个 v1 类没有覆写它，继承的是 ``RegisteredTool`` 协议的默认实现
    （``tooling/registry.py`` 里 ``return False``）；v2 原生类**没有这个方法**
    （裸注册会让 ``getattr`` 拿到 ``None`` → 不强制确认）。本用例先测出**今天**的值
    （协议默认 + :data:`PRE_SWITCH_CONFIRMATION_VALUE` 字面量——五个旧 v1 类已在
    Task 3 退役，不再现场构造），再断言**生产注册对象**（shim）经协调器同一条读法
    给出同一个值。
    """

    async def test_registered_objects_match_the_pre_switch_confirmation_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with production_app(Path(directory) / "g4.db") as (client, container):
                registry = container.tool_registry
                coordinator = container.task_worker._task_executor._tool_coordinator
                call = _production_tool_call("spawn_agent", {"task": "x"})

                # 「今天」的独立测量（**唯一一处**）：协议默认实现本身返回 False。
                # Task 2 版是真测量（现场构造五个 v1 旧类并逐个调用）；Task 3 退役
                # v1 类之后那一步必然退化成字面量，所以这里去掉**同义反复**的
                # `assertIs(PRE_SWITCH_CONFIRMATION_VALUE, False)` 与换名赋值
                # `legacy_value = PRE_SWITCH_CONFIRMATION_VALUE`，只留**有判别力**的
                # 部分：把协议默认的**实测调用结果**与冻结字面量对齐，再用它当
                # "切换前"的基准（协议默认漂移、或 shim 返回值与它不一致，都会红）。
                pre_switch_value = RegisteredTool.requires_explicit_confirmation(
                    None, call
                )
                self.assertIs(PRE_SWITCH_CONFIRMATION_VALUE, pre_switch_value)
                for name in DELEGATION_TOOL_NAMES:
                    with self.subTest(tool=name):
                        # 切换后的生产注册对象，用协调器**同一条**读法。
                        registered = registry.resolve(name)
                        judge = getattr(
                            registered, "requires_explicit_confirmation", None
                        )
                        self.assertTrue(
                            callable(judge),
                            f"{name}: 注册对象缺少 requires_explicit_confirmation，"
                            "协调器会静默降级为「永不强制确认」",
                        )
                        self.assertIs(
                            pre_switch_value,
                            bool(judge(call)),
                            f"{name}: 强制确认判定与切换前不一致",
                        )

                        # 再经**真执行器的协调器**读一遍：与今天逐值一致。
                        resolved = coordinator._resolve_tool(name)
                        self.assertIs(
                            pre_switch_value,
                            bool(resolved.requires_explicit_confirmation(call)),
                        )


class AdmissionMetadataAsymmetryGuardTest(unittest.IsolatedAsyncioTestCase):
    """G5（§4-3）：准入元数据的**故意不对称**——防奇偶守卫**假绿**。

    ``execution_mode`` / ``idempotency`` / ``required_capabilities`` **装不进**
    v1 ``ToolDefinition``，所以准入的**唯一源**是按工具名索引的审计表
    ``tool_platform/legacy_policy.py``:``BUILTIN_LEGACY_TOOL_POLICIES``；
    v2 定义（``delegation/agent_tools.py``）里的同名字段只被奇偶守卫比对。
    这条不对称是**有意**的（本批不改准入源），必须写成断言而不是注释：

    * 改表里 ``spawn_agent`` 的 ``execution_mode`` → 管线算出的准入模式跟着表变；
    * 改 v2 定义里的同名字段（哪怕与表相反）→ 管线算出的准入模式**不变**。

    "管线算出的准入模式"取 ``DefaultToolPipeline.execute`` 里的
    ``ScheduledCall(mode=call.tool.definition.execution_mode)``——即调度器的准入输入。
    """

    async def _admission_modes(self, tool, *, tool_name: str) -> list[ToolExecutionMode]:
        call = _production_tool_call(tool_name, {"task": "调研 A"})
        adapter = LegacyToolAdapter(tool)
        request = ToolExecutionRequest(
            call_id=call.id,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            conversation_id=call.conversation_id,
            run_id=call.response_variant_id,
            model_turn_id=call.turn_id,
            correlation_id=call.response_variant_id,
            cancellation=CancellationToken(),
            created_at=call.created_at,
            legacy_turn_id=call.turn_id,
            legacy_response_variant_id=call.response_variant_id,
        )
        captured: list[ToolExecutionMode] = []

        async def spy(scheduler_self, calls):
            captured.extend(scheduled.mode for scheduled in calls)
            return ()

        with patch.object(ToolScheduler, "execute", spy):
            pipeline = DefaultToolPipeline(scheduler=ToolScheduler(max_concurrent=2))
            await pipeline.execute(
                ToolBatchRequest(
                    calls=(ResolvedToolCall(request=request, tool=adapter),)
                )
            )
        return captured

    async def test_pipeline_admission_follows_the_policy_table_not_the_v2_definition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with production_app(Path(directory) / "g5.db") as (client, container):
                handler = container.delegation_handler

                # （1）生产默认：表里 spawn_agent = EXCLUSIVE，管线据此准入。
                registered = container.tool_registry.resolve("spawn_agent")
                self.assertIs(
                    ToolExecutionMode.EXCLUSIVE,
                    LegacyToolAdapter(registered).definition.execution_mode,
                )
                self.assertEqual(
                    [ToolExecutionMode.EXCLUSIVE],
                    await self._admission_modes(registered, tool_name="spawn_agent"),
                )

                # （2）故意把 **v2 定义** 改成与表相反（PARALLEL）：准入**不跟** v2 定义。
                inner = V2_TOOL_CLASSES["spawn_agent"](handler=handler)
                object.__setattr__(
                    inner,
                    "definition",
                    replace(
                        inner.definition,
                        execution_mode=ToolExecutionMode.PARALLEL,
                    ),
                )
                self.assertIs(
                    ToolExecutionMode.PARALLEL, inner.definition.execution_mode
                )
                shim = V2DelegationToolAsRegistered(inner)
                self.assertEqual(
                    [ToolExecutionMode.EXCLUSIVE],
                    await self._admission_modes(shim, tool_name="spawn_agent"),
                    "准入读到了 v2 定义的 execution_mode（两处真相的假绿要防的正是它）",
                )

                # （3）改**表**里的同名值：准入随之改变——表才是准入的源。
                mutated = tuple(
                    replace(policy, execution_mode=ToolExecutionMode.PARALLEL)
                    if policy.tool_name == "spawn_agent"
                    else policy
                    for policy in BUILTIN_LEGACY_TOOL_POLICIES
                )
                with patch.object(
                    legacy_policy, "BUILTIN_LEGACY_TOOL_POLICIES", mutated
                ):
                    self.assertIs(
                        ToolExecutionMode.PARALLEL,
                        LegacyToolAdapter(shim).definition.execution_mode,
                    )
                    self.assertEqual(
                        [ToolExecutionMode.PARALLEL],
                        await self._admission_modes(shim, tool_name="spawn_agent"),
                        "改审计策略表没有改变管线准入",
                    )
                # 退出 patch 后必须逐字复原（否则本用例会污染同进程其它用例）。
                self.assertIs(
                    ToolExecutionMode.EXCLUSIVE,
                    LegacyToolAdapter(shim).definition.execution_mode,
                )


class ProjectionBoundaryFailsLoudlyTest(unittest.TestCase):
    """F3：v1 词表装不下的 effect/approval、以及装错对象，必须**装配期大声失败**。

    落点理由：这一组守卫与 G1–G5 同属"shim 的耦合不许静默退化"，所以放在本模块
    （而不是 `test_delegation_v2_wiring.py`——那个文件钉的是"生产注册表里被执行的是
    谁"与"非法入参矩阵"；这里钉的是"投影/装配的边界"）。

    shim 的构造期把 v2 定义投影成 v1 形状，投影函数只走"显式 ``is`` 分支 + 抛
    :class:`AgentPlatformError`"这一条路（``legacy_tools.py:_v1_effect_from_v2`` /
    ``_v1_approval_from_v2`` / ``V2DelegationToolAsRegistered.__init__``）。终审 F 区
    抓出：这三个错误码此前**全库无测试引用**（``grep`` 只命中 src 自身）——将来若有
    人把它改成"未知值回落成 READ_ONLY / AUTO"，不会有任何用例变红。

    **为什么这条要紧**：审批闸门（``GatewayToolApprovalGate._approval_prompt``）拿
    **v1** ``ToolEffect.LOCAL_WRITE`` 比 effect，``api/wiring.py`` 的两个过滤器读
    **v1** ``approval_mode``。若把 v2 独有的 ``PROCESS`` / ``NETWORK`` 静默回落成
    别的影响面、或把 ``ASK`` / ``FORBIDDEN_UNATTENDED`` 静默投影成 ``AUTO``，审批
    文案与影响面判定会**静默给出错误的答案**（"需要审批"变"免审批"），而且注册期
    不报错、下游全绿——所以边界必须大声失败（错误码精确，不阻塞装配的诊断）。
    """

    def _spawn_tool(self) -> SpawnAgentTool:
        """最小可构造的 v2 委派工具（装配期只要求 handler 有 ``spawn_child``）。"""
        return SpawnAgentTool(handler=_MinimalHandler())

    def test_effect_outside_the_v1_vocabulary_fails_loudly_at_assembly(self) -> None:
        for effect in (V2ToolEffect.PROCESS, V2ToolEffect.NETWORK):
            with self.subTest(effect=effect.name):
                tool = self._spawn_tool()
                object.__setattr__(
                    tool, "definition", replace(tool.definition, effect=effect)
                )
                with self.assertRaises(AgentPlatformError) as raised:
                    V2DelegationToolAsRegistered(tool)
                self.assertEqual(
                    "invalid_delegation_tool_effect", raised.exception.code
                )

    def test_approval_outside_the_v1_vocabulary_fails_loudly_at_assembly(self) -> None:
        for approval in (
            ApprovalPolicy.ASK,
            ApprovalPolicy.FORBIDDEN_UNATTENDED,
        ):
            with self.subTest(approval=approval.name):
                tool = self._spawn_tool()
                object.__setattr__(
                    tool, "definition", replace(tool.definition, approval=approval)
                )
                with self.assertRaises(AgentPlatformError) as raised:
                    V2DelegationToolAsRegistered(tool)
                self.assertEqual(
                    "invalid_delegation_tool_approval", raised.exception.code
                )

    def test_assembly_rejects_an_object_that_is_not_a_v2_delegation_tool(self) -> None:
        not_v2 = SimpleNamespace(
            definition="不是 ToolDefinitionV2", execute=lambda request: None
        )
        for label, candidate in (
            ("裸 object", object()),
            ("definition 不是 ToolDefinitionV2", not_v2),
        ):
            with self.subTest(candidate=label):
                with self.assertRaises(AgentPlatformError) as raised:
                    V2DelegationToolAsRegistered(candidate)
                self.assertEqual("invalid_delegation_tool", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
