"""M4A DR-0 slice 2: result projection + in-process child coordinator."""

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
    RunUsage,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.delegation import (
    READ_ONLY_FORBIDDEN_CAPABILITIES as READ_ONLY_FORBIDDEN,
    ChildBatchLimits,
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
from endless_task.delegation.child_run_store import ChildRunState
from endless_task.runtime_ledger import TraceContext
from endless_task.tool_platform import create_builtin_profile_registry

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


class SnapshotBackedMismatchKernel:
    """契约违规内核：答回不匹配的 run id，但**自带**可信的留存快照读口。

    它刻意在两个地方给出**互相矛盾**的用量：``outcome.usage`` 属于那个我们刚
    拒绝归属的 run id（不可信），``usage_snapshot`` 按 ``child_run_id`` 留存
    （可信）。协调器只能用后者——否则就是把别人的账记到这条子运行头上。
    """

    def __init__(self, child_run_id: str) -> None:
        self.child_run_id = child_run_id
        self.snapshot_run_ids: list[str] = []

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
            # 伪造的账：真实花费是 11/7/1/1，这里故意写成 999。
            usage=RunUsage(
                input_tokens=999, output_tokens=999, model_turns=9, tool_calls=9
            ),
        )

    def usage_snapshot(self, run_id: str) -> RunUsage:
        self.snapshot_run_ids.append(run_id)
        # 只有按 child_run_id 查才可信。若协调器拿那个不匹配的 run id（或 outcome
        # 里那个 999）来查，就让测试立刻炸——比事后比对断言更早暴露错记。
        if run_id != self.child_run_id:
            raise AssertionError(f"读口被用错了 run id：{run_id!r}")
        return RunUsage(input_tokens=11, output_tokens=7, model_turns=1, tool_calls=1)

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class SnapshotKernel:
    """Echo 内核 + 留存快照读口：用于"逃逸兜底"路径的用量回填。"""

    def __init__(self) -> None:
        self.snapshot_run_ids: list[str] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="调查完成，结论如下。",
            ),
        )

    def usage_snapshot(self, run_id: str) -> RunUsage:
        self.snapshot_run_ids.append(run_id)
        return RunUsage(input_tokens=11, output_tokens=7, model_turns=1, tool_calls=2)

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class ExplodingSnapshotKernel:
    """读口自身抛异常的外来内核：收尾**绝不能**因此中断（行不许停在 running）。"""

    def __init__(self) -> None:
        self.snapshot_run_ids: list[str] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        raise RuntimeError("先炸在内核里")

    def usage_snapshot(self, run_id: str) -> RunUsage:
        self.snapshot_run_ids.append(run_id)
        raise RuntimeError("读口炸了")

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class MalformedSnapshotKernel:
    """读口**返回**（而非抛出）畸形值的外来内核：收尾同样绝不允许被中断。

    鸭子类型读口服务的就是外来内核，它们完全可能返回一个形状不对的对象：
    ``_usage_summary`` 会抛 ``AttributeError``（dict/字符串之类）或
    ``AgentPlatformError``（负数字段）。所以映射必须和探针调用一起待在
    ``_best_available_usage`` 自己的 try/except **之内**。
    """

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot
        self.snapshot_run_ids: list[str] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        # 内核先炸：走 `_failed_record` 那条路（读口在那里被调用）。
        raise RuntimeError("先炸在内核里")

    def usage_snapshot(self, run_id: str) -> object:
        self.snapshot_run_ids.append(run_id)
        return self._snapshot

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


class _NegativeFieldUsage:
    """负数字段的鸭子类型用量：``RunUsage``/``UsageSummary`` 都拒绝构造它。"""

    input_tokens = -1
    output_tokens = 7
    model_turns = 1
    tool_calls = 1


def _malformed_snapshots() -> list[tuple[str, object]]:
    """三种畸形返回：dict 形状、短字符串、负数字段。"""
    return [
        ("dict", {"input_tokens": 5}),
        ("str", "11"),
        ("negative", _NegativeFieldUsage()),
    ]


