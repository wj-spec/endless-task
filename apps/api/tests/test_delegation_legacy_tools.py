"""M4A DR-2 slice 2: v1-form delegation tools over the real handler."""

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
from endless_task.delegation.legacy_tools import (
    CancelAgentLegacyTool,
    QueryAgentLegacyTool,
    SpawnAgentLegacyTool,
)
from endless_task.delegation.runtime_handler import CoordinatorDelegationHandler
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolRegistry,
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
    def __init__(self) -> None:
        self.commands: list[RunCommand] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
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


def tool_call(
    tool_name: str,
    arguments: dict,
    *,
    call_id: str = "call_1",
    run_id: str = "run_parent_1",
) -> ToolCall:
    return ToolCall(
        id=call_id,
        conversation_id="conversation_1",
        turn_id="turn_1",
        response_variant_id=run_id,  # v2 runtime carries the run id here
        tool_name=tool_name,
        arguments=arguments,
        status=ToolCallStatus.RUNNING,
        created_at="2026-09-03T00:00:00Z",
    )


class LegacyDelegationToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kernel = EchoAgentKernel()
        self.handler = CoordinatorDelegationHandler(
            kernel_provider=lambda req: self.kernel,
            capability_provider=lambda req: PARENT_CAPABILITIES,
        )
        self.tools: list[RegisteredTool] = [
            SpawnAgentLegacyTool(self.handler),
            QueryAgentLegacyTool(self.handler),
            CancelAgentLegacyTool(self.handler),
        ]
        self.registry = ToolRegistry()
        for tool in self.tools:
            self.registry.register(tool)

    def _execute(self, call: ToolCall):
        async def run():
            tool = self.registry.resolve(call.tool_name)
            return await tool.execute(call, CancellationToken())

        return asyncio.run(run())

    def test_tools_register_with_v1_definitions(self) -> None:
        names = {tool.definition.name for tool in self.tools}
        self.assertEqual(
            {"spawn_agent", "query_agent", "cancel_agent"},
            names,
        )
        for tool in self.tools:
            self.assertEqual(
                ToolApprovalMode.AUTO,
                tool.definition.approval_mode,
            )
            self.assertEqual("read_only", tool.definition.effect.value)

    def test_spawn_via_v1_call_returns_handle(self) -> None:
        result = self._execute(
            tool_call("spawn_agent", {"task": "调查模块 A"})
        )
        self.assertIsNone(result.error)
        payload = result.structured_content
        self.assertTrue(payload["childRunId"].startswith("child_"))
        self.assertEqual("completed", payload["status"])
        self.assertEqual("run_parent_1", self.kernel.commands[0].metadata["delegation"]["parent_run_id"])

    def test_spawn_then_query_round_trip(self) -> None:
        spawned = self._execute(
            tool_call("spawn_agent", {"task": "调查模块 B"})
        )
        child_run_id = spawned.structured_content["childRunId"]
        queried = self._execute(
            tool_call("query_agent", {"childRunId": child_run_id}, call_id="call_2")
        )
        self.assertIsNone(queried.error)
        self.assertEqual("completed", queried.structured_content["status"])
        self.assertIn("调查完成", queried.structured_content["summary"])

    def test_invalid_task_argument_surfaces_as_tool_error(self) -> None:
        result = self._execute(tool_call("spawn_agent", {"task": ""}))
        self.assertIsNotNone(result.error)
        self.assertEqual("invalid_delegation_task", result.error.code)

    def test_query_unknown_child_surfaces_tool_error(self) -> None:
        result = self._execute(
            tool_call("query_agent", {"childRunId": "child_missing"})
        )
        self.assertIsNotNone(result.error)
        self.assertEqual("delegation_unknown_child", result.error.code)

    def test_cancel_terminal_child_is_benign(self) -> None:
        spawned = self._execute(
            tool_call("spawn_agent", {"task": "任务"})
        )
        child_run_id = spawned.structured_content["childRunId"]
        result = self._execute(
            tool_call(
                "cancel_agent",
                {"childRunId": child_run_id, "reason": "不需要了"},
                call_id="call_3",
            )
        )
        self.assertIsNone(result.error)
        self.assertEqual("already_terminal", result.structured_content["status"])

    def test_tool_name_mismatch_raises_validation_error(self) -> None:
        from endless_task.tooling import ToolValidationError

        tool = self.registry.resolve("spawn_agent")
        with self.assertRaises(ToolValidationError):
            asyncio.run(
                tool.execute(
                    tool_call("query_agent", {"task": "x"}),
                    CancellationToken(),
                )
            )


if __name__ == "__main__":
    unittest.main()
