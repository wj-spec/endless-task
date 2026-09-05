"""Run-terminal trace observer bridging v2 journal into the ledger (M6 W6-1).

08 §OE-0/§OE-2 composition-root wiring: the SQLite RuntimeLedger sink
(OE-0) and canonical usage/cost (OE-2) become part of the real run path
by observing **terminal runs** of the v2 executor.

Design:

- :class:`AgentRunExecutor` gains one optional ``trace_observer`` param
  (default ``None`` → byte-identical behavior; same seam pattern as
  ``metrics`` / ``provider_retry_observer``). On a terminal run it
  awaits ``on_run_terminal(run_id)`` under a hard timeout, swallowing
  errors so observability can never hang or break a run.
- :class:`RunTraceObserver` is the protocol the executor calls.
- :class:`LedgerTraceObserver` implements it over the OE-0 ledger: it
  reads the persisted run + model turns (authoritative v2 journal) and
  mirrors canonical usage/cost rows into the trace tables. The v2
  journal stays the source of truth; the trace ledger is the
  observability projection (08 §OE-5: exporter, not truth).

Guarantees:

- **Idempotent**: a run whose usage rows already exist is skipped, so a
  resumed/retried terminal notification never double-counts cost.
- **Fail open for observability**: mirroring errors are logged and never
  propagate to the run (08 §2/§OE-1: observability must not break the
  agent). Effect/approval fail-closed semantics stay in the v2 journal.
- **Category accounting**: model usage is recorded with
  ``UsageCategory.PRIMARY`` and priced via the injected catalog.

Import discipline: this module never imports ``endless_task.storage`` or
``runtime_v2`` package internals at module top (cycle safety). The
journal accessors are duck-typed callables injected at construction;
``default_run_reader``/``default_usage_exists`` close over whatever
repository/ledger the composition root provides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol, Sequence

from endless_task.runtime_ledger.pricing import (
    CostRecord,
    PricingCatalog,
    UsageCategory,
    compute_cost,
)
from endless_task.runtime_ledger.protocol import (
    CanonicalUsage,
    RuntimeLedger,
    TraceContext,
)

logger = logging.getLogger(__name__)

#: Terminal run statuses that trigger usage mirroring.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: Model turn statuses whose usage rows count as completed usage.
_COMPLETED_TURN_STATUSES = frozenset({"completed"})

#: Hard cap on one terminal mirror so a wedged sink cannot hang a run.
MIRROR_TIMEOUT_SECONDS = 10.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class RunTraceObserver(Protocol):
    """Executors call this on terminal runs (optional, fail-open)."""

    async def on_run_terminal(self, run_id: str) -> None: ...


@dataclass(frozen=True)
class ModelTurnUsageView:
    """One completed model turn's canonical usage (from the v2 journal)."""

    model_turn_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    occurred_at: str


@dataclass(frozen=True)
class TerminalRunView:
    """Everything a terminal observer needs, read from the journal."""

    run_id: str
    correlation_id: str
    status: str
    turns: Sequence[ModelTurnUsageView]


def default_run_reader(repository) -> Callable[[str], TerminalRunView]:
    """Build a journal reader over a v2 repository (duck-typed).

    ``repository`` needs ``get_run``, ``list_model_turns`` and the run
    record's ``correlation_id``. Only terminal/completed rows are kept.
    """

    def read(run_id: str) -> TerminalRunView:
        run = repository.get_run(run_id)
        status_value = run.status.value
        turns: list[ModelTurnUsageView] = []
        for turn in repository.list_model_turns(run_id):
            if turn.status.value not in _COMPLETED_TURN_STATUSES:
                continue
            if turn.input_tokens is None or turn.output_tokens is None:
                continue
            provider = turn.provider or "unknown"
            model = turn.model or "unknown"
            occurred_at = turn.finished_at or turn.started_at or _utc_now()
            turns.append(
                ModelTurnUsageView(
                    model_turn_id=turn.id,
                    provider=provider,
                    model=model,
                    input_tokens=turn.input_tokens,
                    output_tokens=turn.output_tokens,
                    occurred_at=occurred_at,
                )
            )
        return TerminalRunView(
            run_id=run.id,
            correlation_id=run.correlation_id or run.id,
            status=status_value,
            turns=tuple(turns),
        )

    return read


