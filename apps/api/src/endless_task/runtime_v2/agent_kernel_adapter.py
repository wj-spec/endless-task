"""Production AgentKernel adapter over the runtime v2 executor (M4A DR-1).

This adapter is the missing production binding between the frozen
:mod:`agent_kernel` protocol and the real run path (02 3.1 / 11.3 gap):

- ``agent_kernel`` stays a pure protocol package (import boundary test),
- this module lives in ``runtime_v2`` and implements the protocol by
  driving :class:`AgentRunExecutor` against the real repository,

A :class:`RunCommand` is executed as an **internal child run**: an
isolated conversation/lane/trigger entry is created, the run is created
with ``run_id=command.run_id`` (protocol requires outcome run id ==
command run id), the executor runs to a terminal state, and the
:class:`RunExecutionResult` is projected back to a :class:`RunOutcome`.

Composition root supplies the two seams this module cannot and must not
assemble itself:

- ``conversation_factory`` — how a child conversation is created
  (workspace binding, ephemeral kind, product-visibility filtering),
- ``executor_builder`` — a fresh :class:`AgentRunExecutor` per run with
  child-specific context/tools/profile wiring (gateway owns the real
  provider/tool/model dependencies).

Cancellation is real, not stubbed: an active run keeps its cancellation
token in a registry, and :meth:`cancel` releases it so the executor
finishes with ``CANCELLED``.

This module is *Not Integrated*: nothing in ``app.py`` / gateway
constructs it yet, and no feature flag exists. It proves the protocol
binding against the real execution surface with conformance-style tests.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from endless_task.agent_kernel import (
    AgentKernel,
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.domain.models import Conversation
from endless_task.runtime.cancellation import CancellationToken

from .domain import Actor, RunStatus, TranscriptEntryType
from .execution import AgentRunExecutor, RunExecutionResult

ConversationFactory = Callable[[], Awaitable[Conversation]]
ExecutorBuilder = Callable[[], AgentRunExecutor]


class RuntimeV2AgentKernel(AgentKernel):
    """AgentKernel protocol implementation over ``AgentRunExecutor``."""

    def __init__(
        self,
        *,
        repository,
        conversation_factory: ConversationFactory,
        executor_builder: ExecutorBuilder,
    ) -> None:
        if not callable(getattr(repository, "create_run", None)):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "RuntimeV2AgentKernel requires a runtime v2 repository",
            )
        if not callable(conversation_factory):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "conversation_factory must be callable",
            )
        if not callable(executor_builder):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "executor_builder must be callable",
            )
        self._repository = repository
        self._conversation_factory = conversation_factory
        self._executor_builder = executor_builder
        self._active: dict[str, tuple[AgentRunExecutor, CancellationToken]] = {}

    async def run(self, command: RunCommand) -> RunOutcome:
        """Execute one child :class:`RunCommand` to a terminal outcome."""
        if not isinstance(command, RunCommand):
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "RuntimeV2AgentKernel.run requires a RunCommand",
            )
        if command.run_id in self._active:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "A child run with this run id is already active",
                details={"run_id": command.run_id},
            )
        conversation = await self._conversation_factory()
        lane = self._repository.create_lane(conversation_id=conversation.id)
        trigger = self._repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": command.message.content},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self._repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
            run_id=command.run_id,
            correlation_id=command.trace.correlation_id,
        )
        executor = self._executor_builder()
        token = CancellationToken()
        self._active[command.run_id] = (executor, token)
        try:
            result = await executor.execute(command.run_id, cancellation_token=token)
        finally:
            self._active.pop(command.run_id, None)
        return _project_run_result(
            result=result,
            command=command,
            repository=self._repository,
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        entry = self._active.get(run_id)
        if entry is None:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot steer an inactive child run",
                details={"run_id": run_id},
            )
        executor, _ = entry
        await executor.enqueue_user_message(message.content)

    async def cancel(self, run_id: str, reason: str) -> None:
        entry = self._active.get(run_id)
        if entry is None:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot cancel an inactive child run",
                details={"run_id": run_id},
            )
        executor, token = entry
        token.cancel()
        await executor.request_cancel(cancelled_by="delegation")


def _project_run_result(
    *,
    result: RunExecutionResult,
    command: RunCommand,
    repository,
) -> RunOutcome:
    """Map a :class:`RunExecutionResult` onto the kernel protocol types."""
    status = _map_run_status(result.status)
    diagnostics: list[SafeDiagnostic] = []
    if result.status is RunStatus.WAITING_APPROVAL:
        # Read-only children must never block on approvals; a waiting
        # approval means the child surface leaked a gated tool. Fail
        # closed instead of reporting a non-terminal state as success.
        diagnostics.append(
            SafeDiagnostic(
                code="child_waiting_approval",
                safe_message=(
                    "Child run requested approval, which read-only delegation "
                    "does not allow."
                ),
            )
        )
    elif result.status is RunStatus.FAILED:
        run = repository.get_run(command.run_id)
        diagnostics.append(
            SafeDiagnostic(
                code=run.error_code or "child_run_failed",
                safe_message=run.safe_message or "Child run failed.",
            )
        )
    content = result.content or ""
    assistant_message: Optional[AgentMessage] = None
    if status is RunOutcomeStatus.COMPLETED:
        if not content.strip():
            # A completed kernel run must carry a final assistant message,
            # and empty results cannot be forged into a valid one.
            status = RunOutcomeStatus.FAILED
            diagnostics.append(
                SafeDiagnostic(
                    code="child_empty_result",
                    safe_message="Child run completed without a result message.",
                )
            )
        else:
            assistant_message = AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content=content,
            )
    return RunOutcome(
        run_id=command.run_id,
        status=status,
        trace=command.trace,
        assistant_message=assistant_message,
        diagnostics=tuple(diagnostics),
    )


def _map_run_status(status: RunStatus) -> RunOutcomeStatus:
    if status is RunStatus.COMPLETED:
        return RunOutcomeStatus.COMPLETED
    if status is RunStatus.CANCELLED:
        return RunOutcomeStatus.CANCELLED
    return RunOutcomeStatus.FAILED


__all__ = [
    "ConversationFactory",
    "ExecutorBuilder",
    "RuntimeV2AgentKernel",
]
