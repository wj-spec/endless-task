"""M4A DR-2 slice 2: v1 注册形状的委派工具（薄 shim 包原生 v2 类）over the real handler.

切片 9 第四批（Task 3）起，生产注册对象是
:class:`~endless_task.delegation.legacy_tools.V2DelegationToolAsRegistered`（v1
``RegisteredTool`` 形状），内层是 ``delegation/agent_tools.py`` 的原生 v2 类；五个 v1
``*LegacyTool`` 类已退役。本模块的行为断言因此一律经注册对象/shim 走——契约（v1 注册
形状、策略、委派到 handler、往返、非法参数、未知子运行、终态取消、名字不匹配）不变。
"""

from __future__ import annotations

import asyncio
import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.delegation import (
    CancelAgentTool,
    ListAgentsTool,
    QueryAgentTool,
    SpawnAgentTool,
    SteerAgentTool,
    V2DelegationToolAsRegistered,
)
from endless_task.delegation.runtime_handler import CoordinatorDelegationHandler
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import (
    ToolOutcome,
    ToolOutcomeStatus,
)
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
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

#: 五个原生 v2 类（Task 3 之后生产唯一被包围/执行的定义源）。
V2_TOOL_CLASSES = {
    "spawn_agent": SpawnAgentTool,
    "query_agent": QueryAgentTool,
    "cancel_agent": CancelAgentTool,
    "list_agents": ListAgentsTool,
    "steer_agent": SteerAgentTool,
}


def registered_tool(tool_name: str, handler) -> V2DelegationToolAsRegistered:
    """生产形状的注册对象：原生 v2 类外包一层 v1 形状的 shim。"""
    return V2DelegationToolAsRegistered(V2_TOOL_CLASSES[tool_name](handler=handler))


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


class _FakeHandler:
    """只记录调用的假 handler：钉住工具面的形状与委托，不牵进真协调器。"""

    def __init__(self) -> None:
        self.list_requests: list[dict] = []
        self.steer_requests: list[dict] = []

    async def list_children(self, request):
        self.list_requests.append(dict(request.arguments))
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
            structured_content={"children": [], "total": 0},
        )

    async def steer_child(self, request):
        self.steer_requests.append(dict(request.arguments))
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
            structured_content={
                "childRunId": request.arguments.get("childRunId"),
                "status": "steering",
            },
        )


def execute_tool(tool: RegisteredTool, call: ToolCall):
    async def run():
        return await tool.execute(call, CancellationToken())

    return asyncio.run(run())


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
            registered_tool(tool_name, self.handler)
            for tool_name in V2_TOOL_CLASSES
        ]
        self.registry = ToolRegistry()
        for tool in self.tools:
            self.registry.register(tool)

    def _execute(self, call: ToolCall):
        return execute_tool(self.registry.resolve(call.tool_name), call)

    def test_tools_register_with_v1_definitions(self) -> None:
        names = {tool.definition.name for tool in self.tools}
        self.assertEqual(
            {
                "spawn_agent",
                "query_agent",
                "cancel_agent",
                "list_agents",
                "steer_agent",
            },
            names,
        )
        for tool in self.tools:
            self.assertEqual(
                ToolApprovalMode.AUTO,
                tool.definition.approval_mode,
            )
            self.assertEqual("read_only", tool.definition.effect.value)

    def test_list_and_steer_are_registered_with_delegation_policy(self) -> None:
        handler = _FakeHandler()
        tools = (
            registered_tool("list_agents", handler),
            registered_tool("steer_agent", handler),
        )
        self.assertEqual(
            ("list_agents", "steer_agent"),
            tuple(tool.definition.name for tool in tools),
        )
        for tool in tools:
            self.assertEqual(ToolEffect.READ_ONLY, tool.definition.effect)
            self.assertEqual(ToolApprovalMode.AUTO, tool.definition.approval_mode)
            self.assertIs(
                False, tool.definition.input_schema["additionalProperties"]
            )
        # list_agents 无入参；steer_agent 两字段必填且受长度约束。
        self.assertEqual([], list(tools[0].definition.input_schema["required"]))
        self.assertEqual(
            {"childRunId", "message"},
            set(tools[1].definition.input_schema["required"]),
        )
        properties = tools[1].definition.input_schema["properties"]
        self.assertEqual(1, properties["childRunId"]["minLength"])
        self.assertEqual(256, properties["childRunId"]["maxLength"])
        self.assertEqual(1, properties["message"]["minLength"])
        self.assertEqual(2_000, properties["message"]["maxLength"])
        # 验收（M2-2④）：描述必须写清"只允许一句话纠偏，改任务请 cancel + spawn"。
        description = tools[1].definition.description
        self.assertIn("cancel_agent", description)
        self.assertIn("spawn_agent", description)

    def test_list_agents_delegates_to_handler(self) -> None:
        handler = _FakeHandler()
        tool = registered_tool("list_agents", handler)
        result = execute_tool(tool, tool_call("list_agents", {}))
        self.assertIsNone(result.error)
        self.assertEqual(1, len(handler.list_requests))
        self.assertEqual({"children": [], "total": 0}, result.structured_content)

    def test_steer_agent_delegates_to_handler(self) -> None:
        handler = _FakeHandler()
        tool = registered_tool("steer_agent", handler)
        result = execute_tool(
            tool,
            tool_call(
                "steer_agent",
                {"childRunId": "child_a", "message": "换个方向"},
            ),
        )
        self.assertIsNone(result.error)
        self.assertEqual(1, len(handler.steer_requests))
        self.assertEqual("child_a", handler.steer_requests[0]["childRunId"])
        self.assertEqual("换个方向", handler.steer_requests[0]["message"])
        self.assertEqual("steering", result.structured_content["status"])

    def test_list_and_steer_require_a_capable_handler(self) -> None:
        for tool_name in ("list_agents", "steer_agent"):
            with self.assertRaises(AgentPlatformError) as caught:
                registered_tool(tool_name, object())
            self.assertEqual("invalid_delegation_handler", caught.exception.code)
            self.assertIn(tool_name, caught.exception.safe_message)

    def test_spawn_via_v1_call_returns_handle(self) -> None:
        result = self._execute(
            tool_call("spawn_agent", {"task": "调查模块 A"})
        )
        self.assertIsNone(result.error)
        payload = result.structured_content
        self.assertTrue(payload["childRunId"].startswith("child_"))
        # 异步契约：返回值只是句柄，不代表子运行已经结束。
        self.assertEqual("running", payload["status"])
        self.assertEqual("run_parent_1", self.kernel.commands[0].metadata["delegation"]["parent_run_id"])
        asyncio.run(self.handler.drain())
        queried = self._execute(
            tool_call(
                "query_agent",
                {"childRunId": payload["childRunId"]},
                call_id="call_2",
            )
        )
        self.assertEqual("completed", queried.structured_content["status"])
        self.assertIn("调查完成", queried.structured_content["summary"])

    def test_spawn_then_query_round_trip(self) -> None:
        spawned = self._execute(
            tool_call("spawn_agent", {"task": "调查模块 B"})
        )
        child_run_id = spawned.structured_content["childRunId"]
        asyncio.run(self.handler.drain())
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
        asyncio.run(self.handler.drain())
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
