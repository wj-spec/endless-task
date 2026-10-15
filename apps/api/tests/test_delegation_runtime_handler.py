"""M4A DR-2 slice 1: delegation tools over the coordinator-backed handler."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation.agent_tools import (
    CancelAgentTool,
    QueryAgentTool,
    SpawnAgentTool,
)
from endless_task.delegation.child_run_store import ChildRunState
from endless_task.delegation.runtime_handler import CoordinatorDelegationHandler
from endless_task.runtime.cancellation import CancellationToken
from endless_task.storage import Database, SqliteDelegationChildRunRepository
from endless_task.tool_platform import (
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "memory.read",
        "session.query",
    }
)


class EchoAgentKernel:
    """Minimal AgentKernel that completes children with the task echoed."""

    def __init__(self) -> None:
        self.commands: list[RunCommand] = []
        self.fail_next: AgentPlatformError | None = None

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="调查完成，结论如下。",
            ),
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class GatedEchoAgentKernel(EchoAgentKernel):
    """run 进入后挂住，直到放行——用于检查"还有子运行在跑"的协调器。"""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.started = asyncio.Event()

    async def run(self, command: RunCommand) -> RunOutcome:
        self.started.set()
        await self.gate.wait()
        return await super().run(command)


class SteeringEchoAgentKernel(GatedEchoAgentKernel):
    """挂住（`started_at` 非空 = 真在跑）并记录内核收到的 steer。"""

    def __init__(self, gate: asyncio.Event, *, expected_started: int = 1) -> None:
        super().__init__(gate)
        self.expected_started = expected_started
        self.all_started = asyncio.Event()
        self._started_count = 0
        self.steers: list[tuple[str, AgentMessage]] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        self._started_count += 1
        if self._started_count >= self.expected_started:
            self.all_started.set()
        return await super().run(command)

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        self.steers.append((run_id, message))


class SteeringRaceKernel(SteeringEchoAgentKernel):
    """steer 时内核回"没有活动运行"：钉住"复核库行"那一支。"""

    def __init__(self, gate: asyncio.Event, store, *, terminalize: bool) -> None:
        super().__init__(gate)
        self._store = store
        self._terminalize = terminalize

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        if self._terminalize:
            # 模拟"我们的前置检查通过后、内核调用前，子运行刚好跑完"的竞态。
            self._store.mark_child_terminal(
                run_id, state=ChildRunState.COMPLETED, summary="完成"
            )
        raise AgentPlatformError(
            "invalid_agent_kernel_value", "Cannot steer an inactive child run"
        )


class PerInstanceSteeringKernel(SteeringEchoAgentKernel):
    """复刻生产内核的**实例级**活动运行注册表。

    ``api/wiring.py`` 的 ``_delegation_kernel_provider`` 每个父运行都新建一个
    ``RuntimeV2AgentKernel``，而内核只认自己 ``run()`` 过的运行
    （``RuntimeV2AgentKernel._active`` 是实例级字典），steer/cancel 对不认识的 id
    一律抛 ``invalid_agent_kernel_value``。跨轮次的旧实现在第二轮新建内核，于是它
    对着上一轮启动的子运行一无所知——这个假内核就是钉住"新内核不认识旧子运行"。
    """

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(gate)
        self.active: set[str] = set()
        self.cancels: list[tuple[str, str]] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        self.active.add(command.run_id)
        try:
            return await super().run(command)
        finally:
            self.active.discard(command.run_id)

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        if run_id not in self.active:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot steer an inactive child run",
                details={"run_id": run_id},
            )
        await super().steer(run_id, message)

    async def cancel(self, run_id: str, reason: str) -> None:
        if run_id not in self.active:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot cancel an inactive child run",
                details={"run_id": run_id},
            )
        self.cancels.append((run_id, reason))


def per_run_kernel_provider(gate: asyncio.Event):
    """每个 ``run_id`` 一个内核实例（生产装配的形状）。"""
    kernels: dict[str, PerInstanceSteeringKernel] = {}

    def provider(req) -> PerInstanceSteeringKernel:
        kernel = kernels.get(req.run_id)
        if kernel is None:
            kernel = PerInstanceSteeringKernel(gate)
            kernels[req.run_id] = kernel
        return kernel

    return provider, kernels


def request(
    tool_name: str,
    arguments: dict,
    *,
    run_id: str = "run_parent_1",
    call_id: str = "call_parent_1",
    conversation_id: str = "conversation_1",
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        conversation_id=conversation_id,
        run_id=run_id,
        model_turn_id="turn_1",
        correlation_id="corr_1",
        cancellation=CancellationToken(),
        created_at="2026-09-03T00:00:00Z",
    )


def reserve_child(
    store,
    child_run_id: str,
    *,
    conversation_id: str = "conversation_1",
    task: str = "任务",
    workspace_mode: str = "read_only_shared",
):
    return store.reserve_child_run(
        child_run_id=child_run_id,
        parent_run_id="run_parent_1",
        parent_tool_call_id=f"call_{child_run_id}",
        parent_conversation_id=conversation_id,
        depth=1,
        capability_profile="subagent_readonly",
        workspace_mode=workspace_mode,
        timeout_seconds=120.0,
        task=task,
    )


class _StubOwnerCoordinator:
    """只记录"收到哪些取消"的假拥有者协调器（真内核在这里跑不起来）。"""

    def __init__(self, conversation_id: str, *, error: Exception | None = None) -> None:
        self._conversation_id = conversation_id
        self._error = error
        self.cancels: list[tuple[str, str]] = []

    def covers_conversation(self, conversation_id) -> bool:
        if not conversation_id or not self._conversation_id:
            return True
        return conversation_id == self._conversation_id

    async def cancel(self, child_run_id: str, reason: str) -> None:
        if self._error is not None:
            raise self._error
        self.cancels.append((child_run_id, reason))


class DelegationToolHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = EchoAgentKernel()
        self.handler = CoordinatorDelegationHandler(
            kernel_provider=lambda req: self.kernel,
            capability_provider=lambda req: PARENT_CAPABILITIES,
        )
        self.spawn_tool = SpawnAgentTool(handler=self.handler)
        self.query_tool = QueryAgentTool(handler=self.handler)
        self.cancel_tool = CancelAgentTool(handler=self.handler)

    def test_spawn_query_round_trip(self) -> None:
        spawn = asyncio.run(
            self.spawn_tool.execute(
                request("spawn_agent", {"task": "调查模块 A"})
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, spawn.status)
        child_run_id = spawn.structured_content["childRunId"]
        # 异步契约：spawn 立刻返回"运行中"句柄，结果要等子运行跑完再查。
        self.assertEqual("running", spawn.structured_content["status"])
        lineage = self.kernel.commands[0].metadata["delegation"]
        self.assertEqual("run_parent_1", lineage["parent_run_id"])
        self.assertEqual("call_parent_1", lineage["parent_tool_call_id"])

        asyncio.run(self.handler.drain())
        query = asyncio.run(
            self.query_tool.execute(
                request("query_agent", {"childRunId": child_run_id})
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, query.status)
        self.assertEqual("completed", query.structured_content["status"])
        self.assertIn("调查完成", query.structured_content["summary"])

    def test_query_reports_a_still_running_child(self) -> None:
        """子运行还挂在闸门里时，query_agent 必须如实答"运行中"。

        handler 把协调器的 ``delegation_child_running`` 映射成 COMPLETED +
        ``status=running``——这是异步契约下模型最常拿到的回答。没有 store 时协调器
        对"运行中"只能答"查无此子运行"，所以这条路径必须带 store 构造（生产接线
        就是这样）。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                kernel = GatedEchoAgentKernel(gate)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: kernel,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                spawn_tool = SpawnAgentTool(handler=handler)
                query_tool = QueryAgentTool(handler=handler)

                spawn = await spawn_tool.execute(
                    request("spawn_agent", {"task": "调查模块 A"})
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernel.started.wait()

                running = await query_tool.execute(
                    request("query_agent", {"childRunId": child_run_id})
                )
                self.assertEqual(ToolOutcomeStatus.COMPLETED, running.status)
                self.assertEqual("running", running.structured_content["status"])

                gate.set()
                await handler.drain()

                finished = await query_tool.execute(
                    request("query_agent", {"childRunId": child_run_id})
                )
                self.assertEqual(ToolOutcomeStatus.COMPLETED, finished.status)
                self.assertEqual("completed", finished.structured_content["status"])
                self.assertIn("调查完成", finished.structured_content["summary"])

        asyncio.run(scenario())

    def test_query_reports_a_queued_child_with_derived_status(self) -> None:
        """闸门外排队（started_at IS NULL）的子运行：query_agent 必须答 queued。

        规格 M2-3：状态走库里的时间戳派生，排队中不得谎报 running（模型会拿
        running 去 steer 一个内核里根本不存在的运行）；还没开始就没有"已用时"
        可言，必须是 None。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_queued", task="排队任务")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            answer = asyncio.run(
                handler.query_child(
                    request("query_agent", {"childRunId": "child_queued"})
                )
            )

            self.assertEqual(ToolOutcomeStatus.COMPLETED, answer.status)
            payload = answer.structured_content
            self.assertEqual("child_queued", payload["childRunId"])
            self.assertEqual("queued", payload["status"])
            self.assertIsNone(payload["startedAt"])
            self.assertIsNone(payload["elapsedSeconds"])
            self.assertIsNotNone(payload["updatedAt"])
            self.assertEqual("子代理仍在排队（尚未开始）：child_queued", answer.content)

    def test_query_reports_a_running_child_with_elapsed_seconds(self) -> None:
        """已开始跑的子运行：running + startedAt/elapsedSeconds/updatedAt（M2-3）。

        已用时要**派生**出来（now - started_at），不是写死的 0。时钟被钉在
        "42 秒前开始"，所以无论哪一天跑这条用例，答案都该是 42 秒上下；查询前
        又写了一次 injected_at，用来证明 updatedAt 是"最近一次活动"而不是
        startedAt 的副本。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            real_now = datetime.now(timezone.utc)
            started_at = (
                (real_now - timedelta(seconds=42))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            ticks = [started_at]
            store = SqliteDelegationChildRunRepository(database, clock=lambda: ticks[0])
            reserve_child(store, "child_running", task="跑着的任务")
            store.mark_child_started("child_running")
            ticks[0] = (
                real_now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
            store.mark_child_injected("child_running")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            answer = asyncio.run(
                handler.query_child(
                    request("query_agent", {"childRunId": "child_running"})
                )
            )

            self.assertEqual(ToolOutcomeStatus.COMPLETED, answer.status)
            payload = answer.structured_content
            self.assertEqual("child_running", payload["childRunId"])
            self.assertEqual("running", payload["status"])
            self.assertEqual(started_at, payload["startedAt"])
            self.assertGreaterEqual(payload["elapsedSeconds"], 42)
            self.assertLess(payload["elapsedSeconds"], 300)
            self.assertEqual(ticks[0], payload["updatedAt"])
            self.assertIn(f"已用时 {payload['elapsedSeconds']} 秒", answer.content)
            self.assertIn("子代理仍在运行中", answer.content)

    def test_query_terminal_child_keeps_fields_and_adds_frozen_progress(self) -> None:
        """终态回答向后兼容：既有字段一个不少，另加三个进展字段（M2-3）。

        已用时按 ``terminal_at`` 收口：查询时刻被推到第二天，已用时仍必须是
        05 秒 → 30 秒这 25 秒，不能随当前时间增长。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            ticks = ["2026-09-03T00:00:00.000Z"]
            store = SqliteDelegationChildRunRepository(database, clock=lambda: ticks[0])
            reserve_child(store, "child_done", task="完成的任务")
            ticks[0] = "2026-09-03T00:00:05.000Z"
            store.mark_child_started("child_done")
            ticks[0] = "2026-09-03T00:00:30.000Z"
            store.mark_child_terminal(
                "child_done", state=ChildRunState.COMPLETED, summary="调查完成"
            )
            ticks[0] = "2026-09-04T00:00:00.000Z"
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            answer = asyncio.run(
                handler.query_child(
                    request("query_agent", {"childRunId": "child_done"})
                )
            )

            self.assertEqual(ToolOutcomeStatus.COMPLETED, answer.status)
            payload = answer.structured_content
            # 既有契约（工具面与模型都按这些键取值）：一个都不能少或改名。
            self.assertEqual("child_done", payload["childRunId"])
            self.assertEqual("completed", payload["status"])
            self.assertEqual("调查完成", payload["summary"])
            self.assertEqual(0, len(payload["diagnostics"]))
            # 新增的进展字段：终态已用时冻结在 terminal_at。
            self.assertEqual("2026-09-03T00:00:05.000Z", payload["startedAt"])
            self.assertEqual("2026-09-03T00:00:30.000Z", payload["terminalAt"])
            self.assertEqual(25, payload["elapsedSeconds"])
            self.assertEqual("调查完成", answer.content)

    def test_duplicate_spawn_for_same_parent_tool_call_refused(self) -> None:
        # Handler idempotency: the same parent tool call id must not spawn
        # twice (06 §8.6), so a retried call maps to a structured failure.
        first = asyncio.run(
            self.spawn_tool.execute(request("spawn_agent", {"task": "任务"}))
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, first.status)
        # Different parent call id -> allowed.
        second_req = request("spawn_agent", {"task": "任务二"})
        second_req = ToolExecutionRequest(
            call_id="call_parent_2",
            tool_name="spawn_agent",
            arguments={"task": "任务二"},
            conversation_id=second_req.conversation_id,
            run_id=second_req.run_id,
            model_turn_id=second_req.model_turn_id,
            correlation_id=second_req.correlation_id,
            cancellation=second_req.cancellation,
            created_at=second_req.created_at,
        )
        second = asyncio.run(self.spawn_tool.execute(second_req))
        self.assertEqual(ToolOutcomeStatus.COMPLETED, second.status)
        # 子运行在后台：等它们跑完再数内核收到了几条 dispatch。
        asyncio.run(self.handler.drain())
        self.assertEqual(2, len(self.kernel.commands))

    def test_duplicate_call_id_refused_by_coordinator(self) -> None:
        first = asyncio.run(
            self.spawn_tool.execute(request("spawn_agent", {"task": "任务"}))
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, first.status)
        retry = asyncio.run(
            self.spawn_tool.execute(request("spawn_agent", {"task": "任务"}))
        )
        self.assertEqual(ToolOutcomeStatus.FAILED, retry.status)
        self.assertEqual("delegation_duplicate_spawn", retry.diagnostic.code)
        asyncio.run(self.handler.drain())

    def test_query_unknown_child_fails_structured(self) -> None:
        query = asyncio.run(
            self.query_tool.execute(
                request("query_agent", {"childRunId": "child_missing"})
            )
        )
        self.assertEqual(ToolOutcomeStatus.FAILED, query.status)
        self.assertEqual("delegation_unknown_child", query.diagnostic.code)

    def test_cancel_terminal_child_is_benign_noop(self) -> None:
        spawn = asyncio.run(
            self.spawn_tool.execute(request("spawn_agent", {"task": "任务"}))
        )
        child_run_id = spawn.structured_content["childRunId"]
        # 先让后台子运行跑到终态，再取消 -> 终态取消是无害 no-op。
        asyncio.run(self.handler.drain())
        cancel = asyncio.run(
            self.cancel_tool.execute(
                request(
                    "cancel_agent",
                    {"childRunId": child_run_id, "reason": "不需要了"},
                )
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, cancel.status)
        self.assertEqual("already_terminal", cancel.structured_content["status"])

    def test_steer_running_child_reaches_the_kernel(self) -> None:
        """运行中（已进内核、started_at 非空）的子运行：消息必须真的送到内核。"""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                kernel = SteeringEchoAgentKernel(gate)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: kernel,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                spawn = await handler.spawn_child(
                    request("spawn_agent", {"task": "调查模块 A"})
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernel.started.wait()

                steered = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                    )
                )
                self.assertEqual(ToolOutcomeStatus.COMPLETED, steered.status)
                self.assertEqual("steering", steered.structured_content["status"])
                self.assertEqual(child_run_id, steered.structured_content["childRunId"])
                self.assertEqual(1, len(kernel.steers))
                steered_run_id, message = kernel.steers[0]
                self.assertEqual(child_run_id, steered_run_id)
                self.assertEqual("换个方向", message.content)
                self.assertIs(AgentMessageRole.USER, message.role)

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_steer_terminal_child_is_benign(self) -> None:
        spawn = asyncio.run(
            self.spawn_tool.execute(request("spawn_agent", {"task": "任务"}))
        )
        child_run_id = spawn.structured_content["childRunId"]
        # 先让后台子运行跑到终态，再纠偏 -> 终态纠偏是无害 no-op。
        asyncio.run(self.handler.drain())
        steered = asyncio.run(
            self.handler.steer_child(
                request(
                    "steer_agent",
                    {"childRunId": child_run_id, "message": "换个方向"},
                )
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, steered.status)
        self.assertEqual("already_terminal", steered.structured_content["status"])

    def test_steer_queued_child_is_refused(self) -> None:
        """闸门外排队（started_at IS NULL）的子运行内核里没有活动运行：必须如实拒绝。

        默认并发闸门是 4：塞满 4 个在跑的子运行后，第 5 个只能排队。谎报"已纠偏"
        会让模型以为话带到了，而那句话会凭空消失。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                kernel = SteeringEchoAgentKernel(gate, expected_started=4)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: kernel,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                for index in range(4):
                    await handler.spawn_child(
                        request(
                            "spawn_agent",
                            {"task": f"任务{index}"},
                            call_id=f"call_{index}",
                        )
                    )
                await kernel.all_started.wait()
                queued = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "排队任务"},
                        call_id="call_queued",
                    )
                )
                child_run_id = queued.structured_content["childRunId"]

                refused = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                    )
                )
                self.assertEqual(ToolOutcomeStatus.FAILED, refused.status)
                self.assertEqual(
                    "delegation_child_not_started", refused.diagnostic.code
                )
                self.assertEqual([], kernel.steers)

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_steer_unknown_child_fails_structured(self) -> None:
        steered = asyncio.run(
            self.handler.steer_child(
                request(
                    "steer_agent",
                    {"childRunId": "child_missing", "message": "换个方向"},
                )
            )
        )
        self.assertEqual(ToolOutcomeStatus.FAILED, steered.status)
        self.assertEqual("delegation_unknown_child", steered.diagnostic.code)

    def test_list_children_is_scoped_to_the_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_a")
            reserve_child(store, "child_b")
            reserve_child(store, "child_other", conversation_id="conversation_2")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(handler.list_children(request("list_agents", {})))

            self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
            payload = outcome.structured_content
            self.assertEqual(2, payload["total"])
            self.assertEqual(
                ["child_b", "child_a"],  # 最近创建的在前
                [item["childRunId"] for item in payload["children"]],
            )
            # 给模型的文本只能说"最近 N 个"：`total`/`len(children)` 是页大小
            # （最多 50），不是该会话的子运行总数。前端已按此修正，模型面同义。
            self.assertIn("本会话最近 2 个子运行", outcome.content)
            self.assertNotIn("共有", outcome.content)
            # 跨会话等于不存在：换个会话请求只看得到那个会话的子运行。
            other = asyncio.run(
                handler.list_children(
                    request(
                        "list_agents",
                        {},
                        conversation_id="conversation_2",
                        run_id="run_parent_2",
                        call_id="call_parent_2",
                    )
                )
            )
            self.assertEqual(1, other.structured_content["total"])
            self.assertEqual(
                ["child_other"],
                [item["childRunId"] for item in other.structured_content["children"]],
            )

    def test_list_children_reports_derived_status_and_bounded_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_queued", task="排队任务")
            reserve_child(
                store,
                "child_running",
                workspace_mode="isolated_snapshot",
                task="x" * 300,
            )
            store.mark_child_started("child_running")
            reserve_child(store, "child_done", task="完成的任务")
            store.mark_child_started("child_done")
            store.mark_child_terminal(
                "child_done", state=ChildRunState.COMPLETED, summary="完成"
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(handler.list_children(request("list_agents", {})))

            by_id = {
                item["childRunId"]: item
                for item in outcome.structured_content["children"]
            }
            # 三态由库里的时间戳派生：started_at IS NULL = 还在闸门外排队。
            self.assertEqual("queued", by_id["child_queued"]["status"])
            self.assertIsNone(by_id["child_queued"]["startedAt"])
            self.assertIsNone(by_id["child_queued"]["elapsedSeconds"])
            self.assertEqual("running", by_id["child_running"]["status"])
            self.assertIsNotNone(by_id["child_running"]["startedAt"])
            self.assertGreaterEqual(by_id["child_running"]["elapsedSeconds"], 0)
            self.assertEqual("completed", by_id["child_done"]["status"])
            # 只有隔离写子运行带 isolatedWrite；任务摘要截断 200 字。
            self.assertTrue(by_id["child_running"]["isolatedWrite"])
            self.assertFalse(by_id["child_queued"]["isolatedWrite"])
            self.assertEqual(201, len(by_id["child_running"]["task"]))
            self.assertTrue(by_id["child_running"]["task"].endswith("…"))

    def test_list_children_caps_the_listing_at_fifty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            for index in range(51):
                reserve_child(store, f"child_{index:03d}")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(handler.list_children(request("list_agents", {})))

            self.assertEqual(50, outcome.structured_content["total"])
            self.assertEqual(50, len(outcome.structured_content["children"]))

    def test_steer_race_where_the_child_finished_is_benign(self) -> None:
        """内核回 invalid_agent_kernel_value 时复核库行：已终态 → 良性 already_terminal。"""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                store = SqliteDelegationChildRunRepository(database)
                gate = asyncio.Event()
                kernel = SteeringRaceKernel(gate, store, terminalize=True)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: kernel,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=store,
                )
                spawn = await handler.spawn_child(
                    request("spawn_agent", {"task": "任务"})
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernel.started.wait()

                steered = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                    )
                )
                self.assertEqual(ToolOutcomeStatus.COMPLETED, steered.status)
                self.assertEqual(
                    "already_terminal", steered.structured_content["status"]
                )

                # 库行已经终态：关停（取消挂在闸门里的后台任务）而不是放行，
                # 否则内核收尾会撞上"只有 running 的子运行可以收尾"。
                await handler.shutdown()

        asyncio.run(scenario())

    def test_steer_race_where_the_child_is_still_running_propagates(self) -> None:
        """复核后子运行仍在跑：内核的错误原样上抛，不翻译成良性回答。"""

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                store = SqliteDelegationChildRunRepository(database)
                gate = asyncio.Event()
                kernel = SteeringRaceKernel(gate, store, terminalize=False)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: kernel,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=store,
                )
                spawn = await handler.spawn_child(
                    request("spawn_agent", {"task": "任务"})
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernel.started.wait()

                steered = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                    )
                )
                self.assertEqual(ToolOutcomeStatus.FAILED, steered.status)
                self.assertEqual(
                    "invalid_agent_kernel_value", steered.diagnostic.code
                )

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_steer_reaches_a_child_spawned_in_an_earlier_turn(self) -> None:
        """跨轮次 steer：必须路由到**持有该子运行**的协调器，而不是本轮的新内核。

        一次用户消息就是一个运行，而子运行刻意比父运行活得久：第二轮请求的
        ``run_id`` 与 spawn 那一轮不同，所以本轮内核里根本没有这个活动运行。
        路由回上一轮的协调器（内核 A）之后消息才真的送到；否则内核 B 抛
        ``invalid_agent_kernel_value``，复核库行又发现它还在 running，工具只能
        以一个不透明的错误失败，子运行继续失控地跑。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                provider, kernels = per_run_kernel_provider(gate)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=provider,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                # 第一轮：run_1 的协调器启动子运行，活动运行挂在内核 A 里。
                spawn = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "调查模块 A"},
                        run_id="run_1",
                        call_id="call_1",
                    )
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernels["run_1"].started.wait()

                # 第二轮：同一会话、不同的运行（新内核实例 B）。
                steered = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                        run_id="run_2",
                        call_id="call_2",
                    )
                )

                self.assertEqual(ToolOutcomeStatus.COMPLETED, steered.status)
                self.assertEqual("steering", steered.structured_content["status"])
                self.assertEqual(
                    [(child_run_id, "换个方向")],
                    [(run_id, msg.content) for run_id, msg in kernels["run_1"].steers],
                )
                # 路由到持有者，本轮不该再建一个够不到子运行的内核。
                self.assertEqual(["run_1"], sorted(kernels))

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_cancel_reaches_a_child_spawned_in_an_earlier_turn(self) -> None:
        """跨轮次 cancel：同一个根因，也必须路由到持有后台任务的协调器。

        排队中的子运行只能靠持有它的协调器的 ``_child_tasks`` 落地取消；把取消
        发给本轮的新内核会得 ``invalid_agent_kernel_value``，工具失败而子运行
        照跑。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                provider, kernels = per_run_kernel_provider(gate)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=provider,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                spawn = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "调查模块 A"},
                        run_id="run_1",
                        call_id="call_1",
                    )
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernels["run_1"].started.wait()

                cancelled = await handler.cancel_child(
                    request(
                        "cancel_agent",
                        {"childRunId": child_run_id, "reason": "不需要了"},
                        run_id="run_2",
                        call_id="call_2",
                    )
                )

                self.assertEqual(ToolOutcomeStatus.COMPLETED, cancelled.status)
                self.assertEqual("cancelling", cancelled.structured_content["status"])
                self.assertEqual(
                    [(child_run_id, "不需要了")], kernels["run_1"].cancels
                )
                self.assertEqual(["run_1"], sorted(kernels))

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_cross_conversation_steer_is_still_refused(self) -> None:
        """路由不得绕开会话边界：持有者在别的会话时仍然等于"查无此子运行"。

        按 ``parent_run_id`` 找到的协调器如果与本次请求不属于同一会话，就不能把
        控制权交出去（否则会话边界被路由悄悄拆掉）；回落本轮的协调器，保持
        query/cancel 一致的 ``delegation_unknown_child``。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                provider, kernels = per_run_kernel_provider(gate)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=provider,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=SqliteDelegationChildRunRepository(database),
                )
                spawn = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "调查模块 A"},
                        run_id="run_1",
                        call_id="call_1",
                    )
                )
                child_run_id = spawn.structured_content["childRunId"]
                await kernels["run_1"].started.wait()

                steered = await handler.steer_child(
                    request(
                        "steer_agent",
                        {"childRunId": child_run_id, "message": "换个方向"},
                        run_id="run_2",
                        call_id="call_2",
                        conversation_id="conversation_2",
                    )
                )

                self.assertEqual(ToolOutcomeStatus.FAILED, steered.status)
                self.assertEqual("delegation_unknown_child", steered.diagnostic.code)
                self.assertEqual([], kernels["run_1"].steers)

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_cancel_cross_conversation_child_is_still_refused(self) -> None:
        """取消路径与 steer 同一条边界：拥有者代答别的会话时等于"查无此子运行"。

        生产形状：子运行在第一轮的 ``run_1``/``conversation_2`` 下派出去，取消请求
        来自 ``run_2``/``conversation_1``。工具面现在把"库里有这一行"的情况整条交给
        ``cancel_child_for_conversation``（HTTP 取消端点同一个方法），所以边界不会
        再被路由或回落拆掉；拥有者就在缓存里也不许它代劳。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                store = SqliteDelegationChildRunRepository(database)
                reserve_child(store, "child_other", conversation_id="conversation_2")
                owner = _StubOwnerCoordinator("conversation_2")
                handler = CoordinatorDelegationHandler(
                    kernel_provider=lambda req: PerInstanceSteeringKernel(
                        asyncio.Event()
                    ),
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=store,
                )
                handler._coordinators["run_1"] = owner

                cancelled = await handler.cancel_child(
                    request(
                        "cancel_agent",
                        {"childRunId": "child_other", "reason": "不需要了"},
                        run_id="run_2",
                        call_id="call_2",
                    )
                )

                self.assertEqual(ToolOutcomeStatus.FAILED, cancelled.status)
                self.assertEqual(
                    "delegation_unknown_child", cancelled.diagnostic.code
                )
                # 拥有者代答别的会话：连下发都不许发生。
                self.assertEqual([], owner.cancels)

        asyncio.run(scenario())

    def test_cancel_refuses_a_row_whose_conversation_id_is_empty(self) -> None:
        """空 ``parent_conversation_id`` 的行不再借"同一次运行"溜进取消。

        ``InProcessChildCoordinator._owns_row`` 在任一侧没有会话 id 时退回运行 id
        比较（空侧回落），而会话键的严格判定（HTTP 面用的那条）在同样情形下一律
        判"不属于本会话"。工具面现在与 HTTP 面共用严格判定：宁可查无此子运行，
        也不放一条没有会话归属的行进来——两边对同一条行的答案必须一致。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_unscoped", conversation_id="")
            owner = _StubOwnerCoordinator("conversation_1")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )
            # 拥有者就在缓存里、运行 id 也对得上：旧路径据此放行了这条行。
            handler._coordinators["run_parent_1"] = owner

            cancelled = asyncio.run(
                handler.cancel_child(
                    request("cancel_agent", {"childRunId": "child_unscoped"})
                )
            )

            self.assertEqual(ToolOutcomeStatus.FAILED, cancelled.status)
            self.assertEqual("delegation_unknown_child", cancelled.diagnostic.code)
            self.assertEqual([], owner.cancels)

    def test_cancel_running_row_without_a_reachable_owner_is_reported(self) -> None:
        """非终态却没有可达拥有者：如实回可重试的冲突，不谎报"取消中"。

        工具面在有库行时与会话键方法（HTTP 面同一份）共用同一套语义。旧路径会
        回落本轮的协调器、拿一个不认识这个子运行的内核去取消，最后以一个不透明
        的 ``invalid_agent_kernel_value`` 失败；现在统一成具名的
        ``delegation_child_owner_unavailable``，调用方据此知道"没下发成功、可重试"。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_orphan")
            handler = CoordinatorDelegationHandler(
                # 每个运行一个内核实例，且不认识这个从没跑过的子运行（生产形状）。
                kernel_provider=lambda req: PerInstanceSteeringKernel(asyncio.Event()),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            cancelled = asyncio.run(
                handler.cancel_child(
                    request("cancel_agent", {"childRunId": "child_orphan"})
                )
            )

            self.assertEqual(ToolOutcomeStatus.FAILED, cancelled.status)
            self.assertEqual(
                "delegation_child_owner_unavailable", cancelled.diagnostic.code
            )

    def test_list_children_without_children_is_an_empty_selection(self) -> None:
        """无子运行回空数组（不是错误）：模型据此知道"我还没派过活"。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=SqliteDelegationChildRunRepository(database),
            )

            outcome = asyncio.run(handler.list_children(request("list_agents", {})))

            self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
            # ToolOutcome 会把序列规范化成元组：这里只看语义（空数组 + total 0）。
            self.assertEqual((), tuple(outcome.structured_content["children"]))
            self.assertEqual(0, outcome.structured_content["total"])
            self.assertEqual("本会话没有子运行。", outcome.content)

    def test_list_children_freezes_elapsed_for_terminal_rows(self) -> None:
        """终态行的"已用时"必须在结束时刻收口，不能随当前时间继续增长。

        用固定时钟让 started_at == terminal_at：收口后已用时恰好 0；若拿 now 计时，
        这里会是"从固定时刻到今天"的一大坨秒数——对已经结束的子运行就是假信息。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            frozen = "2026-09-03T00:00:00.000Z"
            store = SqliteDelegationChildRunRepository(database, clock=lambda: frozen)
            reserve_child(store, "child_done")
            store.mark_child_started("child_done")
            store.mark_child_terminal(
                "child_done", state=ChildRunState.COMPLETED, summary="完成"
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(handler.list_children(request("list_agents", {})))

            item = outcome.structured_content["children"][0]
            self.assertEqual("completed", item["status"])
            self.assertEqual(frozen, item["startedAt"])
            self.assertEqual(0, item["elapsedSeconds"])

    def test_parent_without_read_capabilities_spawn_fails_closed(self) -> None:
        restricted = CoordinatorDelegationHandler(
            kernel_provider=lambda req: EchoAgentKernel(),
            capability_provider=lambda req: frozenset({"session.query"}),
        )
        tool = SpawnAgentTool(handler=restricted)
        outcome = asyncio.run(
            tool.execute(request("spawn_agent", {"task": "调查"}))
        )
        self.assertEqual(ToolOutcomeStatus.FAILED, outcome.status)
        self.assertEqual("delegation_capability_escalation", outcome.diagnostic.code)

    def test_parent_without_memory_tool_grant_can_spawn(self) -> None:
        # Regression (real-provider E2E, 06 §27): product memory is context
        # injected, not a tool capability, so a real parent's tool-union
        # grant never contains memory.read. The child request set must not
        # include it, or every real spawn would fail closed as an
        # escalation.
        parent = CoordinatorDelegationHandler(
            kernel_provider=lambda req: EchoAgentKernel(),
            capability_provider=lambda req: frozenset(
                {"workspace.read", "session.query"}
            ),
        )
        tool = SpawnAgentTool(handler=parent)
        outcome = asyncio.run(
            tool.execute(request("spawn_agent", {"task": "调查"}))
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)

    def test_idle_coordinators_are_evicted_at_the_tracked_limit(self) -> None:
        self.handler.MAX_TRACKED_PARENT_RUNS = 2
        for index in range(3):
            asyncio.run(
                self.spawn_tool.execute(
                    request(
                        "spawn_agent",
                        {"task": f"任务{index}"},
                        run_id=f"run_{index}",
                        call_id=f"call_{index}",
                    )
                )
            )
        asyncio.run(self.handler.drain())

        # 库是真相：最久且空闲的 run_0 被回收，缓存停在 2 个。
        self.assertEqual(("run_1", "run_2"), self.handler.tracked_parent_run_ids)

    def test_eviction_prefers_the_least_recently_used_over_insertion_order(
        self,
    ) -> None:
        """淘汰要按"最久未用"，不是"最早插入"（规格 M3 边界组）。

        插入序 run_1 < run_2，但对 run_1 又派过一次活（新 call_id）：run_1 是最近
        用过的那个。再派 run_3 时必须淘汰 run_2；按插入序的实现会淘汰 run_1。
        """
        self.handler.MAX_TRACKED_PARENT_RUNS = 2
        for index in (1, 2):
            asyncio.run(
                self.spawn_tool.execute(
                    request(
                        "spawn_agent",
                        {"task": f"任务{index}"},
                        run_id=f"run_{index}",
                        call_id=f"call_{index}",
                    )
                )
            )
        asyncio.run(self.handler.drain())
        self.assertEqual(("run_1", "run_2"), self.handler.tracked_parent_run_ids)

        # 对 run_1 再派一次（新 call_id）：命中缓存 → 它成了最近用过的。
        asyncio.run(
            self.spawn_tool.execute(
                request(
                    "spawn_agent",
                    {"task": "任务1续"},
                    run_id="run_1",
                    call_id="call_1b",
                )
            )
        )
        asyncio.run(self.handler.drain())
        asyncio.run(
            self.spawn_tool.execute(
                request(
                    "spawn_agent",
                    {"task": "任务3"},
                    run_id="run_3",
                    call_id="call_3",
                )
            )
        )
        asyncio.run(self.handler.drain())

        self.assertEqual(("run_1", "run_3"), self.handler.tracked_parent_run_ids)
        self.assertNotIn("run_2", self.handler.tracked_parent_run_ids)

    def test_all_busy_coordinators_may_exceed_the_bound_and_are_logged(self) -> None:
        """所有条目都忙时**允许暂时超界**，但必须 debug 记一笔（有意行为）。

        界是"软"的：唯一的硬约束是不得回收还有非终态子运行的协调器。等它们跑完
        之后界会自动恢复——MAX=1 时那条空下来的老条目会被腾掉。
        """

        async def scenario() -> None:
            gate = asyncio.Event()
            kernel = GatedEchoAgentKernel(gate)
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: kernel,
                capability_provider=lambda req: PARENT_CAPABILITIES,
            )
            handler.MAX_TRACKED_PARENT_RUNS = 1
            tool = SpawnAgentTool(handler=handler)

            await tool.execute(
                request("spawn_agent", {"task": "任务一"}, run_id="run_1", call_id="call_1")
            )
            await kernel.started.wait()
            with self.assertLogs(
                "endless_task.delegation.runtime_handler", level="DEBUG"
            ) as logs:
                await tool.execute(
                    request(
                        "spawn_agent", {"task": "任务二"}, run_id="run_2", call_id="call_2"
                    )
                )
            # run_1 忙：宁可超界也不回收它；超界这件事有记录。
            self.assertEqual(2, len(handler.tracked_parent_run_ids))
            self.assertTrue(
                any("all_busy" in line for line in logs.output),
                logs.output,
            )

            gate.set()
            await handler.drain()
            await tool.execute(
                request("spawn_agent", {"task": "任务三"}, run_id="run_3", call_id="call_3")
            )
            # 两条老条目都已空闲：界恢复，只留最新的 run_3。
            self.assertEqual(("run_3",), handler.tracked_parent_run_ids)
            await handler.drain()

        asyncio.run(scenario())

    def test_eviction_keeps_coordinators_with_running_children(self) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()
            kernel = GatedEchoAgentKernel(gate)
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: kernel,
                capability_provider=lambda req: PARENT_CAPABILITIES,
            )
            handler.MAX_TRACKED_PARENT_RUNS = 1
            tool = SpawnAgentTool(handler=handler)

            await tool.execute(
                request("spawn_agent", {"task": "任务一"}, run_id="run_1", call_id="call_1")
            )
            await kernel.started.wait()
            await tool.execute(
                request("spawn_agent", {"task": "任务二"}, run_id="run_2", call_id="call_2")
            )

            # run_1 还有在跑的子运行：回收它会把内核一起丢掉，必须保留。
            self.assertIn("run_1", handler.tracked_parent_run_ids)
            gate.set()
            await handler.drain()

        asyncio.run(scenario())

    def test_shutdown_reaches_the_per_parent_coordinators(self) -> None:
        """生产关停路径必须真的走到各协调器的收尾。

        之前 handler 只关主管、不碰协调器，于是"任务在第一个事件循环步之前被取消"
        留下的 running 行要等下一次进程启动对账才收尾——这条钉子钉住可达性。
        """
        finalized: list[str] = []

        class _StubCoordinator:
            def __init__(self, parent_run_id: str) -> None:
                self._parent_run_id = parent_run_id

            def finalize_cancelled_children(self) -> None:
                finalized.append(self._parent_run_id)

        self.handler._coordinators["run_stub"] = _StubCoordinator("run_stub")

        asyncio.run(self.handler.shutdown())

        self.assertEqual(["run_stub"], finalized)

    def test_child_workspace_id_for_skips_coordinators_of_other_conversations(
        self,
    ) -> None:
        """内存循环也必须按会话设界：别的会话的协调器连问都不该被问到。

        `child_workspace_id` 一旦被调用就会返回那个会话的 scratch 工作区 id；
        所以这里同时钉住"没被调用"与"本会话仍然够得到"。
        """
        asked: list[str] = []

        class _ScopedStubCoordinator:
            def __init__(self, conversation_id: str) -> None:
                self._conversation_id = conversation_id

            def covers_conversation(self, conversation_id) -> bool:
                if not conversation_id:
                    return True
                return conversation_id == self._conversation_id

            def child_workspace_id(self, child_run_id: str) -> str:
                asked.append(child_run_id)
                return "ws_conv_a"

        self.handler._coordinators["run_conv_a"] = _ScopedStubCoordinator("conv_a")

        with self.assertRaises(AgentPlatformError) as foreign:
            self.handler.child_workspace_id_for("child_1", conversation_id="conv_b")
        self.assertEqual("delegation_unknown_child", foreign.exception.code)
        self.assertEqual([], asked)

        self.assertEqual(
            "ws_conv_a",
            self.handler.child_workspace_id_for("child_1", conversation_id="conv_a"),
        )
        self.assertEqual(["child_1"], asked)

    def test_child_workspace_id_falls_back_to_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            store.reserve_child_run(
                child_run_id="child_isolated",
                parent_run_id="run_parent_1",
                parent_tool_call_id="call_isolated",
                parent_conversation_id="conversation_1",
                depth=1,
                capability_profile="subagent_readonly",
                workspace_mode="isolated_snapshot",
                timeout_seconds=120.0,
                task="隔离写任务",
                child_workspace_id="ws_child_1",
            )
            store.reserve_child_run(
                child_run_id="child_shared",
                parent_run_id="run_parent_1",
                parent_tool_call_id="call_shared",
                parent_conversation_id="conversation_1",
                depth=1,
                capability_profile="subagent_readonly",
                workspace_mode="read_only_shared",
                timeout_seconds=120.0,
                task="只读任务",
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            # 内存里没有任何协调器记录这些行：回落必须以库为准，且契约与
            # InProcessChildCoordinator.child_workspace_id 一致——未完成不给，
            # 没有 scratch 也不给。
            with self.assertRaises(AgentPlatformError) as running:
                handler.child_workspace_id_for("child_isolated")
            self.assertEqual(
                "delegation_child_not_completed", running.exception.code
            )

            store.mark_child_terminal(
                "child_isolated", state=ChildRunState.COMPLETED, summary="完成"
            )
            self.assertEqual(
                "ws_child_1", handler.child_workspace_id_for("child_isolated")
            )

            store.mark_child_terminal(
                "child_shared", state=ChildRunState.COMPLETED, summary="完成"
            )
            with self.assertRaises(AgentPlatformError) as shared:
                handler.child_workspace_id_for("child_shared")
            self.assertEqual("delegation_child_not_isolated", shared.exception.code)

            with self.assertRaises(AgentPlatformError) as missing:
                handler.child_workspace_id_for("child_missing")
            self.assertEqual("delegation_unknown_child", missing.exception.code)

    def test_child_workspace_id_store_fallback_is_scoped_to_the_conversation(
        self,
    ) -> None:
        """库在会话之间共享：给了会话就必须只认同一会话的行。

        与协调器 query/cancel 的边界一致：跨会话的行等于不存在，而不是"不是隔离写"
        或"还没完成"——后两种错会让调用方以为换个时机就能 apply。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            store.reserve_child_run(
                child_run_id="child_isolated",
                parent_run_id="run_parent_1",
                parent_tool_call_id="call_isolated",
                parent_conversation_id="conversation_1",
                depth=1,
                capability_profile="subagent_readonly",
                workspace_mode="isolated_snapshot",
                timeout_seconds=120.0,
                task="隔离写任务",
                child_workspace_id="ws_child_1",
            )
            store.mark_child_terminal(
                "child_isolated", state=ChildRunState.COMPLETED, summary="完成"
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            self.assertEqual(
                "ws_child_1",
                handler.child_workspace_id_for(
                    "child_isolated", conversation_id="conversation_1"
                ),
            )
            with self.assertRaises(AgentPlatformError) as other:
                handler.child_workspace_id_for(
                    "child_isolated", conversation_id="conversation_2"
                )
            self.assertEqual("delegation_unknown_child", other.exception.code)

    def test_cancel_children_of_conversation_only_touches_that_conversation(
        self,
    ) -> None:
        """父会话被删时只终止**这个会话**的子运行，并按子运行自己的
        ``parent_run_id`` 路由到持有它的协调器。

        跨会话的行必须原封不动（库在会话之间共享，按会话 id 枚举够不到别的会话）；
        没有可达拥有者的行只记日志跳过（协调器已回收/换了进程），交给启动对账，
        绝不凭库行硬改状态。
        """

        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Database(Path(directory) / "handler.db")
                database.initialize()
                gate = asyncio.Event()
                provider, kernels = per_run_kernel_provider(gate)
                store = SqliteDelegationChildRunRepository(database)
                handler = CoordinatorDelegationHandler(
                    kernel_provider=provider,
                    capability_provider=lambda req: PARENT_CAPABILITIES,
                    child_run_store=store,
                )
                # 会话 A：一个真在跑的子运行（内核与后台任务都在 run_a 手里）。
                spawn_a = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "任务 A"},
                        run_id="run_a",
                        call_id="call_a",
                        conversation_id="conv_a",
                    )
                )
                child_a = spawn_a.structured_content["childRunId"]
                await kernels["run_a"].started.wait()
                # 会话 B：同样在跑，但它不归这次删除管。
                spawn_b = await handler.spawn_child(
                    request(
                        "spawn_agent",
                        {"task": "任务 B"},
                        run_id="run_b",
                        call_id="call_b",
                        conversation_id="conv_b",
                    )
                )
                child_b = spawn_b.structured_content["childRunId"]
                await kernels["run_b"].started.wait()
                # 会话 A 的另一行：拥有者已不在缓存里（进程回收过）。
                store.reserve_child_run(
                    child_run_id="child_orphan",
                    parent_run_id="run_evicted",
                    parent_tool_call_id="call_orphan",
                    parent_conversation_id="conv_a",
                    depth=1,
                    capability_profile="subagent_readonly",
                    workspace_mode="read_only_shared",
                    timeout_seconds=120.0,
                    task="孤儿任务",
                )

                with self.assertLogs(
                    "endless_task.delegation.runtime_handler", level="INFO"
                ) as captured:
                    count = await handler.cancel_children_of_conversation(
                        "conv_a", "父会话已删除"
                    )

                self.assertEqual(1, count)
                self.assertEqual(
                    [(child_a, "父会话已删除")], kernels["run_a"].cancels
                )
                self.assertEqual([], kernels["run_b"].cancels)
                (row_b,) = store.list_child_runs_for_conversation("conv_b")
                self.assertFalse(row_b.terminal)
                # 没有可达拥有者：只记日志、不写终态（真跑着的任务会自己写）。
                skipped = [
                    record
                    for record in captured.records
                    if getattr(record, "child_run_id", None) == "child_orphan"
                ]
                self.assertEqual(1, len(skipped))
                rows_a = {
                    row.child_run_id: row
                    for row in store.list_child_runs_for_conversation("conv_a")
                }
                self.assertFalse(rows_a["child_orphan"].terminal)
                self.assertFalse(rows_a[child_a].terminal)

                gate.set()
                await handler.drain()

        asyncio.run(scenario())

    def test_cancel_children_of_conversation_skips_terminal_children(self) -> None:
        """已终态的行连取消都不下发：没有任何活动运行可取消。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_done", conversation_id="conversation_1")
            store.mark_child_terminal(
                "child_done", state=ChildRunState.COMPLETED, summary="完成"
            )
            owner = _StubOwnerCoordinator("conversation_1")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )
            handler._coordinators["run_parent_1"] = owner

            count = asyncio.run(
                handler.cancel_children_of_conversation(
                    "conversation_1", "父会话已删除"
                )
            )

            self.assertEqual(0, count)
            self.assertEqual([], owner.cancels)

    def test_cancel_children_of_conversation_without_a_store_is_a_noop(self) -> None:
        """没注入 store、或该会话一条子运行都没有：都无事可做，返回 0。

        没 store 的装配（只落内存的测试路径）不该在父会话删除时炸掉。
        """
        without_store = CoordinatorDelegationHandler(
            kernel_provider=lambda req: EchoAgentKernel(),
            capability_provider=lambda req: PARENT_CAPABILITIES,
        )
        self.assertEqual(
            0,
            asyncio.run(
                without_store.cancel_children_of_conversation(
                    "conversation_1", "父会话已删除"
                )
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            empty = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=SqliteDelegationChildRunRepository(database),
            )
            self.assertEqual(
                0,
                asyncio.run(
                    empty.cancel_children_of_conversation(
                        "conversation_1", "父会话已删除"
                    )
                ),
            )

    def test_cancel_children_of_conversation_never_raises(self) -> None:
        """内核取消失败只能吞掉并记日志：父会话删除不能被回流侧的问题挡住。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_live", conversation_id="conversation_1")
            owner = _StubOwnerCoordinator(
                "conversation_1",
                error=AgentPlatformError(
                    "invalid_agent_kernel_value", "Cannot cancel an inactive child run"
                ),
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )
            handler._coordinators["run_parent_1"] = owner

            with self.assertLogs(
                "endless_task.delegation.runtime_handler", level="ERROR"
            ) as captured:
                count = asyncio.run(
                    handler.cancel_children_of_conversation(
                        "conversation_1", "父会话已删除"
                    )
                )

            self.assertEqual(0, count)
            self.assertEqual(1, len(captured.records))
            self.assertEqual("ERROR", captured.records[0].levelname)

    def test_cancel_children_of_conversation_keeps_going_after_one_failure(
        self,
    ) -> None:
        """一条下发失败不能把同会话其余子运行落下：取消失败最常见的原因是"刚好
        跑完"的良性竞态，而删除父会话是这些子运行的**最后一次**收束机会。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            reserve_child(store, "child_a", conversation_id="conversation_1")
            store.reserve_child_run(
                child_run_id="child_b",
                parent_run_id="run_parent_2",
                parent_tool_call_id="call_child_b",
                parent_conversation_id="conversation_1",
                depth=1,
                capability_profile="subagent_readonly",
                workspace_mode="read_only_shared",
                timeout_seconds=120.0,
                task="任务 B",
            )
            failing = _StubOwnerCoordinator(
                "conversation_1",
                error=AgentPlatformError(
                    "delegation_child_not_active", "子运行刚好跑完了"
                ),
            )
            healthy = _StubOwnerCoordinator("conversation_1")
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )
            handler._coordinators["run_parent_1"] = failing
            handler._coordinators["run_parent_2"] = healthy

            with self.assertLogs(
                "endless_task.delegation.runtime_handler", level="ERROR"
            ):
                count = asyncio.run(
                    handler.cancel_children_of_conversation(
                        "conversation_1", "父会话已删除"
                    )
                )

            self.assertEqual(1, count)
            self.assertEqual([("child_b", "父会话已删除")], healthy.cancels)


