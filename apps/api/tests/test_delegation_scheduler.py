"""M4A DR-1 slice 2: in-process child batch scheduler prefigure."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    InProcessChildCoordinator,
    InProcessChildScheduler,
    ChildBatchLimits,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.runtime_ledger import TraceContext

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "memory.read",
        "session.query",
    }
)


def trace_context() -> TraceContext:
    return TraceContext(
        trace_id="trace_parent_1",
        run_id="run_parent_1",
        correlation_id="corr_1",
    )


def child_spec(tool_call_id: str) -> SpawnSpec:
    return SpawnSpec(
        task="调查并总结",
        expected_output=ChildOutputSchema(
            name="research_output",
            schema={"type": "object"},
        ),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read", "session.query"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=120.0,
        parent_run_id="run_parent_1",
        parent_tool_call_id=tool_call_id,
    )


class ConcurrencyKernel:
    """Kernel that tracks the maximum number of in-flight child runs."""

    def __init__(self) -> None:
        self.commands: list[RunCommand] = []
        self.fail_tool_call_ids: set[str] = set()
        self.max_inflight = 0
        self._inflight = 0

    async def run(self, command: RunCommand) -> RunOutcome:
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            self.commands.append(command)
            await asyncio.sleep(0.005)
            if command.metadata["delegation"]["parent_tool_call_id"] in self.fail_tool_call_ids:
                raise AgentPlatformError(
                    "provider_unavailable",
                    "模型服务不可用。",
                    retryable=True,
                )
            return RunOutcome(
                run_id=command.run_id,
                status=RunOutcomeStatus.COMPLETED,
                trace=command.trace,
                assistant_message=AgentMessage(
                    role=AgentMessageRole.ASSISTANT,
                    content="调查完成。",
                ),
            )
        finally:
            self._inflight -= 1

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class GatedConcurrencyKernel(ConcurrencyKernel):
    """可控内核：``run`` 进入后挂住，直到测试放行（在册排队用例专用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, command: RunCommand) -> RunOutcome:
        self._inflight += 1
        self.max_inflight = max(self.max_inflight, self._inflight)
        try:
            self.commands.append(command)
            self.started.set()
            await self.release.wait()
            if command.metadata["delegation"]["parent_tool_call_id"] in self.fail_tool_call_ids:
                raise AgentPlatformError(
                    "provider_unavailable",
                    "模型服务不可用。",
                    retryable=True,
                )
            return RunOutcome(
                run_id=command.run_id,
                status=RunOutcomeStatus.COMPLETED,
                trace=command.trace,
                assistant_message=AgentMessage(
                    role=AgentMessageRole.ASSISTANT,
                    content="调查完成。",
                ),
            )
        finally:
            self._inflight -= 1


def _open_store(directory: str):
    """临时目录里的真实子运行库：在册计数（inflight）以库为准。"""
    from endless_task.storage import Database, SqliteDelegationChildRunRepository

    database = Database(Path(directory) / "scheduler.db")
    database.initialize()
    return SqliteDelegationChildRunRepository(database)


async def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("条件在超时前没有成立")
        await asyncio.sleep(0.001)


def build(kernel=None, *, limits=None) -> tuple[InProcessChildCoordinator, InProcessChildScheduler]:
    if kernel is None:
        kernel = ConcurrencyKernel()
    coordinator = InProcessChildCoordinator(
        kernel,
        parent_run_id="run_parent_1",
        parent_capabilities=PARENT_CAPABILITIES,
        parent_trace=trace_context(),
    )
    scheduler = InProcessChildScheduler(coordinator, limits=limits)
    return coordinator, scheduler


def run(coro) -> None:
    asyncio.run(coro)


class ChildBatchLimitsTest(unittest.TestCase):
    def test_defaults_match_doc_limits(self) -> None:
        limits = ChildBatchLimits()
        self.assertEqual(4, limits.max_concurrent_children)
        self.assertEqual(8, limits.max_children_per_parent)

    def test_invalid_limits_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            ChildBatchLimits(max_concurrent_children=0)
        self.assertEqual("invalid_child_batch_limits", caught.exception.code)
        with self.assertRaises(AgentPlatformError):
            ChildBatchLimits(max_concurrent_children=10, max_children_per_parent=4)


