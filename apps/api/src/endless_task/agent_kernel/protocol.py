from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    freeze_json_object,
    require_identifier,
    require_protocol_version,
    require_text,
)
from endless_task.runtime_ledger import TraceContext

AGENT_KERNEL_PROTOCOL_VERSION = 1


class AgentMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class RunOutcomeStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AgentMessage:
    role: AgentMessageRole
    content: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = AGENT_KERNEL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=AGENT_KERNEL_PROTOCOL_VERSION,
            protocol="agent_message",
        )
        if not isinstance(self.role, AgentMessageRole):
            raise AgentPlatformError("invalid_agent_kernel_value", "Agent message role must use a protocol enum value")
        object.__setattr__(
            self,
            "content",
            require_text(self.content, field_name="content", max_length=1_000_000),
        )
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata, field_name="metadata"),
        )


@dataclass(frozen=True)
class RunCommand:
    run_id: str
    conversation_id: str
    lane_id: str
    trigger_entry_id: str
    message: AgentMessage
    trace: TraceContext
    capability_profile: str
    deadline_monotonic: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: int = AGENT_KERNEL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=AGENT_KERNEL_PROTOCOL_VERSION,
            protocol="run_command",
        )
        for field_name in (
            "run_id",
            "conversation_id",
            "lane_id",
            "trigger_entry_id",
            "capability_profile",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        if self.run_id != self.trace.run_id:
            raise AgentPlatformError("invalid_agent_kernel_value", "Run command and trace must reference the same run")
        if self.message.role is not AgentMessageRole.USER:
            raise AgentPlatformError("invalid_agent_kernel_value", "Run command must start with a user message")
        object.__setattr__(
            self,
            "metadata",
            freeze_json_object(self.metadata, field_name="metadata"),
        )


@dataclass(frozen=True)
class RunUsage:
    """一次运行的用量计量（中性类型，不依赖委派层）。

    这里只放四个中性计数：适配器要把执行器知道的真实用量带出来，而协议包不能
    反向 import 委派层（``delegation/__init__`` → coordinator → agent_kernel
    会成环），所以"用量 → 委派层 UsageSummary"的映射由委派层自己做。
    """

    input_tokens: int = 0
    output_tokens: int = 0
    model_turns: int = 0
    tool_calls: int = 0

    def __post_init__(self) -> None:
        for field_name in ("input_tokens", "output_tokens", "model_turns", "tool_calls"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise AgentPlatformError(
                    "invalid_agent_kernel_value",
                    f"{field_name} must be a non-negative integer",
                )


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: RunOutcomeStatus
    trace: TraceContext
    assistant_message: Optional[AgentMessage] = None
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    final_context_fingerprint: Optional[str] = None
    # 可选：既有构造点（测试里大量直接构造 RunOutcome）不提供用量时保持 None，
    # 委派层按空用量处理，这些调用点的行为不变。
    usage: Optional[RunUsage] = None
    protocol_version: int = AGENT_KERNEL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.protocol_version,
            expected=AGENT_KERNEL_PROTOCOL_VERSION,
            protocol="run_outcome",
        )
        object.__setattr__(self, "run_id", require_identifier(self.run_id, field_name="run_id"))
        if not isinstance(self.status, RunOutcomeStatus):
            raise AgentPlatformError("invalid_agent_kernel_value", "Run outcome status must use a protocol enum value")
        if self.usage is not None and not isinstance(self.usage, RunUsage):
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Run outcome usage must use the protocol usage type",
            )
        if self.run_id != self.trace.run_id:
            raise AgentPlatformError("invalid_agent_kernel_value", "Run outcome and trace must reference the same run")
        if self.status is RunOutcomeStatus.COMPLETED:
            if self.assistant_message is None:
                raise AgentPlatformError("invalid_agent_kernel_value", "Completed runs require an assistant message")
            if self.assistant_message.role is not AgentMessageRole.ASSISTANT:
                raise AgentPlatformError("invalid_agent_kernel_value", "Completed run message must use the assistant role")
        if self.final_context_fingerprint is not None:
            object.__setattr__(
                self,
                "final_context_fingerprint",
                require_identifier(
                    self.final_context_fingerprint,
                    field_name="final_context_fingerprint",
                ),
            )


class AgentKernel(Protocol):
    async def run(self, command: RunCommand) -> RunOutcome: ...

    async def steer(self, run_id: str, message: AgentMessage) -> None: ...

    async def cancel(self, run_id: str, reason: str) -> None: ...