class CoordinatorDelegationHandlerDepthTest(unittest.TestCase):
    """委派深度闸必须跨协调器实例生效。

    协调器按 ``request.run_id`` 新建，而"发起本次 spawn 的运行自己是不是一条子
    运行"只有 ``delegation_child_runs`` 里那一行说了算（父协调器的 ``_reserve``
    在子运行启动前写下）。这两个用例钉住 handler → 协调器的深度接线：子运行再
    派孙必须被拒（闸门原有语义），顶层运行必须仍能派（查不到行 = 深度 0）。
    """

    def test_spawn_from_a_depth_one_child_run_is_refused(self) -> None:
        """depth=1 的子运行不能再派孙：拒绝、报协调器的错误码、零副作用。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            # 正在发起 spawn 的这个运行自己是一条 depth=1 的子运行。
            store.reserve_child_run(
                child_run_id="run_child_depth1",
                parent_run_id="run_root",
                parent_tool_call_id="call_root",
                parent_conversation_id="conversation_1",
                depth=1,
                capability_profile="subagent_readonly",
                workspace_mode="read_only_shared",
                timeout_seconds=120.0,
                task="父任务",
            )
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(
                SpawnAgentTool(handler=handler).execute(
                    request("spawn_agent", {"task": "调查"}, run_id="run_child_depth1")
                )
            )

            self.assertEqual(ToolOutcomeStatus.FAILED, outcome.status)
            self.assertIsNotNone(outcome.diagnostic)
            self.assertEqual("delegation_max_depth_exceeded", outcome.diagnostic.code)
            # 闸门在 prepare_spawn（纯校验、零副作用）：被拒的运行不得落任何行。
            self.assertEqual((), store.list_child_runs("run_child_depth1"))

    def test_spawn_from_a_top_level_run_still_works(self) -> None:
        """库里没有该运行的行 = 顶层运行 = 深度 0：根运行必须仍能派子运行。

        守卫性质：这条用例在修复前后**都**通过（旧代码恒深度 0）；它钉的是
        fail-open 的方向——"查不到"必须当 0，而不是拒绝。
        """
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "handler.db")
            database.initialize()
            store = SqliteDelegationChildRunRepository(database)
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda req: EchoAgentKernel(),
                capability_provider=lambda req: PARENT_CAPABILITIES,
                child_run_store=store,
            )

            outcome = asyncio.run(
                SpawnAgentTool(handler=handler).execute(
                    request("spawn_agent", {"task": "调查"}, run_id="run_parent_1")
                )
            )

            self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)


class ChildDefaultTimeoutTest(unittest.TestCase):
    """切片 10 T1 Step 3：模型不写 `timeoutSeconds` 时，子运行的墙从哪来。

    为什么在 handler 层测：`spawn_child` 里那句
    ``if timeout is None: timeout = self._default_timeout_seconds`` 才是**唯一**
    决定"默认墙"的地方（`delegation/agent_tools.py` 里过去那个同名的
    `_DEFAULT_TIMEOUT_SECONDS` 常量**全仓无引用**，改它毫无效果）。装配根
    （`wiring.py`）现在把设置喂给构造参数，这里钉住"默认参数 → 子运行 deadline"
    这一段，以及"模型显式声明仍然优先"。
    """

    def _spawn_command(self, **handler_kwargs) -> RunCommand:
        kernel = EchoAgentKernel()
        handler = CoordinatorDelegationHandler(
            kernel_provider=lambda req: kernel,
            capability_provider=lambda req: PARENT_CAPABILITIES,
            **handler_kwargs,
        )
        outcome = asyncio.run(
            handler.spawn_child(request("spawn_agent", {"task": "调查模块 A"}))
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        asyncio.run(handler.drain())
        self.assertEqual(1, len(kernel.commands))
        return kernel.commands[0]

    def test_default_without_injection_is_still_120(self) -> None:
        command = self._spawn_command()
        remaining = command.deadline_monotonic - time.monotonic()
        self.assertAlmostEqual(120.0, remaining, delta=2.0)

    def test_injected_default_becomes_the_child_wall(self) -> None:
        command = self._spawn_command(default_timeout_seconds=300.0)
        remaining = command.deadline_monotonic - time.monotonic()
        self.assertAlmostEqual(300.0, remaining, delta=2.0)

    def test_explicit_timeout_still_wins_over_the_default(self) -> None:
        kernel = EchoAgentKernel()
        handler = CoordinatorDelegationHandler(
            kernel_provider=lambda req: kernel,
            capability_provider=lambda req: PARENT_CAPABILITIES,
            default_timeout_seconds=300.0,
        )
        outcome = asyncio.run(
            handler.spawn_child(
                request(
                    "spawn_agent",
                    {"task": "调查模块 A", "timeoutSeconds": 60},
                )
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        asyncio.run(handler.drain())
        remaining = kernel.commands[0].deadline_monotonic - time.monotonic()
        self.assertAlmostEqual(60.0, remaining, delta=2.0)


if __name__ == "__main__":
    unittest.main()
