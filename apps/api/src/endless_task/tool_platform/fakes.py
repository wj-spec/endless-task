from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import AgentToolV2, ToolDefinitionV2, ToolExecutionRequest, ToolOutcome


@dataclass
class FakeTool(AgentToolV2):
    definition: ToolDefinitionV2
    outcome: ToolOutcome
    requests: list[ToolExecutionRequest] = field(default_factory=list)

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        self.requests.append(request)
        return self.outcome