"""M2 AP-206 Stage A: entries -> context segments mapping + shadow report.

The runtime assembles messages from transcript entries
(``AgentRunExecutor.execute`` -> ``ContextProjection.project``). This module
implements the version-2 bookkeeping view of the SAME entries: a
provenance-preserving mapping to platform ``ContextSegment`` values with
deterministic token estimates, used for shadow parity (observation only)
until the cutover decides entry inclusion from the v2 plan.

Decisions recorded:
- segments are derived from entries (which keep provenance), never from
  assembled messages,
- an entry is skipped when its context policy says ``include_in_llm=false``
  (mirroring the legacy projection),
- kind mapping: SYSTEM_NOTICE->system, USER_MESSAGE->current_user (last
  included) else recent_history, ASSISTANT_MESSAGE/TOOL_CALL->recent_history,
  TOOL_RESULT->tool_result, PLAN->optional_instruction,
  CONTEXT_SUMMARY->checkpoint,
- token estimate uses the platform planner heuristic over the entry text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from endless_task.context_engine import (
    ContextSegment,
    ContextSegmentKind,
    ContextTransform,
    estimate_text_tokens,
)

from .domain import TranscriptEntryRecord, TranscriptEntryType

_SEGMENT_KIND_BY_TYPE: Mapping[TranscriptEntryType, ContextSegmentKind] = {
    TranscriptEntryType.SYSTEM_NOTICE: ContextSegmentKind.SYSTEM,
    TranscriptEntryType.TOOL_RESULT: ContextSegmentKind.TOOL_RESULT,
    TranscriptEntryType.PLAN: ContextSegmentKind.OPTIONAL_INSTRUCTION,
    TranscriptEntryType.CONTEXT_SUMMARY: ContextSegmentKind.CHECKPOINT,
    TranscriptEntryType.ARTIFACT_REF: ContextSegmentKind.ARTIFACT,
}
_PRIORITY_BY_KIND = {
    ContextSegmentKind.SYSTEM: 100,
    ContextSegmentKind.CURRENT_USER: 100,
    ContextSegmentKind.MEMORY: 60,
    ContextSegmentKind.RECENT_HISTORY: 40,
    ContextSegmentKind.OPTIONAL_INSTRUCTION: 30,
    ContextSegmentKind.TOOL_RESULT: 20,
    ContextSegmentKind.CHECKPOINT: 10,
}


@dataclass(frozen=True)
class ContextShadowReport:
    entry_count: int
    included_count: int
    estimated_tokens: int
    segments: tuple[ContextSegment, ...]


def _entry_text(entry: TranscriptEntryRecord) -> str:
    payload = entry.payload or {}
    content = payload.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        import json

        try:
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return str(content)


def _included_by_policy(entry: TranscriptEntryRecord) -> bool:
    policy = entry.context_policy or {}
    include = policy.get("include_in_llm", True)
    transform = policy.get("transform", "full")
    return isinstance(include, bool) and include and transform != "none"


def build_context_shadow(
    entries: tuple[TranscriptEntryRecord, ...],
) -> ContextShadowReport:
    """Map included transcript entries to platform segments (shadow only)."""
    if not isinstance(entries, tuple) or any(
        not isinstance(entry, TranscriptEntryRecord) for entry in entries
    ):
        raise TypeError("build_context_shadow expects TranscriptEntryRecord values")
    included_entries = [entry for entry in entries if _included_by_policy(entry)]
    segments: list[ContextSegment] = []
    last_included_index = len(included_entries) - 1
    for index, entry in enumerate(included_entries):
        kind = _SEGMENT_KIND_BY_TYPE.get(entry.type)
        if kind is None:
            if entry.type is TranscriptEntryType.USER_MESSAGE:
                kind = (
                    ContextSegmentKind.CURRENT_USER
                    if index == last_included_index
                    else ContextSegmentKind.RECENT_HISTORY
                )
            else:
                kind = ContextSegmentKind.RECENT_HISTORY
        text = _entry_text(entry)
        segments.append(
            ContextSegment(
                kind=kind,
                source_ids=(entry.id,),
                trust_level="transcript",
                priority=_PRIORITY_BY_KIND.get(kind, 20),
                estimated_tokens=estimate_text_tokens(text),
                transform=ContextTransform.INCLUDED,
            )
        )
    return ContextShadowReport(
        entry_count=len(entries),
        included_count=len(included_entries),
        estimated_tokens=sum(segment.estimated_tokens for segment in segments),
        segments=tuple(segments),
    )


__all__ = ["ContextShadowReport", "build_context_shadow"]