class GatedEchoKernel:
    """可控内核：``run`` 进入后挂住，直到测试放行（在册排队用例专用）。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.commands: list[RunCommand] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
        self.started.set()
        await self.release.wait()
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="排队后完成。",
            ),
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError("steer 不属于在册排队流程")

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError("cancel 不属于在册排队流程")


def build_coordinator(
    kernel=None,
    *,
    parent_depth: int = 0,
    allowed_profile_names=None,
    profile_resolver=None,
    isolated_write_enabled: bool = False,
) -> InProcessChildCoordinator:
    if kernel is None:
        kernel = ScriptedAgentKernel()
    return InProcessChildCoordinator(
        kernel,
        parent_run_id="run_parent_1",
        parent_capabilities=PARENT_CAPABILITIES,
        parent_trace=trace_context(),
        parent_depth=parent_depth,
        allowed_profile_names=allowed_profile_names,
        profile_resolver=profile_resolver,
        isolated_write_enabled=isolated_write_enabled,
    )


def _open_store(directory: str):
    """在临时目录里开一份真实的子运行库（终态记录收回用例的真相源）。

    只返回 store：Database 句柄由它自己持有，临时目录销毁时一起丢掉
    （与既有持久化用例同一做法，不额外管理连接）。
    """
    from endless_task.storage import Database, SqliteDelegationChildRunRepository

    database = Database(Path(directory) / "coordinator.db")
    database.initialize()
    return SqliteDelegationChildRunRepository(database)


def build_stored_coordinator(store, *, isolated_write_enabled: bool = False):
    """带库的协调器：保留窗口只在有 store 时生效。"""
    return InProcessChildCoordinator(
        ScriptedAgentKernel(),
        parent_run_id="run_parent_1",
        parent_conversation_id="conversation_1",
        parent_capabilities=PARENT_CAPABILITIES,
        parent_trace=trace_context(),
        isolated_write_enabled=isolated_write_enabled,
        child_run_store=store,
    )


def build_queue_coordinator(store, kernel, *, limits: ChildBatchLimits):
    """带库 + 后台任务主管的协调器：``start_child`` 的排队形态两者都要。"""
    from endless_task.runtime.background_tasks import BackgroundTaskSupervisor

    return InProcessChildCoordinator(
        kernel,
        parent_run_id="run_parent_1",
        parent_conversation_id="conversation_1",
        parent_capabilities=PARENT_CAPABILITIES,
        parent_trace=trace_context(),
        child_run_store=store,
        task_supervisor=BackgroundTaskSupervisor(name="test-child-queue"),
        batch_limits=limits,
    )


def run(coro) -> None:
    asyncio.run(coro)


def _child_run_id_for(key: tuple[str, str]) -> str:
    """测试里预判协调器会生成的 child_run_id（与生产同一函数，不是猜）。"""
    from endless_task.delegation.coordinator import _child_run_id_for as derive

    return derive(key)


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

    def test_isolated_workspace_mode_stage_gate_denied_by_default(self) -> None:
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

    def test_isolated_workspace_mode_allowed_when_enabled(self) -> None:
        # M4B P2b-i: with isolated_write_enabled the ISOLATED_SNAPSHOT mode
        # spawns (write-capability/profile expansion for the scratch root
        # arrives with the handler wiring in P2b-ii).
        async def scenario() -> None:
            coordinator = build_coordinator(
                isolated_write_enabled=True,
                allowed_profile_names=frozenset({"subagent_readonly"}),
            )
            spec = child_spec(workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT)
            child_run_id = await coordinator.spawn(spec)
            self.assertTrue(child_run_id)
            self.assertIn(child_run_id, coordinator.tracked_child_run_ids)

        run(scenario())

    def test_isolated_spawn_carries_workspace_to_kernel_command(self) -> None:
        # M4B P2b-ii: spec.child_workspace_id travels to the kernel command
        # metadata so the conversation factory can bind the child scratch.
        from dataclasses import replace

        async def scenario() -> None:
            kernel = ScriptedAgentKernel()
            coordinator = build_coordinator(
                kernel=kernel,
                isolated_write_enabled=True,
                allowed_profile_names=frozenset({"subagent_readonly"}),
            )
            spec = replace(
                child_spec(workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT),
                child_workspace_id="ws_scratch_child",
            )
            await coordinator.spawn(spec)
            self.assertEqual(
                "ws_scratch_child",
                kernel.commands[-1].metadata["child_workspace_id"],
            )

        run(scenario())

    def test_isolated_mode_still_rejected_when_disabled(self) -> None:
        async def scenario() -> None:
            coordinator = build_coordinator()
            spec = child_spec(workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT)
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

    def test_work_profile_child_rejected_at_m4a_profile_gate(self) -> None:
        # DR-1 fixes the child profile to the read-only set: a SpawnSpec
        # carrying a non-read-only profile name is refused before any
        # capability or kernel work happens.
        async def scenario() -> None:
            coordinator = build_coordinator()
            spec = child_spec(
                capabilities=frozenset({"workspace.read"}),
                profile="subagent_workspace",
                workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(spec)
            self.assertEqual("delegation_profile_not_allowed", caught.exception.code)
            self.assertIn("subagent_readonly", caught.exception.details["allowed_profiles"])

        run(scenario())

    def test_explicitly_allowed_work_profile_hits_write_stage_gate(self) -> None:
        # When an operator extends allowed_profile_names to a write profile,
        # the M4A write-capability stage gate still fires: a child whose
        # effective capabilities include write/process/external is refused
        # until M4B isolated-write delegation.
        async def scenario() -> None:
            coordinator = build_coordinator(
                allowed_profile_names=frozenset({"subagent_readonly", "subagent_workspace"})
            )
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

    def test_resolver_profile_layer_narrows_effective_capabilities(self) -> None:
        # With a resolver, effective = parent ∩ requested ∩ resolved profile.
        # The builtin subagent_readonly profile grants only
        # {workspace.read, session.query, memory.read}, so a requested
        # workspace.write must be cut even though the parent holds it.
        async def scenario() -> None:
            registry = create_builtin_profile_registry()
            coordinator = build_coordinator(profile_resolver=registry)
            spec = child_spec(
                capabilities=frozenset(
                    {"workspace.read", "workspace.write", "session.query"}
                ),
                workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            )
            child_run_id = await coordinator.spawn(spec)
            record = next(
                r for r in coordinator.children if r.child_run_id == child_run_id
            )
            self.assertEqual(
                frozenset({"workspace.read", "session.query"}),
                record.decision.effective,
            )
            self.assertNotIn("workspace.write", record.decision.effective)

        run(scenario())

    def test_unknown_profile_name_rejected_by_allowlist_before_resolver(self) -> None:
        # Fail-closed order: a profile name outside the coordinator's allow
        # list is refused without consulting the resolver at all.
        async def scenario() -> None:
            registry = create_builtin_profile_registry()
            coordinator = build_coordinator(profile_resolver=registry)
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec(profile="no_such_profile"))
            self.assertEqual("delegation_profile_not_allowed", caught.exception.code)

        run(scenario())

    def test_resolver_profile_and_forbidden_vocabulary_stay_consistent(self) -> None:
        # Single-vocabulary guard (ADR-AP-2): the read-only forbidden table
        # and the builtin subagent_readonly profile must not disagree, so a
        # child granted the builtin profile can never trip the M4A write gate
        # on the same capability vocabulary.
        registry = create_builtin_profile_registry()
        readonly_profile = registry.resolve("subagent_readonly")
        self.assertTrue(readonly_profile.capabilities.isdisjoint(READ_ONLY_FORBIDDEN))

    def test_empty_allowed_profile_names_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            build_coordinator(allowed_profile_names=frozenset())
        self.assertEqual("invalid_delegation_value", caught.exception.code)

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

    def test_contract_violation_row_uses_the_retained_snapshot_not_the_outcome(
        self,
    ) -> None:
        """契约违规收尾用**按 child_run_id 留存**的快照，绝不用那个不匹配的 outcome。

        不匹配 outcome 的 usage 属于一个我们刚拒绝归属的 run id——把它写进这条子
        运行的账就是错记。可信的只有内核按 child_run_id 留存的那一份，读口正是
        为这条路径存在的。
        """

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            child_run_id = _child_run_id_for(("run_parent_1", "call_1"))
            kernel = SnapshotBackedMismatchKernel(child_run_id)
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
            )
            with self.assertRaises(AgentPlatformError) as caught:
                await coordinator.spawn(child_spec(parent_tool_call_id="call_1"))
            self.assertEqual("delegation_child_run_mismatch", caught.exception.code)

            row = store.get_child_run(child_run_id)
            self.assertEqual(ChildRunState.FAILED, row.state)
            # 留存快照的值（11/7/1/1），不是 outcome 里那个伪造的 999。
            self.assertEqual(11, row.input_tokens)
            self.assertEqual(7, row.output_tokens)
            self.assertEqual(1, row.model_turns)
            self.assertEqual(1, row.tool_calls)
            # 两次读口调用（契约违规分支 + 之后的兜底终结器，后者看到行已终态只
            # 记 debug）都必须按 child_run_id 查；用错 run id 时假内核会立刻抛错。
            self.assertTrue(kernel.snapshot_run_ids)
            self.assertEqual(
                {child_run_id},
                set(kernel.snapshot_run_ids),
                "读口必须按 child_run_id 查询，不是按那个不匹配的 run id",
            )

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))


class ChildRecordIntrospectionTest(unittest.TestCase):
    def test_store_and_protocol_status_maps_are_inverse_of_each_other(self) -> None:
        """两张映射必须互为对方的逆（同域、双射），否则读写库的终态集合会不对称。

        读的一侧（`_CHILD_STATUS_BY_STORE_STATE`）**刻意比写的一侧宽、也刻意是多对
        一**：库里的 `interrupted` 是启动对账直接写的，协议词表里没有它，读的
        时候按失败上报（与 `failed` 同一个协议状态）。所以这里钉的不变式是：

        1. 写进去的每个协议状态都能被读回来（像集包含写域）；
        2. **写的一侧是单射** —— 两个协议状态映射到同一个库状态时"逆"就不存在
           了，而且 `_TERMINAL_CHILD_STATUSES` 会跟着偏。
        """
        from endless_task.delegation.coordinator import (
            _CHILD_STATUS_BY_STORE_STATE,
            _STORE_STATE_BY_STATUS,
        )

        self.assertEqual(
            frozenset(_STORE_STATE_BY_STATUS),
            frozenset(_CHILD_STATUS_BY_STORE_STATE.values()),
        )
        self.assertEqual(
            len(_STORE_STATE_BY_STATUS), len(set(_STORE_STATE_BY_STATUS.values()))
        )

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


class TerminalRecordReclaimTest(unittest.TestCase):
    """终态记录保留窗口：库是真相，内存里只留最近若干条（M3 Task 6）。

    这里的用例刻意区分"有 store"与"没有 store"：没有库时内存是唯一真相，
    保留窗口必须完全不生效，否则 ``query`` 会对老的子运行报 unknown。
    """

    def test_terminal_records_are_reclaimed_beyond_the_window(self) -> None:
        # 三个子运行依次终态、窗口设为 2：最老那条要从内存里被收回。
        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            coordinator = build_stored_coordinator(store)
            # 窗口只在这个实例上收紧，不动类属性（实例属性不跨用例泄漏）。
            coordinator.MAX_RETAINED_TERMINAL_RECORDS = 2
            oldest = await coordinator.spawn(
                child_spec(parent_tool_call_id="call_oldest")
            )
            second = await coordinator.spawn(
                child_spec(parent_tool_call_id="call_second")
            )
            newest = await coordinator.spawn(
                child_spec(parent_tool_call_id="call_newest")
            )

            self.assertEqual(
                (second, newest),
                coordinator.tracked_child_run_ids,
                "保留窗口只该留下最近 2 条终态记录",
            )
            self.assertNotIn(oldest, coordinator.tracked_child_run_ids)
            # 库仍然保留全部三条：内存的窗口不是真相的边界。
            self.assertIsNotNone(store.get_child_run(oldest))

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_reclaimed_child_still_answers_from_the_store(self) -> None:
        # 被收回的子运行在**每一个**公开面上答案都不变：query 走库里的行，
        # cancel/steer 用行状态给出同一个 delegation_child_not_active，
        # 隔离写子运行的 scratch 由 handler 层的库回落兜住。
        async def scenario(directory: str) -> None:
            from dataclasses import replace

            from endless_task.delegation.runtime_handler import (
                CoordinatorDelegationHandler,
            )

            store = _open_store(directory)
            coordinator = build_stored_coordinator(store, isolated_write_enabled=True)
            coordinator.MAX_RETAINED_TERMINAL_RECORDS = 1
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda request: ScriptedAgentKernel(),
                capability_provider=lambda request: PARENT_CAPABILITIES,
                isolated_write_enabled=True,
                child_run_store=store,
            )
            handler._coordinators["run_parent_1"] = coordinator
            try:
                spec = replace(
                    child_spec(
                        parent_tool_call_id="call_isolated",
                        workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT,
                    ),
                    child_workspace_id="ws_scratch_reclaimed",
                )
                reclaimed = await coordinator.spawn(spec)
                # 收回前（内存命中）每个公开面的答案，作为"答案不变"的对照。
                before = await coordinator.query(reclaimed)
                self.assertEqual(
                    "ws_scratch_reclaimed", coordinator.child_workspace_id(reclaimed)
                )
                self.assertIn(reclaimed, coordinator.tracked_child_run_ids)

                # 再派一条：窗口只有 1，上面那条被收回。
                await coordinator.spawn(
                    child_spec(
                        parent_tool_call_id="call_keep",
                        workspace_mode=WorkspaceMode.ISOLATED_SNAPSHOT,
                    )
                )
                self.assertNotIn(reclaimed, coordinator.tracked_child_run_ids)

                after = await coordinator.query(reclaimed)
                self.assertEqual(before.status, after.status)
                self.assertEqual(before.summary, after.summary)
                self.assertEqual(reclaimed, after.child_run_id)

                for surface in (coordinator.cancel, coordinator.steer):
                    with self.assertRaises(AgentPlatformError) as caught:
                        if surface is coordinator.cancel:
                            await surface(reclaimed, "不再需要")
                        else:
                            await surface(reclaimed, "换个方向")
                    self.assertEqual(
                        "delegation_child_not_active", caught.exception.code
                    )

                # 隔离写公开面：协调器内存扫描已经报 unknown，handler 层的
                # 库回落必须给出同一个 scratch workspace id。
                with self.assertRaises(AgentPlatformError) as caught:
                    coordinator.child_workspace_id(reclaimed)
                self.assertEqual("delegation_unknown_child", caught.exception.code)
                self.assertEqual(
                    "ws_scratch_reclaimed",
                    handler.child_workspace_id_for(
                        reclaimed, conversation_id="conversation_1"
                    ),
                )
            finally:
                coordinator.MAX_RETAINED_TERMINAL_RECORDS = 32
                await handler.shutdown()

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_read_only_unfinished_child_keeps_one_error_code_after_reclaim(self) -> None:
        """终审 B-F1：同一子运行在「内存命中」与「库回落」两条路径上必须同序同码。

        只读 + 未完成的子运行：内存路径**先判隔离**（→ ``delegation_child_not_isolated``）。
        库回落若**先判 ``COMPLETED``**，同一个 id 被收回后就会改报
        ``delegation_child_not_completed``，调用方（``apply_child_patches``）会误以为
        「换个时机就能 apply」。两条路径都必须先隔离、后完成。
        """

        async def scenario(directory: str) -> None:
            from endless_task.delegation.runtime_handler import (
                CoordinatorDelegationHandler,
            )

            store = _open_store(directory)
            # 只读子运行跑失败（非完成）：记录此刻还在内存里。
            coordinator = InProcessChildCoordinator(
                ScriptedAgentKernel(status=RunOutcomeStatus.FAILED),
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
            )
            coordinator.MAX_RETAINED_TERMINAL_RECORDS = 1
            handler = CoordinatorDelegationHandler(
                kernel_provider=lambda request: ScriptedAgentKernel(),
                capability_provider=lambda request: PARENT_CAPABILITIES,
                child_run_store=store,
            )
            handler._coordinators["run_parent_1"] = coordinator
            try:
                failed = await coordinator.spawn(
                    child_spec(parent_tool_call_id="call_readonly_failed")
                )
                row = store.get_child_run(failed)
                self.assertEqual(ChildRunState.FAILED, row.state)
                self.assertIsNone(row.child_workspace_id)

                # 内存命中：公开面看到的是内存路径的判定。
                with self.assertRaises(AgentPlatformError) as resident:
                    handler.child_workspace_id_for(failed)
                self.assertEqual(
                    "delegation_child_not_isolated", resident.exception.code
                )

                # 再派一条把上一条挤出保留窗口（窗口 = 1），公开面改走库回落。
                await coordinator.spawn(
                    child_spec(parent_tool_call_id="call_readonly_keep")
                )
                self.assertNotIn(failed, coordinator.tracked_child_run_ids)

                with self.assertRaises(AgentPlatformError) as reclaimed:
                    handler.child_workspace_id_for(failed)
                self.assertEqual(
                    resident.exception.code,
                    reclaimed.exception.code,
                    "同一子运行在内存路径与库回落后必须是同一个错码",
                )
            finally:
                coordinator.MAX_RETAINED_TERMINAL_RECORDS = 32
                await handler.shutdown()

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_records_are_not_reclaimed_without_a_store(self) -> None:
        # 没有库 = 内存是唯一真相：一条都不许丢，否则 query 会对老记录报 unknown。
        async def scenario() -> None:
            coordinator = build_coordinator()
            coordinator.MAX_RETAINED_TERMINAL_RECORDS = 1
            try:
                ids = []
                for call_id in ("call_1", "call_2", "call_3"):
                    ids.append(
                        await coordinator.spawn(
                            child_spec(parent_tool_call_id=call_id)
                        )
                    )

                self.assertEqual(tuple(sorted(ids)), coordinator.tracked_child_run_ids)
                for child_run_id in ids:
                    outcome = await coordinator.query(child_run_id)
                    self.assertEqual(child_run_id, outcome.child_run_id)
            finally:
                coordinator.MAX_RETAINED_TERMINAL_RECORDS = 32

        run(scenario())


class ChildRunEscapeFinaliserTest(unittest.TestCase):
    """兜底终结器：`_run_reserved_locked` 之后任何逃逸都不能把库行留在 running。

    两条真实逃逸口：`_record_for_outcome` 抛非 `AgentPlatformError`（例如 TypeError）、
    `_store_terminal` 写库失败（它**显式**抛出，注释写着"记录丢了必须被看见"）。
    这两种情况下旧实现让异常飞出去、库行永远停在 running（要等下次进程启动对账）。
    """

    def test_record_projection_type_error_still_leaves_a_failed_row(self) -> None:
        """结果投影抛 TypeError：异常照抛，但库行必须已经是 failed。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            coordinator = build_stored_coordinator(store)
            prepared = coordinator.prepare_spawn(child_spec())

            def _explode(**_kwargs):
                raise TypeError("投影炸了（非 AgentPlatformError）")

            with patch.object(coordinator, "_record_for_outcome", _explode):
                with self.assertRaises(TypeError):
                    await coordinator.execute_prepared(prepared)

            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertEqual("delegation_child_crashed", row.error_code)
            self.assertIsNotNone(row.terminal_at)

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_terminal_write_failure_still_leaves_a_failed_row(self) -> None:
        """`_store_terminal` 写库失败：原始异常照抛，库里那一行不能停在 running。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            coordinator = build_stored_coordinator(store)
            prepared = coordinator.prepare_spawn(child_spec())

            def _boom(_record):
                raise RuntimeError("写库炸了（记录丢了必须被看见）")

            with patch.object(coordinator, "_store_terminal", _boom):
                with self.assertRaises(RuntimeError):
                    await coordinator.execute_prepared(prepared)

            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertEqual("delegation_child_crashed", row.error_code)

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_escape_backfills_usage_from_the_retained_snapshot(self) -> None:
        """兜底逃逸收尾也要带用量：内核跑完了、账不能丢在那 0 里。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            kernel = SnapshotKernel()
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
            )
            prepared = coordinator.prepare_spawn(child_spec())

            def _explode(**_kwargs):
                raise ValueError("投影炸了（非 AgentPlatformError）")

            with patch.object(coordinator, "_record_for_outcome", _explode):
                with self.assertRaises(ValueError):
                    await coordinator.execute_prepared(prepared)

            row = store.get_child_run(prepared.child_run_id)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertEqual(11, row.input_tokens)
            self.assertEqual(7, row.output_tokens)
            self.assertEqual(1, row.model_turns)
            self.assertEqual(2, row.tool_calls)

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_raising_snapshot_accessor_never_blocks_finalisation(self) -> None:
        """读口抛异常的外来内核：收尾照常落终态，库行绝不允许停在 running。

        `_best_available_usage` 必须有自己的 try/except：在 `_mark_child_cancelled`
        / `_mark_child_failed_after_escape` 里，一个在写库**之前**抛出的读口会中断
        整个收尾（外层只记日志），把行永久留在 running。

        本用例走的是 `_failed_record` 那个调用点（内核先抛 → 失败记录）；
        另两个被点名的终结器由 `test_raising_accessor_never_blocks_the_finalisers`
        直接钉住。
        """

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            kernel = ExplodingSnapshotKernel()
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
            )
            child_run_id = await coordinator.spawn(child_spec())

            # 读口确实被调用过（否则这条用例证明不了"抛异常也不中断"）。
            self.assertEqual([child_run_id], kernel.snapshot_run_ids)
            row = store.get_child_run(child_run_id)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertIsNotNone(row.terminal_at)
            # 拿不到快照就落 0（诚实），绝不编造。
            self.assertEqual(0, row.input_tokens)
            self.assertEqual(0, row.output_tokens)
            self.assertEqual(0, row.model_turns)
            self.assertEqual(0, row.tool_calls)

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_raising_accessor_never_blocks_the_finalisers(self) -> None:
        """读口抛异常时，两个收尾终结器自己也不许中断（E-2 的覆盖缺口）。

        上一条用例只覆盖 `_failed_record` 调用点，但它的 docstring 点名的危险面是
        `_mark_child_cancelled` / `_mark_child_failed_after_escape`——这里直接调它们
        （预留后即调，与评审探针同一路线），断言"没抛异常 + 行是终态 + 四列 0"。
        """

        async def scenario(directory: str, finaliser: str) -> None:
            store = _open_store(directory)
            kernel = ExplodingSnapshotKernel()
            coordinator = InProcessChildCoordinator(
                kernel,
                parent_run_id="run_parent_1",
                parent_conversation_id="conversation_1",
                parent_capabilities=PARENT_CAPABILITIES,
                parent_trace=trace_context(),
                child_run_store=store,
            )
            prepared = coordinator.prepare_spawn(child_spec())
            coordinator._reserve(prepared)

            raised: list[BaseException] = []
            try:
                getattr(coordinator, finaliser)(prepared.child_run_id)
            except BaseException as error:  # noqa: BLE001 - 收尾抛异常就是缺陷
                raised.append(error)

            self.assertEqual([], raised, "读口抛异常不得让收尾抛出")
            self.assertEqual([prepared.child_run_id], kernel.snapshot_run_ids)
            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(
                ChildRunState.CANCELLED
                if finaliser == "_mark_child_cancelled"
                else ChildRunState.FAILED,
                row.state,
            )
            self.assertIsNotNone(row.terminal_at)
            self.assertEqual(
                (0, 0, 0, 0),
                (row.input_tokens, row.output_tokens, row.model_turns, row.tool_calls),
            )

        for finaliser in ("_mark_child_cancelled", "_mark_child_failed_after_escape"):
            with self.subTest(finaliser=finaliser):
                with tempfile.TemporaryDirectory() as directory:
                    run(scenario(directory, finaliser))

    def test_fallback_finaliser_never_overwrites_a_benign_terminal_row(self) -> None:
        """兜底收尾是幂等的：行已经终态（良性竞态）时只记 debug，不改写它。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            coordinator = build_stored_coordinator(store)
            prepared = coordinator.prepare_spawn(child_spec())

            def _explode(**_kwargs):
                raise TypeError("投影炸了")

            # 先制造"另一个写者已经把行收尾"的良性竞态，再让投影炸掉走兜底路径：
            # 兜底看到 InvalidStateError（行已终态）时只能记 debug，不许覆盖。
            # `_reserve` 被旁路成 no-op（行与 spawn key 都已由"另一个写者"处理）。
            coordinator._reserve(prepared)
            store.mark_child_terminal(
                prepared.child_run_id,
                state=ChildRunState.COMPLETED,
                summary="先跑完了",
            )

            with patch.object(coordinator, "_reserve", lambda _prepared: None):
                with patch.object(coordinator, "_record_for_outcome", _explode):
                    with self.assertRaises(TypeError):
                        await coordinator.execute_prepared(prepared)

            row = store.get_child_run(prepared.child_run_id)
            self.assertEqual(ChildRunState.COMPLETED, row.state)
            self.assertEqual("先跑完了", row.summary)
            self.assertIsNone(row.error_code)

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))


class MalformedSnapshotGuardTest(unittest.TestCase):
    """读口**返回**畸形值：收尾照常落终态，四列落 0（绝不编造）。

    三个调用点共用 `_best_available_usage`，逐个钉住：
      * `_mark_child_cancelled`（取消 / 关停 / 排队回落）；
      * `_mark_child_failed_after_escape`（兜底逃逸终结器）；
      * `_failed_record`（内核失败后的失败记录，`_run_reserved_body` 直接调用）。

    `_store_terminal` 传的是已经映射好的 `outcome.usage`，不经过这个 helper，
    因此不在这里覆盖（它由既有回填用例覆盖）。畸形形状至少三种：dict 形状、
    短字符串、负数字段。
    """

    def _coordinator(self, store, kernel) -> InProcessChildCoordinator:
        return InProcessChildCoordinator(
            kernel,
            parent_run_id="run_parent_1",
            parent_conversation_id="conversation_1",
            parent_capabilities=PARENT_CAPABILITIES,
            parent_trace=trace_context(),
            child_run_store=store,
        )

    def _assert_zero_columns(self, row) -> None:
        self.assertEqual(
            (0, 0, 0, 0),
            (row.input_tokens, row.output_tokens, row.model_turns, row.tool_calls),
        )

    def test_malformed_snapshot_does_not_break_the_cancel_finaliser(self) -> None:
        async def scenario(directory: str, snapshot: object) -> None:
            store = _open_store(directory)
            kernel = MalformedSnapshotKernel(snapshot)
            coordinator = self._coordinator(store, kernel)
            prepared = coordinator.prepare_spawn(child_spec())
            coordinator._reserve(prepared)

            raised: list[BaseException] = []
            try:
                coordinator._mark_child_cancelled(prepared.child_run_id)
            except BaseException as error:  # noqa: BLE001 - 收尾抛异常就是缺陷
                raised.append(error)

            self.assertEqual([], raised, "畸形快照不得让取消收尾抛出")
            self.assertEqual([prepared.child_run_id], kernel.snapshot_run_ids)
            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(ChildRunState.CANCELLED, row.state)
            self.assertIsNotNone(row.terminal_at)
            self._assert_zero_columns(row)

        for name, snapshot in _malformed_snapshots():
            with self.subTest(kind=name):
                with tempfile.TemporaryDirectory() as directory:
                    run(scenario(directory, snapshot))

    def test_malformed_snapshot_does_not_break_the_escape_finaliser(self) -> None:
        async def scenario(directory: str, snapshot: object) -> None:
            store = _open_store(directory)
            kernel = MalformedSnapshotKernel(snapshot)
            coordinator = self._coordinator(store, kernel)
            prepared = coordinator.prepare_spawn(child_spec())
            coordinator._reserve(prepared)

            raised: list[BaseException] = []
            try:
                coordinator._mark_child_failed_after_escape(prepared.child_run_id)
            except BaseException as error:  # noqa: BLE001 - 收尾抛异常就是缺陷
                raised.append(error)

            self.assertEqual([], raised, "畸形快照不得让兜底收尾抛出")
            self.assertEqual([prepared.child_run_id], kernel.snapshot_run_ids)
            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertEqual("delegation_child_crashed", row.error_code)
            self.assertIsNotNone(row.terminal_at)
            self._assert_zero_columns(row)

        for name, snapshot in _malformed_snapshots():
            with self.subTest(kind=name):
                with tempfile.TemporaryDirectory() as directory:
                    run(scenario(directory, snapshot))

    def test_malformed_snapshot_does_not_break_the_failed_record_path(self) -> None:
        """内核失败走 `_failed_record`：畸形返回同样只降级成 0，行必须落 failed。"""

        async def scenario(directory: str, snapshot: object) -> None:
            store = _open_store(directory)
            kernel = MalformedSnapshotKernel(snapshot)
            coordinator = self._coordinator(store, kernel)
            prepared = coordinator.prepare_spawn(child_spec())

            raised: list[BaseException] = []
            try:
                await coordinator.execute_prepared(prepared)
            except BaseException as error:  # noqa: BLE001 - 收尾抛异常就是缺陷
                raised.append(error)

            self.assertEqual([], raised, "畸形快照不得让失败记录抛出")
            self.assertEqual([prepared.child_run_id], kernel.snapshot_run_ids)
            row = store.get_child_run(prepared.child_run_id)
            self.assertIsNotNone(row)
            self.assertEqual(ChildRunState.FAILED, row.state)
            self.assertEqual("child_kernel_error", row.error_code)
            self.assertIsNotNone(row.terminal_at)
            self._assert_zero_columns(row)

        for name, snapshot in _malformed_snapshots():
            with self.subTest(kind=name):
                with tempfile.TemporaryDirectory() as directory:
                    run(scenario(directory, snapshot))


class ChildAdmissionQueueTest(unittest.TestCase):
    """在册容量从"拒绝"改成"排队"（M3 Task 7）。

    闸门含义的分界：在册名额（``max_children_per_parent``）之外的子运行在
    **admission 闸门外排队**，库行的形态是 ``running`` ∧ ``started_at IS NULL``
    ——正是 wire 早就定义好的 ``queued``，所以 wire/前端零改动。绝对上限
    （``max_queued_children``）仍然拦得住无界排队。
    """

    def test_over_capacity_children_queue_instead_of_failing(self) -> None:
        """在册容量 1、绝对上限 4：第 2/3 个排队，三次调用都立刻拿到句柄。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            kernel = GatedEchoKernel()
            coordinator = build_queue_coordinator(
                store,
                kernel,
                limits=ChildBatchLimits(
                    max_concurrent_children=1,
                    max_children_per_parent=1,
                    max_queued_children=4,
                ),
            )

            handles = [
                coordinator.start(child_spec(parent_tool_call_id=f"call_{index}"))
                for index in range(3)
            ]

            # ① 三次调用都没抛，各自拿到句柄（在册 1/1，后两个排队）。
            self.assertEqual(3, len(set(handles)))
            self.assertTrue(all(handle.startswith("child_") for handle in handles))
            self.assertEqual(3, coordinator.inflight_child_count())

            await kernel.started.wait()

            # ② 只有第一个进了内核；另外两个停在 admission 闸门外。
            self.assertEqual(1, len(kernel.commands))
            rows = {
                row.child_run_id: row
                for row in store.list_child_runs("run_parent_1")
            }
            self.assertEqual(3, len(rows))
            started = [
                row for row in rows.values() if row.started_at is not None
            ]
            queued = [
                row
                for row in rows.values()
                if row.state is ChildRunState.RUNNING and row.started_at is None
            ]
            self.assertEqual(1, len(started))
            self.assertEqual(2, len(queued))

            kernel.release.set()
            await coordinator.drain()

            after = store.list_child_runs("run_parent_1")
            self.assertTrue(all(row.terminal for row in after))
            self.assertTrue(
                all(row.state is ChildRunState.COMPLETED for row in after)
            )
            self.assertEqual(3, len(kernel.commands))

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_admission_gate_binds_before_the_concurrency_gate(self) -> None:
        """在册容量(2) 严于并发容量(1)：第 3 个子运行必须卡在 **admission** 闸门。

        为什么需要这条：既有的排队用例（在册 1 / 并发 1）两个闸门容量相同，
        删掉 admission 闸门后并发闸门能顶住同样的断言——它钉不住"闸门存在"。
        这里刻意让两个容量不同（admission 2 > 并发 1），并数**进入并发闸门**
        （``_child_slots()``）的子运行数：admission 在时最多 2 个能走到并发闸门
        前（1 个在内核里、1 个在等并发名额），admission 被删掉时三个子运行会
        一起涌向并发闸门（3）。这就是在册名额独立存在的意义：它是"已预留、未
        终态"的边界，而不是"同时在跑"的边界——少了它，排队人数只剩绝对上限
        一道墙。
        """

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            kernel = GatedEchoKernel()
            limits = ChildBatchLimits(
                max_concurrent_children=1,
                max_children_per_parent=2,
                max_queued_children=4,
            )
            coordinator = build_queue_coordinator(store, kernel, limits=limits)

            into_concurrency_gate: list[int] = []
            original_child_slots = coordinator._child_slots

            def counting_child_slots():
                into_concurrency_gate.append(len(into_concurrency_gate) + 1)
                return original_child_slots()

            with patch.object(coordinator, "_child_slots", counting_child_slots):
                handles = [
                    coordinator.start(
                        child_spec(parent_tool_call_id=f"call_{index}")
                    )
                    for index in range(3)
                ]
                self.assertEqual(3, len(set(handles)))
                self.assertEqual(3, coordinator.inflight_child_count())

                await kernel.started.wait()
                # 每个后台任务只在阻塞前走一个事件循环步；多让几轮保证三个
                # 任务都已经走到各自的阻塞点（不是"还没来得及数"）。
                for _ in range(5):
                    await asyncio.sleep(0)

                # ① 只有 1 个进了内核；② 进入并发闸门的恰好是 admission 容量。
                self.assertEqual(1, len(kernel.commands))
                self.assertEqual(
                    2,
                    len(into_concurrency_gate),
                    "admission 闸门必须先把进入并发闸门的子运行数限到 "
                    "max_children_per_parent(2)",
                )
                rows = {
                    row.child_run_id: row
                    for row in store.list_child_runs("run_parent_1")
                }
                self.assertEqual(3, len(rows))
                started = [row for row in rows.values() if row.started_at is not None]
                queued = [
                    row
                    for row in rows.values()
                    if row.state is ChildRunState.RUNNING and row.started_at is None
                ]
                self.assertEqual(1, len(started))
                self.assertEqual(2, len(queued))

                kernel.release.set()
                await coordinator.drain()

            # 放行后每个子运行都只过一次并发闸门：三个都完整跑完。
            self.assertEqual(3, len(into_concurrency_gate))
            after = store.list_child_runs("run_parent_1")
            self.assertTrue(all(row.terminal for row in after))
            self.assertEqual(3, len(kernel.commands))

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))

    def test_absolute_queue_cap_still_rejects(self) -> None:
        """绝对上限到顶时仍然拒绝：排队不能退化成无界队列。"""

        async def scenario(directory: str) -> None:
            store = _open_store(directory)
            kernel = GatedEchoKernel()
            coordinator = build_queue_coordinator(
                store,
                kernel,
                limits=ChildBatchLimits(
                    max_concurrent_children=1,
                    max_children_per_parent=2,
                    max_queued_children=2,
                ),
            )
            coordinator.start(child_spec(parent_tool_call_id="call_1"))
            coordinator.start(child_spec(parent_tool_call_id="call_2"))

            with self.assertRaises(AgentPlatformError) as caught:
                coordinator.start(child_spec(parent_tool_call_id="call_3"))

            self.assertEqual(
                "delegation_children_limit_exceeded", caught.exception.code
            )
            self.assertEqual(2, caught.exception.details["max_queued_children"])
            self.assertEqual(2, caught.exception.details["tracked"])

            kernel.release.set()
            await coordinator.drain()

        with tempfile.TemporaryDirectory() as directory:
            run(scenario(directory))


if __name__ == "__main__":
    unittest.main()
