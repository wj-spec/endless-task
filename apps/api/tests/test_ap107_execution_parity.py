"""AP-107 flip Stage 4a: execution parity between legacy direct execution
and the v2 pipeline over the SAME tool instances.

TP-5 golden-trajectory evidence at the execution-engine level (test-only):
for representative batch shapes, every call must produce the same
status/content/diagnostic-code outcome on both paths. Approval is excluded
by construction (D2: legacy approval remains the only approval source).
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import (
    DefaultToolPipeline,
    LegacyToolAdapter,
    ResolvedToolCall,
    ToolBatchRequest,
    ToolExecutionMode,
    ToolExecutionRequest,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallError,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)


class EchoRegisteredTool:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Echo {name}.",
            input_schema={"type": "object"},
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=10.0,
        )
        self._fail = fail

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        if self._fail:
            return ToolResult(
                tool_call_id=call.id,
                content="",
                error=ToolCallError(
                    code="tool_invalid_input",
                    safe_message="Echo 失败。",
                    retryable=False,
                ),
            )
        return ToolResult(
            tool_call_id=call.id,
            content=f"echo:{call.id}",
            structured_content={"echo": call.id},
        )


def legacy_call(call_id: str, tool_name: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        conversation_id="conversation_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name=tool_name,
        arguments={},
        status=ToolCallStatus.CREATED,
        created_at="2026-09-03T00:00:00Z",
    )


def v2_request(call_id: str, tool_name: str) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id=call_id,
        tool_name=tool_name,
        arguments={},
        conversation_id="conversation_1",
        run_id="run_1",
        model_turn_id="model_turn_1",
        correlation_id="correlation_1",
        cancellation=CancellationToken(),
        created_at="2026-09-03T00:00:00Z",
        legacy_turn_id="turn_1",
        legacy_response_variant_id="variant_1",
    )


class ExecutionParityTest(unittest.IsolatedAsyncioTestCase):
    async def _legacy_outcomes(
        self,
        entries: tuple[tuple[str, EchoRegisteredTool], ...],
    ) -> Mapping[str, Mapping[str, Any]]:
        outcomes: dict[str, Mapping[str, Any]] = {}
        for call_id, tool in entries:
            result = await tool.execute(
                legacy_call(call_id, tool.definition.name),
                CancellationToken(),
            )
            outcomes[call_id] = {
                "status": "failed" if result.error is not None else "completed",
                "content": result.content,
                "code": result.error.code if result.error is not None else None,
            }
        return outcomes

    async def _v2_outcomes(
        self,
        entries: tuple[tuple[str, EchoRegisteredTool], ...],
        *,
        mode: ToolExecutionMode,
    ) -> Mapping[str, Mapping[str, Any]]:
        pipeline = DefaultToolPipeline()
        calls = []
        for call_id, tool in entries:
            adapter = LegacyToolAdapter(tool)
            calls.append(
                ResolvedToolCall(
                    request=v2_request(call_id, tool.definition.name),
                    tool=adapter,
                )
            )
        result = await pipeline.execute(ToolBatchRequest(calls=tuple(calls)))
        return {
            outcome.call_id: {
                "status": outcome.status.value,
                "content": outcome.content,
                "code": (
                    outcome.diagnostic.code if outcome.diagnostic is not None else None
                ),
            }
            for outcome in result.outcomes
        }

    async def test_parallel_read_batch_matches(self) -> None:
        entries = (
            ("c1", EchoRegisteredTool("echo_a")),
            ("c2", EchoRegisteredTool("echo_b")),
            ("c3", EchoRegisteredTool("echo_c")),
        )
        legacy = await self._legacy_outcomes(entries)
        v2 = await self._v2_outcomes(entries, mode=ToolExecutionMode.PARALLEL)
        self.assertEqual(legacy, v2)

    async def test_failure_and_success_mix_matches(self) -> None:
        entries = (
            ("c1", EchoRegisteredTool("echo_ok")),
            ("c2", EchoRegisteredTool("echo_bad", fail=True)),
        )
        legacy = await self._legacy_outcomes(entries)
        v2 = await self._v2_outcomes(entries, mode=ToolExecutionMode.PARALLEL)
        self.assertEqual("failed", legacy["c2"]["status"])
        self.assertEqual("tool_invalid_input", legacy["c2"]["code"])
        self.assertEqual(legacy, v2)

    async def test_sequential_shape_matches(self) -> None:
        entries = (
            ("c1", EchoRegisteredTool("echo_one")),
            ("c2", EchoRegisteredTool("echo_two")),
        )
        legacy = await self._legacy_outcomes(entries)
        v2 = await self._v2_outcomes(entries, mode=ToolExecutionMode.SEQUENTIAL)
        self.assertEqual(legacy, v2)


if __name__ == "__main__":
    unittest.main()
