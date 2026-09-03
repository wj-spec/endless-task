"""DefaultContextEngine assembly orchestrator (M2 prelude Stage 3).

A seam-ready engine over the frozen ``ContextEngine`` contract: it keeps the
runtime's own message assembly (injected) as the source of truth for parity
while applying the platform planning pipeline (retention -> budget plan ->
fingerprint) to the segment bookkeeping. Runtime cutover (AP-206) will feed
the real message/segment providers here; nothing about loop semantics is
changed until then.

- ``assemble``: candidates from the injected segment provider (after the
  optional tool-result retention pre-pass) go through
  ``plan_context_segments``; the snapshot keeps the injected messages
  untouched (legacy parity) plus the planned segments, diagnostics and a
  stable fingerprint.
- ``bootstrap`` / ``ingest`` / ``maintain`` / ``compact`` / ``commit_turn``
  are pass-throughs returning protocol results; stateful maintenance,
  compaction and memory commit belong to the AP-206 wiring slices and are
  not fabricated here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Optional

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime.provider import ProviderMessage

from .planner import (
    ContextPlan,
    fingerprint_context,
    plan_context_segments,
)
from .protocol import (
    BootstrapRequest,
    CommitResult,
    CompactionRequest,
    CompactionResult,
    ContextBudget,
    ContextEngine,
    ContextInput,
    ContextRequest,
    ContextSegment,
    ContextSnapshot,
    MaintenanceRequest,
    MaintenanceResult,
    TurnOutcome,
)

SegmentProvider = Callable[[ContextRequest], Sequence[ContextSegment]]
MessageProvider = Callable[[ContextRequest], Sequence[ProviderMessage]]
RetentionPass = Callable[
    [ContextRequest, Sequence[ContextSegment]],
    Sequence[ContextSegment],
]


class DefaultContextEngine(ContextEngine):
    """Seam engine: runtime message assembly + platform planning pipeline."""

    def __init__(
        self,
        *,
        messages_provider: MessageProvider,
        segments_provider: SegmentProvider,
        retention_pass: Optional[RetentionPass] = None,
    ) -> None:
        if not callable(messages_provider) or not callable(segments_provider):
            raise AgentPlatformError(
                "invalid_context_engine",
                "DefaultContextEngine requires message and segment providers",
            )
        if retention_pass is not None and not callable(retention_pass):
            raise AgentPlatformError(
                "invalid_context_engine",
                "retention_pass must be callable",
            )
        self._messages_provider = messages_provider
        self._segments_provider = segments_provider
        self._retention_pass = retention_pass

    async def bootstrap(self, request: BootstrapRequest) -> None:
        if not isinstance(request, BootstrapRequest):
            raise AgentPlatformError(
                "invalid_context_value",
                "Bootstrap requires a BootstrapRequest",
            )
        return None

    async def ingest(self, event: ContextInput) -> None:
        if not isinstance(event, ContextInput):
            raise AgentPlatformError(
                "invalid_context_value",
                "Ingest requires a ContextInput",
            )
        return None

    async def assemble(self, request: ContextRequest) -> ContextSnapshot:
        if not isinstance(request, ContextRequest):
            raise AgentPlatformError(
                "invalid_context_value",
                "Assemble requires a ContextRequest",
            )
        messages = tuple(self._messages_provider(request))
        if any(not isinstance(message, ProviderMessage) for message in messages):
            raise AgentPlatformError(
                "invalid_context_value",
                "Messages provider returned non-ProviderMessage values",
            )
        candidates = tuple(self._segments_provider(request))
        if any(not isinstance(segment, ContextSegment) for segment in candidates):
            raise AgentPlatformError(
                "invalid_context_value",
                "Segments provider returned non-ContextSegment values",
            )
        if self._retention_pass is not None:
            candidates = tuple(self._retention_pass(request, candidates))
        plan: ContextPlan = plan_context_segments(candidates, request.budget)
        estimated_tokens = plan.estimated_tokens
        fingerprint = fingerprint_context(
            messages,
            plan.included,
            estimated_tokens=estimated_tokens,
            input_budget=request.budget.input_budget,
        )
        return ContextSnapshot(
            messages=tuple(messages),
            segments=plan.included,
            estimated_tokens=estimated_tokens,
            reserved_output_tokens=request.budget.reserved_output_tokens,
            budget=request.budget,
            fingerprint=fingerprint,
            diagnostics=plan.diagnostics,
        )

    async def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        if not isinstance(request, MaintenanceRequest):
            raise AgentPlatformError(
                "invalid_context_value",
                "Maintain requires a MaintenanceRequest",
            )
        return MaintenanceResult(changed=False)

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        if not isinstance(request, CompactionRequest):
            raise AgentPlatformError(
                "invalid_context_value",
                "Compact requires a CompactionRequest",
            )
        return CompactionResult(checkpoint_id=None, released_tokens=0)

    async def commit_turn(self, outcome: TurnOutcome) -> CommitResult:
        if not isinstance(outcome, TurnOutcome):
            raise AgentPlatformError(
                "invalid_context_value",
                "Commit requires a TurnOutcome",
            )
        return CommitResult(committed=False)


__all__ = ["DefaultContextEngine"]
