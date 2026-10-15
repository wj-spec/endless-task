"""In-process child batch scheduler prefigure (M4A DR-1 slice 2).

DR-1 acceptance wants "主 Agent 能并行运行最多 4 个只读 child 并结构化
汇总", and 06 §5.6 fixes the default scheduling limits:

- max concurrent children per parent = 4,
- max children per parent = 8,
- collect-all default: one failing child never terminates the others.

The production kernel (02 §11.3) does not exist yet, so this module
prefigures the scheduler semantics **inside the delegation boundary**
against any AgentKernel (fake kernels in tests, the production kernel
later): a batch is validated to completion before any child runs (fail
fast, zero side effects on policy violations), then executed with a
concurrency semaphore, and every child result is collected into a
structured summary. Queued/running state persistence, timeout and cancel
propagation stay with DR-2 once a real kernel exists.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

from .coordinator import InProcessChildCoordinator
from .protocol import ChildOutcome, ChildStatus, SpawnSpec

#: Default limits mirror 06 §5.6.
DEFAULT_MAX_CONCURRENT_CHILDREN = 4
DEFAULT_MAX_CHILDREN_PER_PARENT = 8
#: 在册（已预留、未终态）子运行的绝对上限：排队不能变成无界队列。
DEFAULT_MAX_QUEUED_CHILDREN = 32


@dataclass(frozen=True)
class ChildBatchLimits:
    """Data-driven scheduling caps (06 §5.6 defaults)."""

    max_concurrent_children: int = DEFAULT_MAX_CONCURRENT_CHILDREN
    max_children_per_parent: int = DEFAULT_MAX_CHILDREN_PER_PARENT
    #: 在册（含排队）子运行的绝对上限；超过仍拒绝（而不是无限排队）。
    max_queued_children: int = DEFAULT_MAX_QUEUED_CHILDREN

    def __post_init__(self) -> None:
        for field_name in (
            "max_concurrent_children",
            "max_children_per_parent",
            "max_queued_children",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise AgentPlatformError(
                    "invalid_child_batch_limits",
                    f"{field_name} must be a positive integer",
                )
        if self.max_concurrent_children > self.max_children_per_parent:
            raise AgentPlatformError(
                "invalid_child_batch_limits",
                "max_concurrent_children must not exceed max_children_per_parent",
            )
        if self.max_children_per_parent > self.max_queued_children:
            raise AgentPlatformError(
                "invalid_child_batch_limits",
                "max_children_per_parent must not exceed max_queued_children",
            )


@dataclass(frozen=True)
class ChildBatchSummary:
    """Structured result of one batch of child spawns (collect-all)."""

    requested: int
    outcomes: tuple[ChildOutcome, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.requested, int)
            or isinstance(self.requested, bool)
            or self.requested < 0
        ):
            raise AgentPlatformError(
                "invalid_child_batch_summary",
                "requested must be a non-negative integer",
            )
        normalized = tuple(sorted(self.outcomes, key=lambda item: item.child_run_id))
        object.__setattr__(self, "outcomes", normalized)
        if len(normalized) > self.requested:
            raise AgentPlatformError(
                "invalid_child_batch_summary",
                "Batch summary holds more outcomes than requested",
            )

    @property
    def completed(self) -> tuple[ChildOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ChildStatus.COMPLETED
        )

    @property
    def failed(self) -> tuple[ChildOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ChildStatus.FAILED
        )

    @property
    def cancelled(self) -> tuple[ChildOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is ChildStatus.CANCELLED
        )


class InProcessChildScheduler:
    """Runs a validated batch of read-only children with bounded concurrency.

    Bound to one coordinator (one parent run). ``run_batch`` is
    collect-all: policy violations abort the whole batch before any child
    runs; execution failures become per-child FAILED outcomes instead of
    terminating the remaining children.
    """

    def __init__(
        self,
        coordinator: InProcessChildCoordinator,
        *,
        limits: Optional[ChildBatchLimits] = None,
    ) -> None:
        if not isinstance(coordinator, InProcessChildCoordinator):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child scheduler requires an InProcessChildCoordinator",
            )
        if limits is not None and not isinstance(limits, ChildBatchLimits):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "limits must be a ChildBatchLimits",
            )
        self._coordinator = coordinator
        self._limits = limits if limits is not None else ChildBatchLimits()

    async def run_batch(
        self,
        specs: Sequence[SpawnSpec],
        *,
        fail_fast_policy: bool = True,
    ) -> ChildBatchSummary:
        """Validate the whole batch, then execute children concurrently.

        预检守的是**绝对**在册上限 ``max_queued_children``：超过在册容量
        （``max_children_per_parent``）**不是**拒绝理由——那是排队的事，超出的
        子运行会在协调器的 admission 闸门外等名额（库里就是 wire 的 `queued`）。
        仍然"在任何子运行开始前整批失败"，保留 fail-fast 的价值。
        """
        if not isinstance(specs, (list, tuple)) or any(
            not isinstance(spec, SpawnSpec) for spec in specs
        ):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "run_batch requires a sequence of SpawnSpec",
            )
        if not isinstance(fail_fast_policy, bool):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "fail_fast_policy must be boolean",
            )
        already_tracked = self._coordinator.inflight_child_count()
        incoming = len(specs)
        if already_tracked + incoming > self._limits.max_queued_children:
            raise AgentPlatformError(
                "delegation_children_limit_exceeded",
                "Batch exceeds the maximum queued children per parent",
                retryable=False,
                details={
                    "max_queued_children": self._limits.max_queued_children,
                    "tracked": already_tracked,
                    "incoming": incoming,
                },
            )
        # Fail fast on the whole batch before any child runs: prepare is
        # pure (no kernel call, no key reservation), so a policy violation
        # in any spec aborts with zero side effects.
        prepared = [self._coordinator.prepare_spawn(spec) for spec in specs]
        keys = {p.spec.parent_tool_call_id for p in prepared}
        if len(keys) != len(prepared):
            raise AgentPlatformError(
                "delegation_duplicate_spawn",
                "Batch contains more than one spec for the same parent tool call",
                retryable=False,
            )
        semaphore = asyncio.Semaphore(self._limits.max_concurrent_children)

        async def run_one(prepared_spawn):
            async with semaphore:
                return await self._coordinator.execute_prepared(prepared_spawn)

        records = await asyncio.gather(*(run_one(item) for item in prepared))
        return ChildBatchSummary(
            requested=incoming,
            outcomes=tuple(record.outcome for record in records),
        )


__all__ = [
    "DEFAULT_MAX_CHILDREN_PER_PARENT",
    "DEFAULT_MAX_CONCURRENT_CHILDREN",
    "DEFAULT_MAX_QUEUED_CHILDREN",
    "ChildBatchLimits",
    "ChildBatchSummary",
    "InProcessChildScheduler",
]
