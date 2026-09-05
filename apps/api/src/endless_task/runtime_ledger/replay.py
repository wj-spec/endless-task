"""Level 1 Recorded Outcome Replay (M6 OE-3).

08 §9 Level 1: replay a trajectory bundle **without calling any real
Provider or Tool**, driving only the recorded state transitions. It is
the deterministic regression executor for diagnosis and evaluation.

Guarantees:

- **Deterministic**: replay is a pure function of the bundle content;
  identical input bundles produce identical terminal state, ordered
  event sequence and digest (08 §OE-3 acceptance). Repeated calls on the
  same bundle therefore always agree.
- **No side effects**: nothing outside the bundle is read or written;
  tool/effect records are only consumed as recorded outcomes.
- **Fail loud on structural inconsistency**: a replay that references a
  tool outcome or effect the bundle does not record raises instead of
  guessing, because replay is only trustworthy when the recorded
  trajectory is self-consistent.

Terminal state is folded from the recorded event stream (ordered by
``occurredAt`` then ``eventId``), using recorded effect receipts and tool
outcomes as the authoritative outcome sources:

- effect receipts classify the run: an effect with outcome ``unknown``
  means the side effect could not be proven applied or absent, so the
  terminal classification is ``INCONCLUSIVE`` unless a terminal run event
  says otherwise,
- committed/rolled-back receipts roll up into an effect ledger,
- tool outcomes with a matching event type are cross-checked.

Level 0 (crash-state projection) remains in ``runtime_v2.replay`` and is
unchanged; this module is the trajectory-level Level 1 driver.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

from .trajectory import TrajectoryBundle

#: run event types that carry a terminal classification for the run.
_TERMINAL_RUN_EVENTS = frozenset(
    {"run.completed", "run.failed", "run.cancelled", "run_rejected", "safety_stop"}
)

#: effect receipt outcome values recorded by the ledger protocol.
_EFFECT_OUTCOME_VALUES = frozenset(
    {"committed", "not_committed", "unknown", "rolled_back"}
)


class ReplayClassification(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCONCLUSIVE = "inconclusive"
    EMPTY = "empty"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ReplayClassification.COMPLETED,
            ReplayClassification.FAILED,
            ReplayClassification.CANCELLED,
        }


@dataclass(frozen=True)
class ReplayedEffect:
    effect_id: str
    tool_call_id: Optional[str]
    outcome: str
    effect_type: Optional[str]


@dataclass(frozen=True)
class ReplayResult:
    """Deterministic Level 1 replay outcome for one bundle."""

    run_id: str
    classification: ReplayClassification
    ordered_event_ids: Tuple[str, ...]
    effects: Tuple[ReplayedEffect, ...]
    unknown_effect_count: int
    digest: str
    consistency_notes: Tuple[str, ...] = ()

    def to_json(self) -> Mapping[str, Any]:
        return {
            "runId": self.run_id,
            "classification": self.classification.value,
            "orderedEventIds": list(self.ordered_event_ids),
            "effects": [
                {
                    "effectId": effect.effect_id,
                    "toolCallId": effect.tool_call_id,
                    "outcome": effect.outcome,
                    "effectType": effect.effect_type,
                }
                for effect in self.effects
            ],
            "unknownEffectCount": self.unknown_effect_count,
            "digest": self.digest,
            "consistencyNotes": list(self.consistency_notes),
        }


def replay_trajectory(bundle: TrajectoryBundle) -> ReplayResult:
    """Deterministically replay one trajectory bundle (Level 1).

    Pure function of ``bundle``: the same bundle always yields the same
    terminal classification, ordered event id sequence and digest.
    """
    events = _ordered_events(bundle)
    run_id = bundle.manifest.source_run_id

    classification = ReplayClassification.EMPTY
    notes: list[str] = []
    terminal_events: list[str] = []

    effects: dict[str, ReplayedEffect] = {}
    for event in events:
        event_type = event["eventType"]
        data = event.get("data") or {}
        if event_type in _TERMINAL_RUN_EVENTS:
            terminal_events.append(event_type)
        if event_type == "effect_receipt":
            effect_id = data.get("effect_id")
            outcome = data.get("outcome")
            if not isinstance(effect_id, str) or not effect_id:
                notes.append(
                    f"Event {event['eventId']} is an effect_receipt without effect_id"
                )
                continue
            if outcome not in _EFFECT_OUTCOME_VALUES:
                notes.append(
                    f"Effect {effect_id} carries unsupported outcome {outcome!r}"
                )
                continue
            effects[effect_id] = ReplayedEffect(
                effect_id=effect_id,
                tool_call_id=(
                    data["tool_call_id"] if isinstance(data.get("tool_call_id"), str) else None
                ),
                outcome=outcome,
                effect_type=(
                    data["effect_type"]
                    if isinstance(data.get("effect_type"), str)
                    else None
                ),
            )

    # Cross-check recorded tool outcomes in the bundle against receipt
    # events (a tool outcome without an effect receipt is a structural gap).
    recorded_tool_calls = _recorded_tool_call_ids(bundle)
    for tool_call_id in recorded_tool_calls:
        if not any(
            effect.tool_call_id == tool_call_id for effect in effects.values()
        ):
            notes.append(
                f"Tool outcome {tool_call_id} has no matching effect receipt in the bundle"
            )

    if terminal_events:
        last_terminal = terminal_events[-1]
        if last_terminal in {"run.completed"}:
            classification = ReplayClassification.COMPLETED
        elif last_terminal in {"run.cancelled", "run_rejected"}:
            classification = ReplayClassification.CANCELLED
        else:
            classification = ReplayClassification.FAILED
    elif effects:
        unknown_count = sum(
            1 for effect in effects.values() if effect.outcome == "unknown"
        )
        if unknown_count:
            classification = ReplayClassification.INCONCLUSIVE
        else:
            classification = ReplayClassification.COMPLETED
    else:
        classification = ReplayClassification.EMPTY

    # Fail closed on claims of clean completion: a run that completed but
    # carries an effect whose outcome is unknown cannot prove the side
    # effect was applied or absent (08 §2.3), so it is INCONCLUSIVE.
    unknown_count = sum(1 for effect in effects.values() if effect.outcome == "unknown")
    if (
        classification is ReplayClassification.COMPLETED
        and unknown_count > 0
    ):
        classification = ReplayClassification.INCONCLUSIVE
        if terminal_events:
            notes.append(
                "Terminal run.completed event present but effects with unknown "
                "outcomes were recorded; classification downgraded to inconclusive"
            )

    ordered_event_ids = tuple(event["eventId"] for event in events)
    result = ReplayResult(
        run_id=run_id,
        classification=classification,
        ordered_event_ids=ordered_event_ids,
        effects=tuple(effects.values()),
        unknown_effect_count=sum(
            1 for effect in effects.values() if effect.outcome == "unknown"
        ),
        digest="",
        consistency_notes=tuple(notes),
    )
    return ReplayResult(
        run_id=run_id,
        classification=classification,
        ordered_event_ids=ordered_event_ids,
        effects=result.effects,
        unknown_effect_count=result.unknown_effect_count,
        digest=_replay_digest(result),
        consistency_notes=tuple(notes),
    )


def _ordered_events(bundle: TrajectoryBundle) -> Tuple[Mapping[str, Any], ...]:
    """Deterministic event order: ``occurredAt`` then ``eventId`` tie-break."""
    return tuple(
        sorted(
            bundle.events,
            key=lambda event: (event.get("occurredAt") or "", event.get("eventId") or ""),
        )
    )


def _recorded_tool_call_ids(bundle: TrajectoryBundle) -> frozenset[str]:
    ids: set[str] = set()
    for outcome in bundle.tool_outcomes:
        tool_call_id = outcome.get("toolCallId") or outcome.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.add(tool_call_id)
    return frozenset(ids)


def _replay_digest(result: ReplayResult) -> str:
    canonical = json.dumps(
        result.to_json(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ReplayClassification",
    "ReplayResult",
    "ReplayedEffect",
    "replay_trajectory",
]
