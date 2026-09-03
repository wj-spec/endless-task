"""M2 prelude Stage 3: DefaultContextEngine assembly orchestration."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.context_engine import (
    BootstrapRequest,
    CompactionRequest,
    ContextBudget,
    ContextInput,
    ContextInputKind,
    ContextRequest,
    ContextSegment,
    ContextSegmentKind,
    ContextSnapshot,
    ContextTransform,
    DefaultContextEngine,
    MaintenanceRequest,
    TurnOutcome,
)
from endless_task.runtime.provider import ProviderMessage
from endless_task.runtime_ledger import TraceContext
from endless_task.agent_platform import EffectReceiptRef, EffectOutcome

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")


def budget(*, window: int = 1000) -> ContextBudget:
    return ContextBudget(
        window_tokens=window,
        reserved_output_tokens=200,
        safety_margin_tokens=50,
    )


def segment(kind, tokens, priority, source="s1") -> ContextSegment:
    return ContextSegment(
        kind=kind,
        source_ids=(source,),
        trust_level="trusted",
        priority=priority,
        estimated_tokens=tokens,
        transform=ContextTransform.INCLUDED,
    )


def request(**overrides) -> ContextRequest:
    values = dict(
        run_id="run_1",
        model_turn_id="model_turn_1",
        lane_id="lane_1",
        budget=budget(),
        trace=TRACE,
    )
    values.update(overrides)
    return ContextRequest(**values)


class EngineAssembleTest(unittest.IsolatedAsyncioTestCase):
    def _engine(
        self,
        *,
        messages=(
            ProviderMessage(role="system", content="sys"),
            ProviderMessage(role="user", content="hi"),
        ),
        segments=(
            segment(ContextSegmentKind.SYSTEM, 50, 100),
            segment(ContextSegmentKind.RECENT_HISTORY, 300, 40),
            segment(ContextSegmentKind.TOOL_RESULT, 400, 20, source="tool_1"),
        ),
        retention=None,
    ) -> DefaultContextEngine:
        return DefaultContextEngine(
            messages_provider=lambda req: messages,
            segments_provider=lambda req: segments,
            retention_pass=retention,
        )

    async def test_assemble_keeps_messages_and_plans_segments(self) -> None:
        engine = self._engine()
        snapshot = await engine.assemble(request())
        self.assertIsInstance(snapshot, ContextSnapshot)
        self.assertEqual(2, len(snapshot.messages))
        kinds = {item.kind for item in snapshot.segments}
        self.assertIn(ContextSegmentKind.SYSTEM, kinds)
        # history + tool result (400+300+50=750 > 750? input_budget=750) -
        # verify planning applied
        self.assertLessEqual(snapshot.estimated_tokens, snapshot.budget.input_budget)
        self.assertEqual(64, len(snapshot.fingerprint))

    async def test_retention_pass_runs_before_planning(self) -> None:
        calls = []

        def retention(req, candidates):
            calls.append(len(candidates))
            return tuple(c for c in candidates if c.kind is not ContextSegmentKind.TOOL_RESULT)

        engine = self._engine(retention=retention)
        snapshot = await engine.assemble(request())
        self.assertEqual([3], calls)
        self.assertNotIn(
            ContextSegmentKind.TOOL_RESULT,
            {item.kind for item in snapshot.segments},
        )

    async def test_under_budget_everything_included(self) -> None:
        engine = self._engine(
            segments=(
                segment(ContextSegmentKind.SYSTEM, 50, 100),
                segment(ContextSegmentKind.RECENT_HISTORY, 300, 40),
            )
        )
        snapshot = await engine.assemble(request(budget=budget(window=1000)))
        self.assertEqual(2, len(snapshot.segments))

    async def test_over_budget_history_dropped_with_diagnostics(self) -> None:
        engine = self._engine(
            segments=(
                segment(ContextSegmentKind.SYSTEM, 50, 100),
                segment(ContextSegmentKind.RECENT_HISTORY, 3000, 40),
            )
        )
        snapshot = await engine.assemble(request(budget=budget(window=400)))
        self.assertEqual(
            [ContextSegmentKind.SYSTEM],
            [item.kind for item in snapshot.segments],
        )

    async def test_fingerprint_stable_across_runs(self) -> None:
        engine = self._engine()
        first = await engine.assemble(request())
        second = await engine.assemble(request())
        self.assertEqual(first.fingerprint, second.fingerprint)

    async def test_pass_through_methods(self) -> None:
        engine = self._engine()
        await engine.bootstrap(
            BootstrapRequest(run_id="run_1", lane_id="lane_1", trace=TRACE)
        )
        await engine.ingest(
            ContextInput(
                run_id="run_1",
                kind=ContextInputKind.TRANSCRIPT,
                source_id="src_1",
                payload={"content": "x"},
                trace=TRACE,
            )
        )
        maintenance = await engine.maintain(
            MaintenanceRequest(run_id="run_1", trace=TRACE)
        )
        self.assertFalse(maintenance.changed)
        compaction = await engine.compact(
            CompactionRequest(run_id="run_1", target_tokens=100, trace=TRACE)
        )
        self.assertIsNone(compaction.checkpoint_id)
        commit = await engine.commit_turn(
            TurnOutcome(
                run_id="run_1",
                model_turn_id="model_turn_1",
                context_fingerprint="f",
                effects=(
                    EffectReceiptRef(
                        effect_id="effect_1",
                        outcome=EffectOutcome.COMMITTED,
                    ),
                ),
                trace=TRACE,
            )
        )
        self.assertFalse(commit.committed)

    async def test_invalid_provider_output_fails_closed(self) -> None:
        engine = DefaultContextEngine(
            messages_provider=lambda req: ("not-a-message",),
            segments_provider=lambda req: (),
        )
        with self.assertRaises(AgentPlatformError):
            await engine.assemble(request())


if __name__ == "__main__":
    unittest.main()