class ChildSchedulerTest(unittest.TestCase):
    def test_four_read_only_children_complete_in_parallel(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            coordinator, scheduler = build(kernel)
            summary = await scheduler.run_batch(
                [child_spec(f"call_{i}") for i in range(4)]
            )
            self.assertEqual(4, summary.requested)
            self.assertEqual(4, len(summary.completed))
            self.assertEqual(0, len(summary.failed))
            self.assertEqual(4, len(coordinator.tracked_child_run_ids))
            self.assertEqual(4, len(kernel.commands))
            self.assertGreaterEqual(kernel.max_inflight, 2)  # ran in parallel

        run(scenario())

    def test_concurrency_capped_at_limit(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            _, scheduler = build(
                kernel,
                limits=ChildBatchLimits(max_concurrent_children=2),
            )
            await scheduler.run_batch(
                [child_spec(f"call_{i}") for i in range(6)]
            )
            self.assertLessEqual(kernel.max_inflight, 2)

        run(scenario())

    def test_collect_all_keeps_siblings_on_child_failure(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            kernel.fail_tool_call_ids = {"call_2"}
            _, scheduler = build(kernel)
            summary = await scheduler.run_batch(
                [child_spec(f"call_{i}") for i in range(4)]
            )
            self.assertEqual(3, len(summary.completed))
            self.assertEqual(1, len(summary.failed))
            failed = summary.failed[0]
            self.assertEqual("provider_unavailable", failed.diagnostics[0].code)

        run(scenario())

    def test_policy_violation_aborts_batch_before_any_child_runs(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            coordinator, scheduler = build(kernel)
            bad = SpawnSpec(
                task="写文件",
                expected_output=ChildOutputSchema(
                    name="output",
                    schema={"type": "object"},
                ),
                capability_profile="subagent_workspace",
                requested_capabilities=frozenset({"workspace.write"}),
                tool_allowlist=(),
                model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
                context_policy=ChildContextPolicy(),
                workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
                timeout_seconds=120.0,
                parent_run_id="run_parent_1",
                parent_tool_call_id="call_bad",
            )
            specs = [child_spec("call_1"), bad, child_spec("call_3")]
            with self.assertRaises(AgentPlatformError) as caught:
                await scheduler.run_batch(specs)
            self.assertEqual("delegation_profile_not_allowed", caught.exception.code)
            # Fail fast: zero children ran and zero spawn keys were reserved.
            self.assertEqual(0, len(kernel.commands))
            self.assertEqual(0, len(coordinator.tracked_child_run_ids))

        run(scenario())

    def test_children_limit_exceeded_rejected(self) -> None:
        """整批只在越过**绝对**上限（max_queued_children）时拒绝。

        在册容量（max_children_per_parent）满**不是**拒绝理由——那是排队的事。
        """

        async def scenario() -> None:
            _, scheduler = build(
                limits=ChildBatchLimits(
                    max_concurrent_children=3,
                    max_children_per_parent=3,
                    max_queued_children=3,
                )
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await scheduler.run_batch(
                    [child_spec(f"call_{i}") for i in range(4)]
                )
            self.assertEqual("delegation_children_limit_exceeded", caught.exception.code)
            self.assertEqual(3, caught.exception.details["max_queued_children"])
            self.assertEqual(0, caught.exception.details["tracked"])
            self.assertEqual(4, caught.exception.details["incoming"])

        run(scenario())

    def test_full_admission_book_queues_the_batch_instead_of_rejecting(self) -> None:
        """在册已满（1/1）但绝对上限（4）还有余量：批次照常排队执行，不拒绝。

        旧预检数的是**终态记录数**（``tracked_child_run_ids``），在飞子运行不在
        其中，语义错位；这里用带库的协调器把"在飞数"钉死：第二个批次的子运行在
        admission 闸门外排队，等第一个终态后照常跑完。
        """

        async def scenario(directory: str) -> None:
            kernel = GatedConcurrencyKernel()
            limits = ChildBatchLimits(
                max_concurrent_children=1,
                max_children_per_parent=1,
                max_queued_children=4,
            )
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=_open_store(directory),
                batch_limits=limits,
            )
            scheduler = InProcessChildScheduler(coordinator, limits=limits)

            first = asyncio.create_task(scheduler.run_batch([child_spec("call_1")]))
            await kernel.started.wait()
            self.assertEqual(1, coordinator.inflight_child_count())

            # 在册 1/1 已满：不再被拒，第二个子运行排队。
            second = asyncio.create_task(scheduler.run_batch([child_spec("call_2")]))
            await _wait_for(lambda: coordinator.inflight_child_count() == 2)
            self.assertEqual(1, len(kernel.commands))

            kernel.release.set()
            first_summary, second_summary = await asyncio.gather(first, second)

            self.assertEqual(1, len(first_summary.completed))
            self.assertEqual(1, len(second_summary.completed))
            self.assertEqual(2, len(kernel.commands))

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_terminal_records_do_not_fill_the_absolute_cap(self) -> None:
        """终态记录不占在册名额：预检必须数**在飞**，不是数历史终态记录。

        先跑完一批（2 条终态记录 = 绝对上限 2，在飞归零），再派新批次：
        新公式（在飞 0 + 1 ≤ 2）放行；旧公式（终态记录 2 + 1 > 2）会误拒。
        终态记录会一直累积到保留窗口，拿它当"在册"用的话，这位父运行派满
        ``max_queued_children`` 条之后就再也派不出任何子运行了。
        """

        async def scenario(directory: str) -> None:
            kernel = ConcurrencyKernel()
            limits = ChildBatchLimits(
                max_concurrent_children=2,
                max_children_per_parent=2,
                max_queued_children=2,
            )
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=_open_store(directory),
                batch_limits=limits,
            )
            scheduler = InProcessChildScheduler(coordinator, limits=limits)

            first = await scheduler.run_batch(
                [child_spec("call_1"), child_spec("call_2")]
            )
            self.assertEqual(2, len(first.completed))
            # 账本形势：终态记录已到绝对上限，但在飞归零——没有任何名额被占。
            self.assertEqual(2, len(coordinator.tracked_child_run_ids))
            self.assertEqual(0, coordinator.inflight_child_count())

            second = await scheduler.run_batch([child_spec("call_3")])

            self.assertEqual(1, len(second.completed))
            self.assertEqual(3, len(kernel.commands))

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_inflight_children_fill_the_absolute_cap(self) -> None:
        """在飞子运行必须占满绝对名额：一条终态记录都没有也不该放行。

        两条子运行在飞（在册 2/2，都是 wire 的 queued），终态记录为 0：
        新公式（在飞 2 + 1 > 2）在**任何子运行开始前**整批拒绝；旧公式
        （终态记录 0 + 1 ≤ 2）会放行，直到下游 ``_reserve`` 才炸——那时整批
        已经 prepare 过，"零副作用快速失败"名存实亡。
        """

        async def scenario(directory: str) -> None:
            from endless_task.runtime.background_tasks import BackgroundTaskSupervisor

            kernel = GatedConcurrencyKernel()
            store = _open_store(directory)
            limits = ChildBatchLimits(
                max_concurrent_children=1,
                max_children_per_parent=1,
                max_queued_children=2,
            )
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
                task_supervisor=BackgroundTaskSupervisor(name="test-scheduler-cap"),
                batch_limits=limits,
            )
            scheduler = InProcessChildScheduler(coordinator, limits=limits)

            # 两条在飞：第 1 条在内核里（挂住），第 2 条在 admission 闸门外排队。
            # 走 ``start`` 而不是 ``run_batch``，因为调度器自己还有一层局部并发
            # 名额（同样取 max_concurrent_children=1），会把第 2 条挡在预留之前。
            coordinator.start(child_spec("call_1"))
            coordinator.start(child_spec("call_2"))
            await kernel.started.wait()
            # 关键形势：在飞 2/2，但终态记录是 0（旧公式会算出 0）。
            self.assertEqual(2, coordinator.inflight_child_count())
            self.assertEqual((), coordinator.tracked_child_run_ids)

            prepared: list[str] = []
            original_prepare = coordinator.prepare_spawn

            def spy_prepare(spec):
                prepared.append(spec.parent_tool_call_id)
                return original_prepare(spec)

            try:
                with patch.object(coordinator, "prepare_spawn", spy_prepare):
                    with self.assertRaises(AgentPlatformError) as caught:
                        await scheduler.run_batch([child_spec("call_3")])
                # 先钉"零副作用"：预检拒绝时一条 spec 都不该进 prepare。
                self.assertEqual([], prepared, "预检必须整批零副作用地拒绝")
                self.assertEqual(
                    "delegation_children_limit_exceeded", caught.exception.code
                )
                self.assertEqual(2, caught.exception.details["tracked"])
                self.assertEqual(1, caught.exception.details["incoming"])
                self.assertEqual(2, caught.exception.details["max_queued_children"])
                # 被拒的批次没有落库、没有进内核：在飞仍是 2、内核仍只有 1 条。
                self.assertEqual(2, coordinator.inflight_child_count())
                self.assertEqual(1, len(kernel.commands))
            finally:
                kernel.release.set()
                await coordinator.drain()

            # 收尾：两条在飞的都跑完，被拒的那条从未存在。
            self.assertEqual(2, len(kernel.commands))
            self.assertTrue(
                all(row.terminal for row in store.list_child_runs("run_parent_1"))
            )

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_duplicate_within_batch_rejected(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            coordinator, scheduler = build(kernel)
            with self.assertRaises(AgentPlatformError) as caught:
                await scheduler.run_batch(
                    [child_spec("call_1"), child_spec("call_1")]
                )
            self.assertEqual("delegation_duplicate_spawn", caught.exception.code)
            self.assertEqual(0, len(kernel.commands))
            self.assertEqual(0, len(coordinator.tracked_child_run_ids))

        run(scenario())

    def test_duplicate_against_spawned_child_rejected_in_prepare(self) -> None:
        async def scenario() -> None:
            kernel = ConcurrencyKernel()
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
            )
            scheduler = InProcessChildScheduler(coordinator)
            await scheduler.run_batch([child_spec("call_1")])
            with self.assertRaises(AgentPlatformError) as caught:
                await scheduler.run_batch([child_spec("call_1")])
            self.assertEqual("delegation_duplicate_spawn", caught.exception.code)
            self.assertEqual(1, len(kernel.commands))

        run(scenario())

    def test_summary_outcomes_are_sorted_and_bounded(self) -> None:
        async def scenario() -> None:
            _, scheduler = build()
            summary = await scheduler.run_batch(
                [child_spec(f"call_{i}") for i in range(3)]
            )
            ids = [outcome.child_run_id for outcome in summary.outcomes]
            self.assertEqual(sorted(ids), ids)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
