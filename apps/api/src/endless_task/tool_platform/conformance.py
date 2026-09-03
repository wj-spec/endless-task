from __future__ import annotations

from collections.abc import Awaitable, Callable

from endless_task.agent_platform import AgentPlatformError

from .protocol import AgentToolV2, ToolExecutionRequest, ToolOutcome


async def assert_tool_conformance(
    tool: AgentToolV2,
    request: ToolExecutionRequest,
    *,
    expected: ToolOutcome | None = None,
    predicate: Callable[[ToolOutcome], bool] | None = None,
) -> ToolOutcome:
    """Minimal reusable contract harness for native and adapted v2 tools."""

    if request.tool_name != tool.definition.name:
        raise AgentPlatformError(
            "conformance_name_mismatch",
            "Conformance request must target the tested tool",
        )
    outcome = await tool.execute(request)
    if outcome.call_id != request.call_id:
        raise AgentPlatformError(
            "conformance_call_mismatch",
            "Tool outcome must preserve the request call identifier",
        )
    if expected is not None and outcome != expected:
        raise AgentPlatformError(
            "conformance_outcome_mismatch",
            "Tool outcome did not match the expected contract value",
        )
    if predicate is not None and not predicate(outcome):
        raise AgentPlatformError(
            "conformance_predicate_failed",
            "Tool outcome failed its conformance predicate",
        )
    return outcome


async def invoke_and_capture(factory: Callable[[], Awaitable[ToolOutcome]]) -> ToolOutcome:
    return await factory()