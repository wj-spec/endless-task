"""Fallback output spill for the version 2 tool platform (AP-104).

The preferred spill path is the ``OutputSpillExtension`` on the ExtensionBus;
``spill_outcome`` is the pipeline-level backstop that guarantees a completed
tool outcome never exceeds ``max_output_characters`` even when no spill
extension is registered.
"""

from __future__ import annotations

from endless_task.agent_platform import AgentPlatformError, require_text

from .protocol import ToolOutcome, ToolOutcomeStatus

DEFAULT_SPILL_MARKER = "\n…[输出超长已截断]"


def spill_outcome(
    outcome: ToolOutcome,
    *,
    max_characters: int,
    spill_reference: str | None = None,
) -> ToolOutcome:
    if not isinstance(outcome, ToolOutcome):
        raise AgentPlatformError(
            "invalid_tool_outcome",
            "Spill requires a ToolOutcome",
        )
    if not isinstance(max_characters, int) or max_characters <= 0:
        raise AgentPlatformError(
            "invalid_spill_limit",
            "Spill max_characters must be a positive integer",
        )
    if spill_reference is not None:
        spill_reference = require_text(
            spill_reference,
            field_name="spill_reference",
            max_length=512,
        )
    if outcome.status is not ToolOutcomeStatus.COMPLETED:
        return outcome
    if len(outcome.content) <= max_characters:
        return outcome
    marker = (
        f"\n…[输出超长已截断：完整结果见 {spill_reference}]"
        if spill_reference
        else DEFAULT_SPILL_MARKER
    )
    return ToolOutcome(
        call_id=outcome.call_id,
        status=outcome.status,
        content=outcome.content[:max_characters] + marker,
        structured_content=outcome.structured_content,
        effects=outcome.effects,
        is_truncated=True,
        terminate=outcome.terminate,
    )


__all__ = ["DEFAULT_SPILL_MARKER", "spill_outcome"]
