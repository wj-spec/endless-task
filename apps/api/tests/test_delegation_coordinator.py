"""M4A DR-0 slice 2: result projection + in-process child coordinator."""

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
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildResultPolicy,
    ChildRunStatus,
    InProcessChildCoordinator,
    SpawnSpec,
    WorkspaceMode,
    project_child_outcome,
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


def trace_context(run_id: str = "run_parent_1") -> TraceContext:
    return TraceContext(
        trace_id="trace_parent_1",
        run_id=run_id,
        correlation_id="corr_1",
    )


def child_spec(
    *,
    parent_run_id: str = "run_parent_1",
    parent_tool_call_id: str = "call_1",
    workspace_mode: WorkspaceMode = WorkspaceMode.READ_ONLY_SHARED,
    excerpts: tuple[str, ...] = (),
    profile: str = "subagent_readonly",
    capabilities: frozenset[str] = frozenset({"workspace.read", "session.query"}),
) -> SpawnSpec:
    return SpawnSpec(
        task="调查并总结",
        expected_output=ChildOutputSchema(
            name="research_output",
            schema={"type": "object"},
        ),
        capability_profile=profile,
        requested_capabilities=frozenset(capabilities),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
        context_policy=ChildContextPolicy(excerpts=excerpts),
        workspace_mode=workspace_mode,
        timeout_seconds=120.0,
        parent_run_id=parent_run_id,
        parent_tool_call_id=parent_tool_call_id,
    )


class ScriptedAgentKernel:
    """AgentKernel test double that echoes the command run id and records."""

    def __init__(self, *, status: RunOutcomeStatus = RunOutcomeStatus.COMPLETED) -> None:
        self._status = status
        self.commands: list[RunCommand] = []
        self.fail_next: Exception | None = None

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
        if self.fail_next is not None:
            error = self.fail_next
            self.fail_next = None
            raise error
        message = None
        if self._status is RunOutcomeStatus.COMPLETED:
            message = AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="调查完成，结论如下。",
            )
        return RunOutcome(
            run_id=command.run_id,
            status=self._status,
            trace=command.trace,
            assistant_message=message,
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError("steer is not part of DR-0 coordinator flow")

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError("cancel is not part of DR-0 coordinator flow")


class WrongRunKernel:
    """Kernel violating the run contract: answers for a different run id."""

    async def run(self, command: RunCommand) -> RunOutcome:
        return RunOutcome(
            run_id="run_someone_else",
            status=RunOutcomeStatus.COMPLETED,
            trace=TraceContext(
                trace_id="trace_parent_1",
                run_id="run_someone_else",
                correlation_id="corr_1",
            ),
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="结果。",
            ),
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


def build_coordinator(kernel=None, *, parent_depth: int = 0) -> InProcessChildCoordinator:
    if kernel is None:
        kernel = ScriptedAgentKernel()
    return InProcessChildCoordinator(
        kernel,
        parent_run_id="run_parent_1",
        parent_capabilities=PARENT_CAPABILITIES,
        parent_trace=trace_context(),
        parent_depth=parent_depth,
    )


def run(coro) -> None:
    asyncio.run(coro)


class ChildResultProjectionTest(unittest.TestCase):
    def test_completed_outcome_projected_with_summary(self) -> None:
        outcome = RunOutcome(
            run_id="run_parent_1",
            status=RunOutcomeStatus.COMPLETED,
            trace=trace_context(),
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="调查完成。",
            ),
        )
        projected = project_child_outcome(outcome)
        self.assertEqual("completed", projected.status.value)
        self.assertEqual("调查完成。", projected.summary)
        self.assertEqual("run_parent_1", projected.child_run_id)

    def test_failed_outcome_maps_to_failed_and_keeps_diagnostics(self) -> None:
        outcome = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.FAILED,
            trace=trace_context(run_id="run_1"),
            diagnostics=(
                SafeDiagnostic(
                    code="provider_error",
                    safe_message="上游服务不可用。",
                    retryable=True,
                ),
            ),
        )
        projected = project_child_outcome(outcome)
        self.assertEqual("failed", projected.status.value)
        self.assertEqual(1, len(projected.diagnostics))
        self.assertEqual("provider_error", projected.diagnostics[0].code)
        self.assertTrue(projected.diagnostics[0].retryable)

    def test_cancelled_and_interrupted_status_mapping(self) -> None:
        cancelled = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.CANCELLED,
            trace=trace_context(run_id="run_1"),
        )
        self.assertEqual("cancelled", project_child_outcome(cancelled).status.value)
        interrupted = RunOutcome(
            run_id="run_2",
            status=RunOutcomeStatus.INTERRUPTED,
            trace=trace_context(run_id="run_2"),
        )
        # INTERRUPTED has no delegation counterpart: it is a failure the
        # parent must observe, never an unknown that gets re-spawned.
        self.assertEqual("failed", project_child_outcome(interrupted).status.value)

    def test_oversized_summary_truncated_with_diagnostic(self) -> None:
        policy = ChildResultPolicy(summary_max_characters=50)
        outcome = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.COMPLETED,
            trace=trace_context(run_id="run_1"),
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="x" * 1_000,
            ),
        )
        projected = project_child_outcome(outcome, policy=policy)
        self.assertEqual(50, len(projected.summary))
        self.assertTrue(
            any(d.code == "child_summary_truncated" for d in projected.diagnostics)
        )

    def test_oversized_deliverable_diagnosed_but_summary_kept(self) -> None:
        policy = ChildResultPolicy(
            summary_max_characters=100,
            deliverable_max_characters=200,
        )
        outcome = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.COMPLETED,
            trace=trace_context(run_id="run_1"),
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="y" * 10_000,
            ),
        )
        projected = project_child_outcome(outcome, policy=policy)
        self.assertTrue(
            any(d.code == "child_deliverable_too_large" for d in projected.diagnostics)
        )
        self.assertTrue(any(d.code == "child_summary_truncated" for d in projected.diagnostics))

    def test_missing_final_message_gets_factual_summary(self) -> None:
        # The kernel protocol forbids a completed run without a message, so
        # only non-completed outcomes can reach the no-message branch.
        outcome = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.FAILED,
            trace=trace_context(run_id="run_1"),
        )
        projected = project_child_outcome(outcome)
        self.assertEqual("failed", projected.status.value)
        self.assertEqual("Child run produced no final message.", projected.summary)

    def test_non_outcome_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            project_child_outcome(object())  # type: ignore[arg-type]
        self.assertEqual("invalid_delegation_value", caught.exception.code)

    def test_invalid_policy_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            ChildResultPolicy(summary_max_characters=0)
        self.assertEqual("invalid_child_result_policy", caught.exception.code)
        with self.assertRaises(AgentPlatformError):
            ChildResultPolicy(
                summary_max_characters=100,
                deliverable_max_characters=50,
            )


