"""Agent execution state-machine boundary."""

from .fakes import FakeAgentKernel
from .protocol import (
    AGENT_KERNEL_PROTOCOL_VERSION,
    AgentKernel,
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)

__all__ = [
    "AGENT_KERNEL_PROTOCOL_VERSION",
    "AgentKernel",
    "AgentMessage",
    "AgentMessageRole",
    "FakeAgentKernel",
    "RunCommand",
    "RunOutcome",
    "RunOutcomeStatus",
]