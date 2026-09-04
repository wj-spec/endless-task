"""M4A DR-2 slice 1: delegation agent tools (schema + handler bridge)."""

from __future__ import annotations

import unittest

from endless_task.delegation.agent_tools import (
    CANCEL_AGENT_NAME,
    QUERY_AGENT_NAME,
    SPAWN_AGENT_NAME,
    CancelAgentTool,
    DelegationToolHandler,
    QueryAgentTool,
    SpawnAgentTool,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import (
    ToolExecutionRequest,
    ToolOutcomeStatus,
)


class _FakeHandler:
    """Handler recording calls and returning scripted outcomes."""

    def __init__(self) -> None:
        self.spawn_requests: list[ToolExecutionRequest] = []
        self.query_requests: list[ToolExecutionRequest] = []
        self.cancel_requests: list[ToolExecutionRequest] = []
        self.spawn_outcome = _completed("call_1", "spawned")
        self.query_outcome = _completed("call_1", "queried")
        self.cancel_outcome = _completed("call_1", "cancelled")

    async def spawn_child(self, request: ToolExecutionRequest):
        self.spawn_requests.append(request)
        return self.spawn_outcome

    async def query_child(self, request: ToolExecutionRequest):
        self.query_requests.append(request)
        return self.query_outcome

    async def cancel_child(self, request: ToolExecutionRequest):
        self.cancel_requests.append(request)
        return self.cancel_outcome


def _request(tool_name: str, arguments: dict) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id="call_1",
        tool_name=tool_name,
        arguments=arguments,
        conversation_id="conversation_1",
        run_id="run_parent_1",
        model_turn_id="turn_1",
        correlation_id="corr_1",
        cancellation=CancellationToken(),
        created_at="2026-09-03T00:00:00Z",
    )


def _completed(call_id: str, content: str = "ok") -> ToolOutcome:
    from endless_task.tool_platform import ToolOutcome

    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.COMPLETED,
        content=content,
    )


class SpawnAgentToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _FakeHandler()
        self.tool = SpawnAgentTool(handler=self.handler)

    def test_definition_shape(self) -> None:
        self.assertEqual(SPAWN_AGENT_NAME, self.tool.definition.name)
        self.assertEqual("read_only", self.tool.definition.effect.value)
        self.assertEqual("object", self.tool.definition.input_schema["type"])
        self.assertIn("task", self.tool.definition.input_schema["required"])

    def test_valid_task_delegates_to_handler(self) -> None:
        outcome = self._run(
            {"task": "调查模块 A", "timeoutSeconds": 60}
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual(1, len(self.handler.spawn_requests))
        self.assertEqual("run_parent_1", self.handler.spawn_requests[0].run_id)

    def test_missing_task_rejected_before_handler(self) -> None:
        outcome = self._run({})
        self.assertEqual(ToolOutcomeStatus.FAILED, outcome.status)
        self.assertEqual("invalid_delegation_task", outcome.diagnostic.code)
        self.assertEqual(0, len(self.handler.spawn_requests))

    def test_empty_task_rejected(self) -> None:
        outcome = self._run({"task": "   "})
        self.assertEqual("invalid_delegation_task", outcome.diagnostic.code)

    def test_invalid_timeout_rejected(self) -> None:
        outcome = self._run({"task": "x", "timeoutSeconds": -1})
        self.assertEqual("invalid_delegation_timeout", outcome.diagnostic.code)

    def test_non_object_schema_rejected(self) -> None:
        outcome = self._run({"task": "x", "expectedOutputSchema": "not-an-object"})
        self.assertEqual(
            "invalid_expected_output_schema", outcome.diagnostic.code
        )

    def _run(self, arguments: dict):
        return _run_async(self.tool.execute(_request(SPAWN_AGENT_NAME, arguments)))


class QueryAgentToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _FakeHandler()
        self.handler.query_outcome = _completed("call_1", "child done")
        self.tool = QueryAgentTool(handler=self.handler)

    def test_definition_requires_child_run_id(self) -> None:
        self.assertEqual(QUERY_AGENT_NAME, self.tool.definition.name)
        self.assertIn("childRunId", self.tool.definition.input_schema["required"])

    def test_valid_query_delegates(self) -> None:
        outcome = _run_async(
            self.tool.execute(
                _request(QUERY_AGENT_NAME, {"childRunId": "child_x"})
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual(1, len(self.handler.query_requests))

    def test_missing_child_run_id_rejected(self) -> None:
        outcome = _run_async(
            self.tool.execute(_request(QUERY_AGENT_NAME, {}))
        )
        self.assertEqual("invalid_child_run_id", outcome.diagnostic.code)
        self.assertEqual(0, len(self.handler.query_requests))


class CancelAgentToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _FakeHandler()
        self.tool = CancelAgentTool(handler=self.handler)

    def test_definition_shape(self) -> None:
        self.assertEqual(CANCEL_AGENT_NAME, self.tool.definition.name)

    def test_valid_cancel_delegates(self) -> None:
        outcome = _run_async(
            self.tool.execute(
                _request(CANCEL_AGENT_NAME, {"childRunId": "child_x", "reason": "够了"})
            )
        )
        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual(1, len(self.handler.cancel_requests))

    def test_missing_child_run_id_rejected(self) -> None:
        outcome = _run_async(
            self.tool.execute(_request(CANCEL_AGENT_NAME, {}))
        )
        self.assertEqual("invalid_child_run_id", outcome.diagnostic.code)
        self.assertEqual(0, len(self.handler.cancel_requests))

    def test_empty_reason_rejected(self) -> None:
        outcome = _run_async(
            self.tool.execute(
                _request(CANCEL_AGENT_NAME, {"childRunId": "child_x", "reason": "  "})
            )
        )
        self.assertEqual("invalid_cancel_reason", outcome.diagnostic.code)
        self.assertEqual(0, len(self.handler.cancel_requests))


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main()
