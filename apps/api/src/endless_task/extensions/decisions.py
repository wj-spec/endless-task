from __future__ import annotations

from collections.abc import Iterable

from endless_task.agent_platform import AgentPlatformError

from .protocol import ExtensionDecision, ExtensionDecisionKind

_PRECEDENCE = {
    ExtensionDecisionKind.ALLOW: 0,
    ExtensionDecisionKind.REPLACE_ARGUMENTS: 1,
    ExtensionDecisionKind.ASK: 2,
    ExtensionDecisionKind.DENY: 3,
    ExtensionDecisionKind.ACCEPT: 0,
    ExtensionDecisionKind.REPLACE_RESULT: 1,
    ExtensionDecisionKind.BLOCK_RESULT: 2,
}
_PRE_TOOL = {
    ExtensionDecisionKind.ALLOW,
    ExtensionDecisionKind.REPLACE_ARGUMENTS,
    ExtensionDecisionKind.ASK,
    ExtensionDecisionKind.DENY,
}
_POST_TOOL = {
    ExtensionDecisionKind.ACCEPT,
    ExtensionDecisionKind.REPLACE_RESULT,
    ExtensionDecisionKind.BLOCK_RESULT,
}


def merge_pre_tool_decisions(
    decisions: Iterable[ExtensionDecision],
) -> ExtensionDecision | None:
    return _merge(tuple(decisions), allowed=_PRE_TOOL)


def merge_post_tool_decisions(
    decisions: Iterable[ExtensionDecision],
) -> ExtensionDecision | None:
    return _merge(tuple(decisions), allowed=_POST_TOOL)


def _merge(
    decisions: tuple[ExtensionDecision, ...],
    *,
    allowed: set[ExtensionDecisionKind],
) -> ExtensionDecision | None:
    if not decisions:
        return None
    invalid = [decision.kind.value for decision in decisions if decision.kind not in allowed]
    if invalid:
        raise AgentPlatformError(
            "invalid_extension_decision_stage",
            "Extension decision is not valid for this hook stage",
            details={"decisionKinds": invalid},
        )
    return max(
        enumerate(decisions),
        key=lambda item: (_PRECEDENCE[item[1].kind], -item[0]),
    )[1]