class ChildCoordinatorTest(unittest.TestCase):
    def test_spawn_runs_child_and_returns_stable_child_id(self) -> None:
        async def scenario() -> None:
            kernel = ScriptedAgentKernel()
            coordinator = build_coordinator(kernel)
            child_run_id = await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            self.assertEqual(1, len(kernel.commands))
            self.assertEqual(child_run_id, kernel.commands[0].run_id)
            outcome = await coordinator.query(child_run_id)
            self.assertEqual(child_run_id, outcome.child_run_id)
            # Ids are derived deterministically from the parent key, so they
            # are stable and collision-resistant across spawn attempts.
            self.assertTrue(child_run_id.startswith("child_"))

        run(scenario())

    def test_child_run_ids_differ_between_parent_tool_calls(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            first = await coordinator.spawn(child_spec(parent_tool_call_id="call_a"))
            second = await coordinator.spawn(child_spec(parent_tool_call_id="call_b"))
            self.assertNotEqual(first, second)
            self.assertEqual(2, len(coordinator.tracked_child_run_ids))

        run(scenario())

    def test_spawn_writes_context_from_task_and_excerpts_only(self) -> None:
        async def scenario() -> None:
            kernel = ScriptedAgentKernel()
            coordinator = build_coordinator(kernel)
            spec = child_spec(excerpts=("source_a 摘要", "source_b 摘要"))
            await coordinator.spawn(spec)
            command = kernel.commands[0]
            self.assertEqual("subagent_readonly", command.capability_profile)
            self.assertEqual(AgentMessageRole.USER, command.message.role)
            self.assertIn("调查并总结", command.message.content)
            self.assertIn("source_a 摘要", command.message.content)
            self.assertIn("source_b 摘要", command.message.content)
            self.assertNotIn("完整对话", command.message.content)
            self.assertIsNotNone(command.deadline_monotonic)

        run(scenario())

    def test_query_returns_projected_outcome(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            child_run_id = await coordinator.spawn(child_spec())
            outcome = await coordinator.query(child_run_id)
            self.assertEqual("completed", outcome.status.value)
            self.assertEqual(child_run_id, outcome.child_run_id)

        run(scenario())

    def test_parent_mismatch_rejected(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec(parent_run_id="run_other"))
            self.assertEqual("delegation_parent_mismatch", caught.exception.code)

        run(scenario())

    def test_isolated_workspace_mode_stage_gate_denied(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            spec = child_spec(
                workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT,
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(spec)
            self.assertEqual(
                "delegation_workspace_mode_not_supported",
                caught.exception.code,
            )

        run(scenario())

    def test_read_only_profile_write_request_denied(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            spec = child_spec(
                capabilities=frozenset({"workspace.write"}),
                workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(spec)
            self.assertEqual("delegation_read_only_violation", caught.exception.code)

        run(scenario())

    def test_work_profile_child_rejected_at_m4a_stage_gate(self) -> None:
        # A non-read-only profile whose write capability the parent holds is
        # still refused even in a legal read-only workspace: M4A executes
        # read-only children only (M4B adds isolated write).
        async def scenario() -> None:
            coordinator = build_coordinator()
            spec = child_spec(
                capabilities=frozenset({"workspace.write"}),
                profile="subagent_workspace",
                workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(spec)
            self.assertEqual("delegation_write_child_not_supported", caught.exception.code)

        run(scenario())

    def test_duplicate_spawn_for_same_parent_tool_call_denied(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            self.assertEqual("delegation_duplicate_spawn", caught.exception.code)
            self.assertEqual(1, len(coordinator.tracked_child_run_ids))

        run(scenario())

    def test_depth_gate_denies_grandchild(self) -> None:
        async def scenario() -> None:
            # Coordinator owned by a run that is itself depth 1: spawning a
            # child from it would make depth 2, past MAX_DEPTH=1.
            coordinator = build_coordinator(parent_depth=1)
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec())
            self.assertEqual("delegation_max_depth_exceeded", caught.exception.code)

        run(scenario())

    def test_unknown_child_query_denied(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.query("child_missing")
            self.assertEqual("delegation_unknown_child", caught.exception.code)

        run(scenario())

    def test_terminal_child_cancel_denied(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            child_run_id = await coordinator.spawn(child_spec())
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.cancel(child_run_id, "不再需要")
            self.assertEqual("delegation_child_not_active", caught.exception.code)

        run(scenario())

    def test_kernel_failure_becomes_failed_record(self) -> None:
        async def scenario() -> None:
            kernel = ScriptedAgentKernel()
            coordinator = build_coordinator(kernel)
            kernel.fail_next = AgentPlatformError(
                "provider_unavailable",
                "模型服务不可用。",
                retryable=True,
            )
            child_run_id = await coordinator.spawn(child_spec())
            outcome = await coordinator.query(child_run_id)
            self.assertEqual("failed", outcome.status.value)
            self.assertEqual("provider_unavailable", outcome.diagnostics[0].code)
            self.assertTrue(outcome.diagnostics[0].retryable)

        run(scenario())

    def test_kernel_run_id_mismatch_is_fail_closed_and_key_released(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator(WrongRunKernel())
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            self.assertEqual("delegation_child_run_mismatch", caught.exception.code)
            # The spawn key was released: a corrected kernel may retry the
            # same parent tool call without tripping the duplicate gate.
            self.assertEqual(0, len(coordinator.tracked_child_run_ids))

        run(scenario())


class ChildRecordIntrospectionTest(unittest.TestCase):
    def test_record_keeps_lineage_and_decision(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            record = coordinator.children[0]
            self.assertEqual("run_parent_1", record.parent_run_id)
            self.assertEqual("call_1", record.parent_tool_call_id)
            self.assertEqual(1, record.depth)
            self.assertEqual(ChildRunStatus.COMPLETED, record.status)
            self.assertTrue(record.decision.read_only)

        run(scenario())


if __name__ == "__main__":
    unittest.main()