def default_usage_exists(ledger) -> Callable[[str], bool]:
    """Idempotency check: does the ledger already hold usage for a run?

    ``ledger`` needs ``usage_for_run(run_id)`` returning a sequence.
    """

    def exists(run_id: str) -> bool:
        reader = getattr(ledger, "usage_for_run", None)
        if not callable(reader):
            return False  # no read side → best-effort mirror
        return bool(reader(run_id))

    return exists


class LedgerTraceObserver:
    """Mirror one terminal run's canonical usage into the runtime ledger.

    Constructed once per composition root when trace mode is enabled and
    shared by all executors; executors without an observer keep legacy
    behavior.
    """

    def __init__(
        self,
        ledger: RuntimeLedger,
        *,
        catalog: Optional[PricingCatalog] = None,
        run_reader: Optional[Callable[[str], TerminalRunView]] = None,
        usage_exists: Optional[Callable[[str], bool]] = None,
    ) -> None:
        if not callable(getattr(ledger, "record_usage", None)):
            raise ValueError("LedgerTraceObserver requires a RuntimeLedger sink")
        if catalog is not None and not isinstance(catalog, PricingCatalog):
            raise ValueError("catalog must be a PricingCatalog")
        if run_reader is not None and not callable(run_reader):
            raise ValueError("run_reader must be callable")
        if usage_exists is not None and not callable(usage_exists):
            raise ValueError("usage_exists must be callable")
        self._ledger = ledger
        self._catalog = catalog
        self._run_reader = run_reader
        self._usage_exists = usage_exists

    async def on_run_terminal(self, run_id: str) -> None:
        try:
            await self._mirror_terminal_run(run_id)
        except Exception:
            # Observability projection: a mirror failure must never
            # surface into the run; the v2 journal remains authoritative.
            logger.exception(
                "Failed to mirror terminal run into runtime ledger",
                extra={"run_id": run_id},
            )

    async def _mirror_terminal_run(self, run_id: str) -> None:
        if self._run_reader is None:
            return
        if self._usage_exists is not None and self._usage_exists(run_id):
            logger.info(
                "Runtime ledger already mirrors run; skipping",
                extra={"run_id": run_id},
            )
            return
        view = self._run_reader(run_id)
        if view.status not in _TERMINAL_STATUSES:
            logger.info(
                "Run is not terminal; skipping usage mirror",
                extra={"run_id": run_id, "status": view.status},
            )
            return
        for turn in view.turns:
            usage = CanonicalUsage(
                provider=turn.provider,
                model=turn.model,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                cached_input_tokens=0,
                reasoning_tokens=0,
                request_count=1,
                occurred_at=turn.occurred_at,
                trace=TraceContext(
                    trace_id=run_id,
                    run_id=run_id,
                    correlation_id=view.correlation_id,
                    span_id=turn.model_turn_id,
                    model_turn_id=turn.model_turn_id,
                ),
            )
            cost: Optional[CostRecord] = None
            if self._catalog is not None:
                cost = compute_cost(
                    usage,
                    self._catalog,
                    category=UsageCategory.PRIMARY,
                    usage_id=f"usage_{run_id}_{turn.model_turn_id}",
                )
            await self._ledger.record_usage(usage, cost=cost)
        logger.info(
            "Mirrored terminal run usage into runtime ledger",
            extra={"run_id": run_id, "turns": len(view.turns)},
        )


__all__ = [
    "MIRROR_TIMEOUT_SECONDS",
    "LedgerTraceObserver",
    "ModelTurnUsageView",
    "RunTraceObserver",
    "TerminalRunView",
    "default_run_reader",
    "default_usage_exists",
]
