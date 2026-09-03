from __future__ import annotations

from dataclasses import dataclass, field

from .protocol import AgentKernel, AgentMessage, RunCommand, RunOutcome


@dataclass
class FakeAgentKernel(AgentKernel):
    outcome: RunOutcome
    commands: list[RunCommand] = field(default_factory=list)
    steering: list[tuple[str, AgentMessage]] = field(default_factory=list)
    cancellations: list[tuple[str, str]] = field(default_factory=list)

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
        return self.outcome

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        self.steering.append((run_id, message))

    async def cancel(self, run_id: str, reason: str) -> None:
        self.cancellations.append((run_id, reason))