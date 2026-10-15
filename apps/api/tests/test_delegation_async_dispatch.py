"""异步派发：start_child 立刻返回句柄，子运行在后台跑完并落终态。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildStatus,
    InProcessChildCoordinator,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.delegation.child_run_store import ChildRunState
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime_ledger import TraceContext
from endless_task.storage import Database, SqliteDelegationChildRunRepository

_PARENT_CAPABILITIES = frozenset({"workspace.read", "session.query"})


class _GateKernel:
    """可控内核：run 进入后挂住，直到测试放行。"""

    def __init__(self, text: str = "后台结论") -> None:
        self.text = text
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.commands = []

    async def run(self, command):
        self.commands.append(command)
        self.started.set()
        await self.release.wait()
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT, content=self.text
            ),
        )

    async def cancel(self, run_id, reason):
        # 内核活动表里没有它（还卡在闸门外的子运行就是这种状态）。
        raise AgentPlatformError(
            "invalid_agent_kernel_value", "Cannot cancel an inactive child run"
        )


class _CancellableKernel(_GateKernel):
    """支持真取消的假内核：cancel 会把 run 抬出来并返回取消结果。"""

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []
        self.reasons: list[str] = []
        self._pending = asyncio.Event()

    async def run(self, command):
        self.commands.append(command)
        self.started.set()
        await self._pending.wait()
        return RunOutcome(
            run_id=command.run_id,
            status=(
                RunOutcomeStatus.CANCELLED
                if command.run_id in self.cancelled
                else RunOutcomeStatus.COMPLETED
            ),
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT, content="取消后的收尾"
            ),
        )

    async def cancel(self, run_id, reason):
        self.cancelled.append(run_id)
        self.reasons.append(reason)
        self._pending.set()


class _InactiveKernel(_GateKernel):
    """cancel 一律报"内核侧没有活动运行"，可选地先让子运行刚好跑完。"""

    def __init__(self, *, store=None, finish_child_run_id: str = "") -> None:
        super().__init__()
        self._store = store
        self._finish_child_run_id = finish_child_run_id
        self.reasons: list[str] = []

    async def cancel(self, run_id, reason):
        self.reasons.append(reason)
        if self._finish_child_run_id and self._store is not None:
            # 模拟"取消请求到达内核时子运行刚好结束"：先落终态，再报不活动。
            self._store.mark_child_terminal(
                self._finish_child_run_id,
                state=ChildRunState.COMPLETED,
                summary="跑完了",
            )
        raise AgentPlatformError(
            "invalid_agent_kernel_value", "Cannot cancel an inactive child run"
        )


def _spec(parent_run_id: str = "run_parent", call_id: str = "call_1") -> SpawnSpec:
    return SpawnSpec(
        task="后台总结仓库测试现状",
        expected_output=ChildOutputSchema(name="out", schema={"type": "object"}),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=120.0,
        parent_run_id=parent_run_id,
        parent_tool_call_id=call_id,
    )


class AsyncDispatchTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "async.db")
        self.database.initialize()
        self.store = SqliteDelegationChildRunRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _coordinator(self, kernel, **overrides):
        payload = {
            "parent_run_id": "run_parent",
            "parent_conversation_id": "conv_parent",
            "parent_capabilities": _PARENT_CAPABILITIES,
            "parent_trace": TraceContext(
                trace_id="trace_1", run_id="run_parent", correlation_id="corr_1"
            ),
            "child_run_store": self.store,
            "task_supervisor": BackgroundTaskSupervisor(name="test-delegation"),
        }
        payload.update(overrides)
        return InProcessChildCoordinator(kernel, **payload)

    def _reserve_row(self, child_run_id: str = "child_reserved") -> str:
        """直接经库占一行 running：模拟"内存里没有记录、只剩库"的协调器视野。"""
        self.store.reserve_child_run(
            child_run_id=child_run_id,
            parent_run_id="run_parent",
            parent_tool_call_id="call_reserved",
            parent_conversation_id="conv_parent",
            depth=1,
            capability_profile="subagent_readonly",
            workspace_mode="read_only_shared",
            timeout_seconds=120.0,
            task="后台总结仓库测试现状",
        )
        return child_run_id

    async def test_start_returns_handle_before_the_child_finishes(self) -> None:
        kernel = _GateKernel()
        coordinator = self._coordinator(kernel)

        child_run_id = coordinator.start(_spec())

        # 关键断言：句柄已经拿到，而子运行还挂在 run 里。
        self.assertTrue(child_run_id.startswith("child_"))
        self.assertEqual(ChildRunState.RUNNING, self.store.get_child_run(child_run_id).state)
        self.assertEqual(1, coordinator.running_child_count())

        kernel.release.set()
        await coordinator.drain()

        self.assertEqual(
            ChildRunState.COMPLETED, self.store.get_child_run(child_run_id).state
        )
        self.assertEqual("后台结论", self.store.get_child_run(child_run_id).summary)
        self.assertEqual(0, coordinator.running_child_count())

    async def test_start_twice_for_one_parent_tool_call_is_refused(self) -> None:
        kernel = _GateKernel()
        coordinator = self._coordinator(kernel)
        coordinator.start(_spec())

        with self.assertRaises(AgentPlatformError) as caught:
            coordinator.start(_spec())

        self.assertEqual("delegation_duplicate_spawn", caught.exception.code)
        kernel.release.set()
        await coordinator.drain()

    async def test_start_without_supervisor_fails_loudly(self) -> None:
        coordinator = self._coordinator(_GateKernel(), task_supervisor=None)
        with self.assertRaises(AgentPlatformError) as caught:
            coordinator.start(_spec())
        self.assertEqual("delegation_task_supervisor_missing", caught.exception.code)

    async def test_shutdown_marks_running_children_cancelled(self) -> None:
        kernel = _GateKernel()
        coordinator = self._coordinator(kernel)
        child_run_id = coordinator.start(_spec())
        await kernel.started.wait()

        await coordinator.shutdown()

        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(child_run_id).state
        )

    async def test_spawn_still_runs_to_completion(self) -> None:
        """既有阻塞入口语义不变：测试与批量调度继续用它。"""
        kernel = _GateKernel()
        kernel.release.set()
        coordinator = self._coordinator(kernel)
        child_run_id = await coordinator.spawn(_spec())
        self.assertEqual(
            ChildRunState.COMPLETED, self.store.get_child_run(child_run_id).state
        )

    async def test_cancel_running_child_reaches_the_kernel(self) -> None:
        kernel = _CancellableKernel()
        coordinator = self._coordinator(kernel)
        child_run_id = coordinator.start(_spec())
        await kernel.started.wait()

        await coordinator.cancel(child_run_id, "不再需要")
        await coordinator.drain()

        self.assertEqual([child_run_id], kernel.cancelled)
        self.assertEqual(["不再需要"], kernel.reasons)
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(child_run_id).state
        )

    async def test_cancel_queued_child_waiting_on_the_gate(self) -> None:
        """闸门外排队的子运行内核里没有活动运行：取消必须落在它的后台任务上。"""
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=4
            ),
        )
        running_id = coordinator.start(_spec(call_id="call_1"))
        queued_id = coordinator.start(_spec(call_id="call_2"))
        await kernel.started.wait()

        # 并发上限 1：第二个还卡在闸门外，内核没被调用过它。
        self.assertEqual(1, len(kernel.commands))
        self.assertEqual(ChildRunState.RUNNING, self.store.get_child_run(queued_id).state)

        await coordinator.cancel(queued_id, "不用做了")  # 不抛 = 取消成功

        # 取消在返回前就落了终态，而且它确实没进过内核。
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(queued_id).state
        )
        self.assertEqual(1, len(kernel.commands))

        # 守卫：0 是这条路径的**诚实真值**——它在闸门外就被取消，从未有过
        # run/executor，没有任何用量可回填。不许后来者把它"顺手修成"编造的数字。
        queued_row = self.store.get_child_run(queued_id)
        self.assertIsNotNone(queued_row.terminal_at)
        self.assertEqual(
            (0, 0, 0, 0),
            (
                queued_row.input_tokens,
                queued_row.output_tokens,
                queued_row.model_turns,
                queued_row.tool_calls,
            ),
        )

        kernel.release.set()
        await coordinator.drain()

        self.assertEqual(
            ChildRunState.COMPLETED, self.store.get_child_run(running_id).state
        )
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(queued_id).state
        )

    async def test_cancel_queued_child_before_any_step_is_not_a_lie(self) -> None:
        """后台任务还没跑第一个事件循环步就被取消：行不能停在 running。

        start() 只同步写库 + 注册任务，两个子运行的任务都还没被事件循环调度过；
        紧接着 select 路径取消第二个，asyncio 会因为 ``_must_cancel`` 直接把
        CancelledError 灌给**尚未启动的协程**（协程体一行都不执行），所以
        ``_run_reserved`` 里的收尾永远跑不到——库行必须由 cancel 自己补写。
        """
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=4
            ),
        )
        # 关键：两个 start 与 cancel 之间没有任何 await，任务拿不到第一个步。
        running_id = coordinator.start(_spec(call_id="call_1"))
        queued_id = coordinator.start(_spec(call_id="call_2"))
        self.assertEqual(
            ChildRunState.RUNNING, self.store.get_child_run(queued_id).state
        )

        await coordinator.cancel(queued_id, "不用做了")  # 不抛 = 取消成功

        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(queued_id).state
        )
        self.assertEqual(
            ChildRunState.RUNNING, self.store.get_child_run(running_id).state
        )

        # 守卫：这是一条"从未起步"的收尾（后台任务在第一个事件循环步之前就被取消，
        # 从未有过 run/executor）。0 是它的诚实真值，不许改成编造的数字。
        queued_row = self.store.get_child_run(queued_id)
        self.assertIsNotNone(queued_row.terminal_at)
        self.assertEqual(
            (0, 0, 0, 0),
            (
                queued_row.input_tokens,
                queued_row.output_tokens,
                queued_row.model_turns,
                queued_row.tool_calls,
            ),
        )

        kernel.release.set()
        await coordinator.shutdown()
        self.assertFalse(coordinator.has_inflight_children())

    async def test_shutdown_marks_never_stepped_queued_child_cancelled(self) -> None:
        """关停路径的同一个洞：没进过闸门、也没被调度过的行必须收尾。"""
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=4
            ),
        )
        running_id = coordinator.start(_spec(call_id="call_1"))
        queued_id = coordinator.start(_spec(call_id="call_2"))

        await coordinator.shutdown()

        # 两个后台任务都没跑过第一个事件循环步，收尾只能由 shutdown 自己做。
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(running_id).state
        )
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(queued_id).state
        )
        self.assertFalse(coordinator.has_inflight_children())

        # 收尾：两个任务都没被调度过，显式取消后再关停，避免跨用例残留。
        kernel.release.set()
        await coordinator.shutdown()
        self.assertTrue(
            all(row.terminal for row in self.store.list_child_runs("run_parent"))
        )

    def test_finalize_does_not_touch_blocking_path_children(self) -> None:
        """阻塞路径（``spawn`` / ``execute_prepared``）的子运行不归主管管。

        它们的 ``_store_terminal`` 还在途：收尾把它们标成 cancelled 只会让那个
        写者撞 InvalidStateError，所以收尾只看本协调器交给主管跑过的任务。
        """
        child_run_id = self._reserve_row()
        coordinator = self._coordinator(_GateKernel())

        coordinator.finalize_cancelled_children()

        self.assertEqual(
            ChildRunState.RUNNING, self.store.get_child_run(child_run_id).state
        )

    async def test_shutdown_finalizes_never_stepped_children(self) -> None:
        """两个子运行都没跑过第一个事件循环步：关停必须补写终态并摘牌。"""
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=4
            ),
        )
        # 关键：两个 start 与 shutdown 之间没有任何 await，任务拿不到第一个步。
        running_id = coordinator.start(_spec(call_id="call_1"))
        queued_id = coordinator.start(_spec(call_id="call_2"))

        await coordinator.shutdown()

        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(running_id).state
        )
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(queued_id).state
        )
        # 协程体一行都没跑，``_run_reserved`` 的 finally 也摘不了牌：收尾负责清空。
        self.assertEqual({}, coordinator._child_tasks)

    async def test_cancel_terminal_child_still_refused(self) -> None:
        kernel = _GateKernel()
        kernel.release.set()
        coordinator = self._coordinator(kernel)
        child_run_id = await coordinator.spawn(_spec())

        with self.assertRaises(AgentPlatformError) as caught:
            await coordinator.cancel(child_run_id, "太晚了")

        self.assertEqual("delegation_child_not_active", caught.exception.code)

    async def test_children_limit_is_enforced_per_parent(self) -> None:
        """绝对在册上限（含排队）仍然按父运行强制：到顶即拒，不退化成无界队列。

        在册容量本身在 M3 Task 7 改成了"排队"，所以这里钉的是 `max_queued_children`
        那道绝对墙。
        """
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1,
                max_children_per_parent=2,
                max_queued_children=2,
            ),
        )
        coordinator.start(_spec(call_id="call_1"))
        coordinator.start(_spec(call_id="call_2"))

        with self.assertRaises(AgentPlatformError) as caught:
            coordinator.start(_spec(call_id="call_3"))

        self.assertEqual("delegation_children_limit_exceeded", caught.exception.code)
        self.assertEqual(2, caught.exception.details["max_queued_children"])
        kernel.release.set()
        await coordinator.drain()

    async def test_concurrency_gate_serialises_children(self) -> None:
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel, batch_limits=ChildBatchLimits(max_concurrent_children=1, max_children_per_parent=4)
        )
        coordinator.start(_spec(call_id="call_1"))
        coordinator.start(_spec(call_id="call_2"))
        await kernel.started.wait()

        # 并发上限 1：第二个还卡在闸门外，内核只被调用一次。
        self.assertEqual(1, len(kernel.commands))
        kernel.release.set()
        await coordinator.drain()
        self.assertEqual(2, len(kernel.commands))

    async def test_in_book_limit_counts_only_non_terminal_children(self) -> None:
        """在册上限只数"还在跑"的子运行：跑完的旧行不该永久锁死父运行。"""
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        kernel.release.set()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=2
            ),
        )
        coordinator.start(_spec(call_id="call_1"))
        coordinator.start(_spec(call_id="call_2"))
        await coordinator.drain()

        rows = self.store.list_child_runs("run_parent")
        self.assertEqual(2, len(rows))
        self.assertTrue(all(row.terminal for row in rows))

        # 两行都已是终态：第三次 spawn 不占用在册额度，必须放行。
        third = coordinator.start(_spec(call_id="call_3"))
        self.assertTrue(third.startswith("child_"))
        await coordinator.drain()

    async def test_query_and_cancel_are_scoped_to_the_owning_parent(self) -> None:
        """库是共享的：**别的会话**的子运行对本协调器等于不存在。

        范围是会话而不是运行：同一会话里的下一个用户轮次（新 run id）必须还能
        管住那条子运行（见
        ``test_query_and_cancel_reach_children_of_later_runs_in_the_same_conversation``），
        所以这里的否定面只能由**跨会话**构成。
        """
        kernel = _GateKernel()
        owner = self._coordinator(kernel)
        other = self._coordinator(
            kernel,
            parent_run_id="run_other",
            parent_conversation_id="conv_other",
            parent_trace=TraceContext(
                trace_id="trace_2", run_id="run_other", correlation_id="corr_2"
            ),
        )
        child_run_id = owner.start(_spec())
        await kernel.started.wait()

        with self.assertRaises(AgentPlatformError) as queried:
            await other.query(child_run_id)
        self.assertEqual("delegation_unknown_child", queried.exception.code)

        with self.assertRaises(AgentPlatformError) as cancelled:
            await other.cancel(child_run_id, "越权")
        self.assertEqual("delegation_unknown_child", cancelled.exception.code)

        kernel.release.set()
        await owner.drain()

    def test_covers_conversation_scopes_to_the_owning_conversation(self) -> None:
        """协调器只代答自己会话的查询：跨会话为 False，未指定/无会话信息为 True。

        这是 `child_workspace_id_for` 内存循环的闸门，判定必须与 `_owns_row`
        同边界（会话，而不是运行）。
        """
        coordinator = self._coordinator(_GateKernel(), parent_conversation_id="conv_a")

        self.assertTrue(coordinator.covers_conversation("conv_a"))
        self.assertFalse(coordinator.covers_conversation("conv_b"))
        self.assertTrue(coordinator.covers_conversation(None))

    def test_covers_conversation_without_own_conversation_id_falls_back(self) -> None:
        """协调器自身没有会话信息时不做限定（兼容未注入会话的构造路径）。"""
        coordinator = self._coordinator(_GateKernel(), parent_conversation_id="")

        self.assertTrue(coordinator.covers_conversation("conv_b"))
        self.assertTrue(coordinator.covers_conversation(None))

    async def test_query_and_cancel_reach_children_of_later_runs_in_the_same_conversation(
        self,
    ) -> None:
        """边界是会话而不是运行：下一轮（新 run id）仍能查询/取消老子运行。

        子运行刻意比父运行活得久，而每次用户消息都是一个新 run——按运行限定范围
        的话，跨轮次就再也够不到那个还活着的子运行，"跨轮次仍能管住"这条验收
        标准就落空了。
        """
        kernel = _CancellableKernel()
        supervisor = BackgroundTaskSupervisor(name="test-delegation-shared")
        owner = self._coordinator(kernel, task_supervisor=supervisor)
        child_run_id = owner.start(_spec())
        await kernel.started.wait()

        # 下一轮：新的 run id，同一个会话；库里那行仍然是 running。
        later_turn = self._coordinator(
            kernel,
            parent_run_id="run_2",
            parent_trace=TraceContext(
                trace_id="trace_2", run_id="run_2", correlation_id="corr_2"
            ),
            task_supervisor=supervisor,
        )

        # 查询够得到它：回答是"仍在运行中"，而不是"查无此子运行"。
        with self.assertRaises(AgentPlatformError) as queried:
            await later_turn.query(child_run_id)
        self.assertEqual("delegation_child_running", queried.exception.code)

        # 取消也够得到它：内核收到了这次取消，落库是 cancelled。
        await later_turn.cancel(child_run_id, "跨轮次取消")
        await owner.drain()

        self.assertEqual([child_run_id], kernel.cancelled)
        self.assertEqual(["跨轮次取消"], kernel.reasons)
        self.assertEqual(
            ChildRunState.CANCELLED, self.store.get_child_run(child_run_id).state
        )
        # 终态之后，下一轮照样查得到结果。
        outcome = await later_turn.query(child_run_id)
        self.assertEqual(ChildStatus.CANCELLED, outcome.status)
        self.assertEqual(child_run_id, outcome.child_run_id)

    async def test_inflight_children_are_scoped_to_the_owning_parent(self) -> None:
        """回收判忙按本父运行：共用一个主管时全局在跑数不能算到别人头上。"""
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        supervisor = BackgroundTaskSupervisor(name="test-delegation-shared")
        limits = ChildBatchLimits(max_concurrent_children=2, max_children_per_parent=4)
        owner = self._coordinator(
            kernel, batch_limits=limits, task_supervisor=supervisor
        )
        other = self._coordinator(
            kernel,
            parent_run_id="run_other",
            parent_trace=TraceContext(
                trace_id="trace_2", run_id="run_other", correlation_id="corr_2"
            ),
            batch_limits=limits,
            task_supervisor=supervisor,
        )
        owner.start(_spec())
        await kernel.started.wait()

        # 共用主管的全局在跑数把别的父运行也算了进来——这正是不能拿它判忙的原因。
        self.assertEqual(1, other.running_child_count())
        self.assertTrue(owner.has_inflight_children())
        self.assertFalse(other.has_inflight_children())

        kernel.release.set()
        await owner.drain()

        self.assertFalse(owner.has_inflight_children())
        self.assertFalse(other.has_inflight_children())

    async def test_cancel_terminal_row_without_memory_record_reports_completed(self) -> None:
        kernel = _GateKernel()
        kernel.release.set()
        coordinator = self._coordinator(kernel)
        child_run_id = coordinator.start(_spec())
        await coordinator.drain()

        fresh = self._coordinator(_GateKernel())
        with self.assertRaises(AgentPlatformError) as caught:
            await fresh.cancel(child_run_id, "太晚了")

        self.assertEqual("delegation_child_not_active", caught.exception.code)
        self.assertEqual("completed", caught.exception.details["status"])

    async def test_cancel_surfaces_kernel_inactive_error_while_still_running(self) -> None:
        """库里还是 running 且我们手里没有它的后台任务：内核报错原样抛出。

        闸门外排队的子运行由后台任务兜底（见
        ``test_cancel_queued_child_waiting_on_the_gate``），这条钉子钉的是
        "没有活任务可取消"时的回落路径。
        """
        child_run_id = self._reserve_row()
        kernel = _InactiveKernel()
        coordinator = self._coordinator(kernel)

        with self.assertRaises(AgentPlatformError) as caught:
            await coordinator.cancel(child_run_id, "取消")

        self.assertEqual("invalid_agent_kernel_value", caught.exception.code)
        self.assertEqual(["取消"], kernel.reasons)

    async def test_cancel_translates_kernel_inactive_when_child_finished_mid_call(self) -> None:
        child_run_id = self._reserve_row()
        kernel = _InactiveKernel(store=self.store, finish_child_run_id=child_run_id)
        coordinator = self._coordinator(kernel)

        with self.assertRaises(AgentPlatformError) as caught:
            await coordinator.cancel(child_run_id, "取消")

        # 内核调用期间子运行刚好跑完：以库为准复核，翻译成"已结束"。
        self.assertEqual("delegation_child_not_active", caught.exception.code)
        self.assertEqual("completed", caught.exception.details["status"])

    async def test_start_after_shutdown_fails_and_leaves_no_running_row(self) -> None:
        kernel = _GateKernel()
        coordinator = self._coordinator(kernel)
        await coordinator.shutdown()

        with self.assertRaises(AgentPlatformError) as caught:
            coordinator.start(_spec())

        self.assertEqual("delegation_task_supervisor_closed", caught.exception.code)
        # 主管关闭在本进程内不可复活：不可重试，避免模型去重试注定失败的请求。
        self.assertFalse(caught.exception.retryable)
        rows = self.store.list_child_runs("run_parent")
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0].terminal)
        self.assertEqual(ChildRunState.FAILED, rows[0].state)
        self.assertEqual("delegation_supervisor_closed", rows[0].error_code)
        # 守卫：这条路径**从来没有**运行/执行器（内核压根没被调用），所以四列
        # 保持 0 是诚实的真值。后来者不许把它"顺手修成"编造的数字——成本账的
        # 修补只针对真花过 token 的终态路径。
        self.assertEqual(0, rows[0].input_tokens)
        self.assertEqual(0, rows[0].output_tokens)
        self.assertEqual(0, rows[0].model_turns)
        self.assertEqual(0, rows[0].tool_calls)

    async def test_started_at_separates_queued_from_running(self) -> None:
        """``started_at`` 是"排队中"与"真在跑"的分界：闸门外排队必须是 NULL。

        取消/关停的设计依赖这条分界（排队中的子运行内核里没有活动运行），所以它
        必须在**拿到闸门之后、调内核之前**写，而不是 `start()` 落 running 的时候。
        """
        from endless_task.delegation import ChildBatchLimits

        kernel = _GateKernel()
        coordinator = self._coordinator(
            kernel,
            batch_limits=ChildBatchLimits(
                max_concurrent_children=1, max_children_per_parent=4
            ),
        )
        running_id = coordinator.start(_spec(call_id="call_1"))
        queued_id = coordinator.start(_spec(call_id="call_2"))
        await kernel.started.wait()

        # 第一个已经进了内核：它有 started_at；第二个还卡在闸门外：它没有。
        self.assertIsNotNone(self.store.get_child_run(running_id).started_at)
        self.assertIsNone(self.store.get_child_run(queued_id).started_at)

        kernel.release.set()
        await coordinator.drain()

        # 轮到它进内核之后，时刻才被写上。
        self.assertIsNotNone(self.store.get_child_run(queued_id).started_at)
