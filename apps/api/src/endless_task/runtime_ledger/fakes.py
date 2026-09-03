from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from endless_task.agent_platform import EffectReceipt, SafeDiagnostic

from .protocol import (
    CanonicalUsage,
    RuntimeLedgerEvent,
    SpanSpec,
    SpanStatus,
    TraceContext,
)


@dataclass
class FakeSpanHandle:
    context: TraceContext
    completions: list[tuple[SpanStatus, str, float, Optional[SafeDiagnostic]]] = field(
        default_factory=list
    )

    async def end(
        self,
        status: SpanStatus,
        *,
        ended_at: str,
        monotonic_ended: float,
        diagnostic: Optional[SafeDiagnostic] = None,
    ) -> None:
        self.completions.append((status, ended_at, monotonic_ended, diagnostic))


@dataclass
class FakeRuntimeLedger:
    events: list[RuntimeLedgerEvent] = field(default_factory=list)
    spans: list[tuple[SpanSpec, FakeSpanHandle]] = field(default_factory=list)
    effects: list[tuple[EffectReceipt, TraceContext]] = field(default_factory=list)
    usage: list[CanonicalUsage] = field(default_factory=list)

    async def append_event(self, event: RuntimeLedgerEvent) -> None:
        self.events.append(event)

    def start_span(self, spec: SpanSpec) -> FakeSpanHandle:
        context = TraceContext(
            trace_id=spec.trace.trace_id,
            run_id=spec.trace.run_id,
            correlation_id=spec.trace.correlation_id,
            span_id=spec.trace.span_id,
            parent_span_id=spec.trace.parent_span_id,
            model_turn_id=spec.trace.model_turn_id,
            tool_execution_id=spec.trace.tool_execution_id,
            child_run_id=spec.trace.child_run_id,
        )
        handle = FakeSpanHandle(context=context)
        self.spans.append((spec, handle))
        return handle

    async def record_effect(self, receipt: EffectReceipt, trace: TraceContext) -> None:
        self.effects.append((receipt, trace))

    async def record_usage(self, usage: CanonicalUsage) -> None:
        self.usage.append(usage)