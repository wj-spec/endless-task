"""Protocol receipt sink adapter: coordinator -> runtime ledger (RS-6 2b).

The tool coordinator emits protocol receipt *fields* (dict) + TraceContext
for every completed tool side effect (execution.py). This adapter builds
the frozen protocol :class:`EffectReceipt` and hands it to the injected
ledger's ``record_effect`` so side effects become safety-critical runtime
ledger events (OE-0 fail-closed semantics) — the missing production
caller of the ledger effect path.

The sink is best-effort *by design* at the coordinator call site, but
``record_effect`` itself keeps its fail-closed contract: once called, an
unpersisted safety record raises. The adapter therefore only forwards
receipts the tool actually marked completed; anything else is skipped
before reaching the ledger.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from endless_task.agent_platform import EffectOutcome
from endless_task.runtime_ledger.protocol import TraceContext


def build_record_effect_sink(
    ledger,
) -> Callable[[Mapping[str, Any], TraceContext], Awaitable[None]]:
    """Return an async sink(fields, trace) that records into ``ledger``.

    ``ledger`` must expose ``record_effect(receipt, trace)`` (the
    RuntimeLedger protocol). The sink constructs the protocol receipt from
    the coordinator's mapped fields.
    """
    if not callable(getattr(ledger, "record_effect", None)):
        raise ValueError("ledger must expose record_effect")

    from endless_task.agent_platform import EffectReceipt

    async def sink(fields: Mapping[str, Any], trace: TraceContext) -> None:
        outcome_value = fields.get("outcome")
        try:
            outcome = EffectOutcome(outcome_value)
        except ValueError:
            return  # unknown outcome value: skip rather than fabricate
        receipt = EffectReceipt(
            effect_id=str(fields.get("effect_id") or ""),
            tool_call_id=str(fields.get("tool_call_id") or ""),
            effect_type=str(fields.get("effect_type") or "unknown"),
            target=str(fields.get("target") or ""),
            started_at=str(fields.get("started_at") or ""),
            outcome=outcome,
            backend=str(fields.get("backend") or "workspace_tool"),
            safe_summary=str(fields.get("safe_summary") or ""),
            committed_at=fields.get("committed_at"),
            idempotency_key=str(fields.get("effect_id") or ""),
        )
        await ledger.record_effect(receipt, trace)

    return sink


__all__ = ["build_record_effect_sink"]
