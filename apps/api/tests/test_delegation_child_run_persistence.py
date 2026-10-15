"""协调器写库：跨轮次、跨进程重启仍能回答子运行状态。"""

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.delegation import (
    ChildStatus,
    InProcessChildCoordinator,
    SpawnSpec,
)
from endless_task.delegation.child_run_store import ChildRunState
from endless_task.runtime_ledger import TraceContext
from endless_task.storage import Database, SqliteDelegationChildRunRepository


class _CompletingKernel:
    def __init__(self, text: str = "子运行结论") -> None:
        self.text = text
        self.commands = []

    async def run(self, command):
        self.commands.append(command)
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT, content=self.text
            ),
        )


def _spec(parent_run_id: str = "run_parent", call_id: str = "call_1"):
    from endless_task.delegation import (
        ChildContextPolicy,
        ChildModelPolicy,
        ChildOutputSchema,
        WorkspaceMode,
    )

    return SpawnSpec(
        task="总结仓库测试现状",
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


_PARENT_CAPABILITIES = frozenset({"workspace.read", "session.query"})


class CoordinatorPersistenceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "d.db")
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
        }
        payload.update(overrides)
        return InProcessChildCoordinator(kernel, **payload)

    async def test_spawn_writes_running_then_terminal_row(self) -> None:
        kernel = _CompletingKernel()
        coordinator = self._coordinator(kernel)
        child_run_id = await coordinator.spawn(_spec())
        row = self.store.get_child_run(child_run_id)
        self.assertEqual(ChildRunState.COMPLETED, row.state)
        self.assertEqual("子运行结论", row.summary)
        self.assertEqual("run_parent", row.parent_run_id)
        self.assertEqual("conv_parent", row.parent_conversation_id)
        # 内核不带用量（老代码/假内核）时四列保持 0，不因为回填改动而回归。
        self.assertEqual((0, 0, 0, 0), (
            row.input_tokens,
            row.output_tokens,
            row.model_turns,
            row.tool_calls,
        ))

    async def test_query_answers_from_store_after_restart(self) -> None:
        kernel = _CompletingKernel(text="重启前完成的结论")
        coordinator = self._coordinator(kernel)
        child_run_id = await coordinator.spawn(_spec())

        # 模拟进程重启：新的协调器（内存里没有这条子运行），同一份库。
        restarted = self._coordinator(_CompletingKernel())
        outcome = await restarted.query(child_run_id)
        self.assertEqual(ChildStatus.COMPLETED, outcome.status)
        self.assertEqual("重启前完成的结论", outcome.summary)
        self.assertEqual(child_run_id, outcome.child_run_id)

    async def test_query_reports_running_child(self) -> None:
        from endless_task.agent_platform import AgentPlatformError

        coordinator = self._coordinator(_CompletingKernel())
        # 直接经 store 造一行"在跑"的子运行：本任务还没有异步派发，
        # 真实在跑的子运行由后续任务（异步派发）的用例覆盖。
        self.store.reserve_child_run(
            child_run_id="child_running",
            parent_run_id="run_parent",
            parent_tool_call_id="call_running",
            parent_conversation_id="conv_parent",
            depth=1,
            capability_profile="subagent_readonly",
            workspace_mode="read_only_shared",
            timeout_seconds=120.0,
            task="还在跑",
        )

        with self.assertRaises(AgentPlatformError) as caught:
            await coordinator.query("child_running")

        self.assertEqual("delegation_child_running", caught.exception.code)

    async def test_duplicate_spawn_key_is_refused_after_restart(self) -> None:
        from endless_task.agent_platform import AgentPlatformError

        first = self._coordinator(_CompletingKernel())
        await first.spawn(_spec())
        restarted = self._coordinator(_CompletingKernel())
        with self.assertRaises(AgentPlatformError) as caught:
            await restarted.spawn(_spec())
        self.assertEqual("delegation_duplicate_spawn", caught.exception.code)
