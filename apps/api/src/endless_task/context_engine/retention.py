"""Tool-result retention planning for context assembly (M2 AP-203).

Pure planning over ``ContextSegment`` tool-result candidates, implementing
the doc 04 rules that need tool-result semantics (kept out of AP-202):

- hard rule 2 (04 §4.3): results of active (not yet answered) tool calls are
  paired with their call and are never pruned or spilled,
- old completed results are pruned oldest-first up to ``max_kept_results``
  (duplicates of the same source are deduplicated keeping the newest),
- hard rule 5: a single completed result larger than ``max_result_share`` of
  the input budget is marked ``spilled`` (the full content lives in a store;
  only the marker and reason enter the context plan),
- an active result that is over the share is still kept, with a diagnostic
  so the engine can react (e.g., fail the turn or ask for compaction).

Order of the input tuple is treated as recency ascending (oldest first), a
contract the future ``DefaultContextEngine`` must uphold when feeding
candidates. No runtime state is touched here.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic

from .protocol import (
    ContextSegment,
    ContextSegmentKind,
    ContextTransform,
)

RETENTION_SCHEMA_VERSION = 1
DEFAULT_MAX_RESULT_SHARE = 0.3


@dataclass(frozen=True)
class ToolResultRetention:
    """Retention decision for a batch of tool-result candidates."""

    kept: tuple[ContextSegment, ...]
    pruned: tuple[ContextSegment, ...]
    spilled: tuple[ContextSegment, ...]
    diagnostics: tuple[SafeDiagnostic, ...]
    schema_version: int = RETENTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in ("kept", "pruned", "spilled"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(segment, ContextSegment) for segment in values
            ):
                raise AgentPlatformError(
                    "invalid_retention_result",
                    f"{field_name} must be a tuple of ContextSegment values",
                )
        for field_name, required_transform in (
            ("kept", ContextTransform.INCLUDED),
            ("pruned", ContextTransform.PRUNED),
            ("spilled", ContextTransform.SPILLED),
        ):
            for segment in getattr(self, field_name):
                if segment.transform is not required_transform:
                    raise AgentPlatformError(
                        "invalid_retention_result",
                        f"{field_name} segments must carry transform {required_transform.value}",
                    )


def plan_tool_result_retention(
    results: Sequence[ContextSegment],
    *,
    active_call_ids: Collection[str],
    max_kept_results: int,
    input_budget: int,
    max_result_share: float = DEFAULT_MAX_RESULT_SHARE,
) -> ToolResultRetention:
    """Decide which tool results stay, get pruned or get spilled."""
    if isinstance(results, (str, bytes)):
        raise AgentPlatformError(
            "invalid_retention_input",
            "results must be a sequence of ContextSegment values",
        )
    result_tuple = tuple(results)
    if any(
        not isinstance(segment, ContextSegment)
        or segment.kind is not ContextSegmentKind.TOOL_RESULT
        for segment in result_tuple
    ):
        raise AgentPlatformError(
            "invalid_retention_input",
            "Retention expects TOOL_RESULT ContextSegment values",
        )
    if any(
        segment.transform is not ContextTransform.INCLUDED for segment in result_tuple
    ):
        raise AgentPlatformError(
            "invalid_retention_input",
            "Retention expects raw INCLUDED candidates only",
        )
    if isinstance(active_call_ids, (str, bytes)):
        raise AgentPlatformError(
            "invalid_retention_input",
            "active_call_ids must be a collection of identifiers",
        )
    try:
        normalized_active = frozenset(
            str(call_id) for call_id in active_call_ids
        )
    except TypeError as error:
        raise AgentPlatformError(
            "invalid_retention_input",
            "active_call_ids must be a collection of identifiers",
        ) from error
    if (
        not isinstance(max_kept_results, int)
        or isinstance(max_kept_results, bool)
        or max_kept_results <= 0
    ):
        raise AgentPlatformError(
            "invalid_retention_input",
            "max_kept_results must be a positive integer",
        )
    if not isinstance(input_budget, int) or input_budget <= 0:
        raise AgentPlatformError(
            "invalid_retention_input",
            "input_budget must be a positive integer",
        )
    if (
        isinstance(max_result_share, bool)
        or not isinstance(max_result_share, (int, float))
        or not 0 < float(max_result_share) <= 1
    ):
        raise AgentPlatformError(
            "invalid_retention_input",
            "max_result_share must be within (0, 1]",
        )

    def is_active(segment: ContextSegment) -> bool:
        return any(source_id in normalized_active for source_id in segment.source_ids)

    active: list[ContextSegment] = []
    completed: list[ContextSegment] = []
    diagnostics: list[SafeDiagnostic] = []
    for segment in result_tuple:
        (active if is_active(segment) else completed).append(segment)

    # Deduplicate completed results by source identity, keeping the newest
    # (the input is oldest-first, so the last occurrence wins).
    newest_by_source: dict[tuple[str, ...], ContextSegment] = {}
    for segment in completed:
        newest_by_source[segment.source_ids] = segment
    completed = list(newest_by_source.values())

    pruned_count = max(0, len(completed) - max_kept_results)
    completed_to_keep = completed[-max_kept_results:] if max_kept_results else ()
    pruned = [
        replace(segment, transform=ContextTransform.PRUNED, transform_reason="tool_result_pruned_old")
        for segment in completed[:pruned_count]
    ]

    share_limit = max(1, int(float(input_budget) * float(max_result_share)))
    kept: list[ContextSegment] = list(active)
    spilled: list[ContextSegment] = []
    for segment in active:
        if segment.estimated_tokens > share_limit:
            diagnostics.append(
                SafeDiagnostic(
                    code="active_tool_result_over_share",
                    safe_message=(
                        "活跃工具结果超过输入预算份额，已保留（成对完整性优先）；"
                        "可能导致后续轮次触发 spill/compact。"
                    ),
                    retryable=False,
                    details={
                        "source_ids": list(segment.source_ids),
                        "estimated_tokens": segment.estimated_tokens,
                        "share_limit": share_limit,
                    },
                )
            )
    for segment in completed_to_keep:
        if segment.estimated_tokens > share_limit:
            spilled.append(
                replace(
                    segment,
                    transform=ContextTransform.SPILLED,
                    transform_reason="tool_result_over_share",
                )
            )
        else:
            kept.append(segment)

    return ToolResultRetention(
        kept=tuple(kept),
        pruned=tuple(pruned),
        spilled=tuple(spilled),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "DEFAULT_MAX_RESULT_SHARE",
    "RETENTION_SCHEMA_VERSION",
    "ToolResultRetention",
    "plan_tool_result_retention",
]
