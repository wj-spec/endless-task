"""Bounded projection of a child AgentKernel run into a ChildOutcome (M4A DR-0).

DR-0 acceptance keeps two properties explicit for every child result:

- **bounded**: the parent never receives the child transcript; only a
  projection is returned, and its fields respect the same caps the
  protocol enforces (``ChildOutcome.summary`` etc.),
- **schema/status controlled**: kernel statuses map to the delegation
  status vocabulary and diagnostics cross the boundary as
  :class:`SafeDiagnostic` only.

Findings/artifacts/effects stay empty at DR-0: a fake child executes no
tools, and the runtime-ledger wiring that turns real child effects into
receipts belongs to the coordinator/DR-1+ slices. Empty structured
fields are still protocol-valid and bounded by construction, so the
"result is bounded" gate holds without inventing unverified semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from endless_task.agent_kernel import (
    AgentMessageRole,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic

from .protocol import ChildOutcome, ChildStatus, UsageSummary

#: Summary bound reused from the protocol's own require_text cap so the
#: projection never widens what ChildOutcome already allows.
CHILD_SUMMARY_MAX_CHARACTERS = 20_000

#: Hard cap on the raw assistant text the projection will accept before
#: truncation. A full child deliverable larger than this is truncated to
#: the summary bound instead of being refused, because the child already
#: completed; the parent must still learn about the run outcome.
CHILD_DELIVERABLE_MAX_CHARACTERS = 200_000


@dataclass(frozen=True)
class ChildResultPolicy:
    """Data-driven caps for projecting one child result (DR-0 defaults).

    Defaults mirror the protocol's own field caps so no new magic number
    widens an existing bound. Real runtime settings are layered in when
    the coordinator is integrated (README 4.7: production defaults come
    from the running surface).
    """

    summary_max_characters: int = CHILD_SUMMARY_MAX_CHARACTERS
    deliverable_max_characters: int = CHILD_DELIVERABLE_MAX_CHARACTERS

    def __post_init__(self) -> None:
        if (
            not isinstance(self.summary_max_characters, int)
            or isinstance(self.summary_max_characters, bool)
            or self.summary_max_characters <= 0
        ):
            raise AgentPlatformError(
                "invalid_child_result_policy",
                "summary_max_characters must be a positive integer",
            )
        if (
            not isinstance(self.deliverable_max_characters, int)
            or isinstance(self.deliverable_max_characters, bool)
            or self.deliverable_max_characters < self.summary_max_characters
        ):
            raise AgentPlatformError(
                "invalid_child_result_policy",
                "deliverable_max_characters must not be smaller than the summary cap",
            )


def _map_status(status: RunOutcomeStatus) -> ChildStatus:
    """Map a kernel run status onto the delegation status vocabulary."""
    mapping = {
        RunOutcomeStatus.COMPLETED: ChildStatus.COMPLETED,
        RunOutcomeStatus.FAILED: ChildStatus.FAILED,
        RunOutcomeStatus.CANCELLED: ChildStatus.CANCELLED,
    }
    if status not in mapping:
        # interrupted has no delegation counterpart yet; it is a failure
        # the parent must notice, never an unknown that gets re-spawned.
        return ChildStatus.FAILED
    return mapping[status]


def _bounded_summary(
    text: str,
    policy: ChildResultPolicy,
    diagnostics: list[SafeDiagnostic],
) -> str:
    if len(text) <= policy.summary_max_characters:
        return text
    diagnostics.append(
        SafeDiagnostic(
            code="child_summary_truncated",
            safe_message=(
                "Child result summary exceeded the delegation cap and was truncated."
            ),
            details={
                "summary_max_characters": policy.summary_max_characters,
                "actual_characters": len(text),
            },
        )
    )
    return text[: policy.summary_max_characters]


def project_child_outcome(
    outcome: RunOutcome,
    *,
    policy: Optional[ChildResultPolicy] = None,
    usage: Optional[UsageSummary] = None,
) -> ChildOutcome:
    """Project one child kernel :class:`RunOutcome` into a :class:`ChildOutcome`.

    Pure and total: never raises for a completed/failed/cancelled child.
    The child run id is taken from the kernel outcome, so the projection
    cannot invent lineage.
    """
    if not isinstance(outcome, RunOutcome):
        raise AgentPlatformError(
            "invalid_delegation_value",
            "Child result projection requires a RunOutcome",
        )
    if policy is None:
        policy = ChildResultPolicy()
    if not isinstance(policy, ChildResultPolicy):
        raise AgentPlatformError(
            "invalid_delegation_value",
            "Child result projection requires a ChildResultPolicy",
        )
    diagnostics: list[SafeDiagnostic] = list(outcome.diagnostics)
    status = _map_status(outcome.status)
    if outcome.assistant_message is None:
        # Only non-completed outcomes can lack a message (the kernel
        # protocol requires one on completion); the summary stays bounded
        # and factual.
        summary = "Child run produced no final message."
    else:
        if outcome.assistant_message.role is not AgentMessageRole.ASSISTANT:
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child run assistant message must use the assistant role",
            )
        content = outcome.assistant_message.content
        if len(content) > policy.deliverable_max_characters:
            diagnostics.append(
                SafeDiagnostic(
                    code="child_deliverable_too_large",
                    safe_message=(
                        "Child deliverable exceeded the projection cap; only the "
                        "bounded summary is exposed to the parent."
                    ),
                    details={
                        "deliverable_max_characters": policy.deliverable_max_characters,
                        "actual_characters": len(content),
                    },
                )
            )
        summary = _bounded_summary(content, policy, diagnostics)
    return ChildOutcome(
        child_run_id=outcome.run_id,
        status=status,
        summary=summary,
        usage=usage if usage is not None else UsageSummary(),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "CHILD_DELIVERABLE_MAX_CHARACTERS",
    "CHILD_SUMMARY_MAX_CHARACTERS",
    "ChildResultPolicy",
    "project_child_outcome",
]
