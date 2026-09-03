"""Deterministic tool scheduler for the version 2 tool platform.

AP-104 (M1). Executes a batch of scheduled tool calls honoring each tool's
declared ``execution_mode``:

- ``parallel`` calls share a bounded-concurrency bucket (global semaphore),
- ``sequential`` calls run one at a time in model call order,
- ``exclusive`` calls wait until every other call has finished, then run one
  at a time in call order,
- ``path_scoped`` calls serialize per canonical path and run concurrently
  across distinct paths; a call without path information degrades to
  sequential.

Results are always committed in the model's original call order. A call whose
cancellation is already set at admission time is not dispatched and yields a
``cancelled`` outcome with code ``aborted_before_dispatch`` so replay stays
complete.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    require_identifier,
    require_protocol_version,
)

from .protocol import ToolExecutionMode, ToolOutcome, ToolOutcomeStatus

TOOL_SCHEDULER_SCHEMA_VERSION = 1


class CancellationProbe(Protocol):
    @property
    def is_cancelled(self) -> bool: ...


@dataclass(frozen=True)
class ScheduledCall:
    """One unit of scheduled work; ``runner`` must never raise bare errors."""

    index: int
    call_id: str
    tool_name: str
    mode: ToolExecutionMode
    runner: Callable[[], Awaitable[ToolOutcome]]
    path: Optional[str] = None
    cancellation: Optional[CancellationProbe] = None
    schema_version: int = TOOL_SCHEDULER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_SCHEDULER_SCHEMA_VERSION,
            protocol="scheduled_call",
        )
        if not isinstance(self.index, int) or self.index < 0:
            raise AgentPlatformError(
                "invalid_scheduled_call",
                "Scheduled call index must be a non-negative integer",
            )
        object.__setattr__(
            self,
            "call_id",
            require_identifier(self.call_id, field_name="call_id"),
        )
        object.__setattr__(
            self,
            "tool_name",
            require_identifier(self.tool_name, field_name="tool_name", max_length=64),
        )
        if not isinstance(self.mode, ToolExecutionMode):
            raise AgentPlatformError(
                "invalid_scheduled_call",
                "Scheduled call mode must use a protocol enum value",
            )
        if not callable(self.runner):
            raise AgentPlatformError(
                "invalid_scheduled_call",
                "Scheduled call requires a runner",
            )
        if self.path is not None and not isinstance(self.path, str):
            raise AgentPlatformError(
                "invalid_scheduled_call",
                "Scheduled call path must be text",
            )
        if self.cancellation is not None and not callable(
            getattr(self.cancellation, "raise_if_cancelled", None)
        ):
            raise AgentPlatformError(
                "invalid_scheduled_call",
                "Scheduled call cancellation must expose raise_if_cancelled",
            )


class ToolScheduler:
    """Plans and executes scheduled calls with bounded concurrency."""

    def __init__(self, *, max_concurrent: int = 8) -> None:
        if not isinstance(max_concurrent, int) or max_concurrent <= 0:
            raise AgentPlatformError(
                "invalid_scheduler_config",
                "Scheduler max_concurrent must be a positive integer",
            )
        self._max_concurrent = max_concurrent

    async def execute(
        self,
        calls: Sequence[ScheduledCall],
    ) -> tuple[ToolOutcome, ...]:
        if isinstance(calls, (str, bytes)) or not isinstance(calls, Sequence):
            raise AgentPlatformError(
                "invalid_scheduled_calls",
                "Scheduler requires a sequence of ScheduledCall values",
            )
        scheduled = tuple(calls)
        if any(not isinstance(call, ScheduledCall) for call in scheduled):
            raise AgentPlatformError(
                "invalid_scheduled_calls",
                "Scheduler requires ScheduledCall values",
            )
        indexes = [call.index for call in scheduled]
        if len(set(indexes)) != len(indexes):
            raise AgentPlatformError(
                "invalid_scheduled_calls",
                "Scheduled call indexes must be unique",
            )
        ordered = tuple(sorted(scheduled, key=lambda call: call.index))
        outcomes: dict[int, ToolOutcome] = {}
        concurrent_groups, tail = _plan(ordered)
        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def run_group(group: tuple[ScheduledCall, ...]) -> dict[int, ToolOutcome]:
            # Calls inside one group must never overlap each other.
            group_outcomes: dict[int, ToolOutcome] = {}
            for call in group:
                group_outcomes[call.index] = await self._run_one(call)
            return group_outcomes

        async def guarded(
            group: tuple[ScheduledCall, ...],
        ) -> dict[int, ToolOutcome]:
            async with semaphore:
                return await run_group(group)

        # Groups (parallel calls and distinct path groups) run concurrently
        # under the global bound; calls within a group serialize.
        group_results = await asyncio.gather(
            *(guarded(group) for group in concurrent_groups)
        )
        for group_outcomes in group_results:
            outcomes.update(group_outcomes)
        # Sequential (including degraded path-less path-scoped) calls run one
        # at a time in model order, then exclusive calls run alone.
        for call in tail:
            outcomes[call.index] = await self._run_one(call)
        return tuple(outcomes[index] for index in indexes)

    async def _run_one(self, call: ScheduledCall) -> ToolOutcome:
        if _is_cancelled(call.cancellation):
            return _aborted_outcome(call, code="aborted_before_dispatch")
        try:
            return await call.runner()
        except asyncio.CancelledError:
            # Per-call cancellation surfaces as CANCELLED for replay; outer
            # task cancellation of the scheduler itself is not intercepted
            # here, so this branch only covers runner-level cancels.
            return _aborted_outcome(call, code="cancelled_during_execution")
        except Exception as error:
            # A runner must return structured outcomes; a bare error means the
            # side effect state is unknown and must not be reported as failed.
            return ToolOutcome(
                call_id=call.call_id,
                status=ToolOutcomeStatus.UNKNOWN,
                content="",
                diagnostic=SafeDiagnostic(
                    code="tool_execution_unknown",
                    safe_message="工具执行返回未知结果，副作用状态无法确认。",
                    retryable=False,
                    details={"error_type": type(error).__name__},
                ),
            )


def _plan(
    calls: tuple[ScheduledCall, ...],
) -> tuple[tuple[tuple[ScheduledCall, ...], ...], tuple[ScheduledCall, ...]]:
    """Build deterministic execution groups per §4.8 scheduling semantics.

    Returns ``(concurrent_groups, tail)``. Every group in ``concurrent_groups``
    may run concurrently with the others (bounded by the scheduler), but calls
    inside one group are serialized; the tail runs one call at a time in model
    order after all concurrent groups finish.
    """
    parallel: list[list[ScheduledCall]] = []
    path_groups: dict[str, list[ScheduledCall]] = {}
    no_path_path_scoped: list[ScheduledCall] = []
    sequential: list[ScheduledCall] = []
    exclusive: list[ScheduledCall] = []
    for call in calls:
        if call.mode is ToolExecutionMode.PARALLEL:
            parallel.append([call])
        elif call.mode is ToolExecutionMode.PATH_SCOPED:
            if call.path:
                path_groups.setdefault(call.path, []).append(call)
            else:
                # Degrades to sequential when no path information is present.
                no_path_path_scoped.append(call)
        elif call.mode is ToolExecutionMode.SEQUENTIAL:
            sequential.append(call)
        elif call.mode is ToolExecutionMode.EXCLUSIVE:
            exclusive.append(call)
        else:  # pragma: no cover - enum is exhaustive
            sequential.append(call)
    concurrent_groups: list[tuple[ScheduledCall, ...]] = list(
        tuple(group) for group in parallel
    )
    for path in sorted(
        path_groups,
        key=lambda path: min(call.index for call in path_groups[path]),
    ):
        concurrent_groups.append(tuple(path_groups[path]))
    tail = tuple(no_path_path_scoped + sequential + exclusive)
    return tuple(concurrent_groups), tail


def _is_cancelled(cancellation: Optional[CancellationProbe]) -> bool:
    if cancellation is None:
        return False
    try:
        return bool(cancellation.is_cancelled)
    except Exception:
        return False


def _aborted_outcome(call: ScheduledCall, *, code: str) -> ToolOutcome:
    return ToolOutcome(
        call_id=call.call_id,
        status=ToolOutcomeStatus.CANCELLED,
        content="",
        diagnostic=SafeDiagnostic(
            code=code,
            safe_message="工具调用在派发前被取消。",
            retryable=False,
            details={"tool_name": call.tool_name},
        ),
    )


__all__ = [
    "TOOL_SCHEDULER_SCHEMA_VERSION",
    "CancellationProbe",
    "ScheduledCall",
    "ToolScheduler",
]
