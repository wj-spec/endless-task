"""M4A DR-2 slice 1: delegation tools over the coordinator-backed handler."""

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
from endless_task.delegation.agent_tools import (
    CancelAgentTool,
    QueryAgentTool,
    SpawnAgentTool,
)
from endless_task.delegation.runtime_handler import CoordinatorDelegationHandler
from endless_task.runtime.cancellation import CancellationToken
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


def request(tool_name: str, arguments: dict) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id="call_parent_1",
        tool_name=tool_name,
        arguments=arguments,
        conversation_id="conversation_1",
        run_id="run_parent_1",
        model_turn_id="turn_1",
        correlation_id="corr_1",
        cancellation=CancellationToken(),
        created_at="2026-09-03T00:00:00Z",
    )


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
        self.assertEqual("completed", spawn.structured_content["status"])
        lineage = self.kernel.commands[0].metadata["delegation"]
        self.assertEqual("run_parent_1", lineage["parent_run_id"])
        self.assertEqual("call_parent_1", lineage["parent_tool_call_id"])

        query = asyncio.run(
            self.query_tool.execute(
                request("query_agent", {"childRunId": child_run_id})
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, query.status)
        self.assertEqual("completed", query.structured_content["status"])
        self.assertIn("调查完成", query.structured_content["summary"])

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


if __name__ == "__main__":
    unittest.main()
