"""M4A DR-1 slice 2: in-process child batch scheduler prefigure."""

from __future__ import annotations

import asyncio
import unittest

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
        async def scenario() -> None:
            _, scheduler = build(
                limits=ChildBatchLimits(
                    max_concurrent_children=3,
                    max_children_per_parent=3,
                )
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await scheduler.run_batch(
                    [child_spec(f"call_{i}") for i in range(4)]
                )
            self.assertEqual("delegation_children_limit_exceeded", caught.exception.code)

        run(scenario())

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
