from __future__ import annotations

from collections.abc import Sequence

from endless_task.agent_platform import AgentPlatformError
from endless_task.runtime.provider import ProviderMessage

from .protocol import ContextEngine, ContextRequest, ContextSnapshot


async def assert_context_parity(
    engine: ContextEngine,
    request: ContextRequest,
    expected_messages: Sequence[ProviderMessage],
) -> ContextSnapshot:
    snapshot = await engine.assemble(request)
    if snapshot.messages != tuple(expected_messages):
        raise AgentPlatformError(
            "context_parity_mismatch",
            "Context adapter messages differ from the legacy projection",
        )
    if snapshot.reserved_output_tokens != request.budget.reserved_output_tokens:
        raise AgentPlatformError(
            "context_budget_mismatch",
            "Context adapter changed the reserved output budget",
        )
    return snapshot