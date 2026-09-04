"""Skill dependency validation (M5 SK-3).

07 §2.2 / §7 / SK-3: a skill's ``required-tools`` / ``required-capabilities``
declare what the skill needs; they never grant anything. A skill is
model-visible / locator-resolvable only when its dependencies are
satisfied by the runtime surface of the conversation:

- ``required-tools`` must exist in the conversation's available tools,
- ``required-capabilities`` must be granted to the conversation profile.

This module is vocabulary-neutral: it compares against frozensets the
composition root already resolved (workspace binding grant, tool
registry names), so ``skills`` never imports ``tool_platform``
(ADR-AP-2 single vocabulary lives at the caller). Explicit invocation of
a skill with missing dependencies returns an understandable error; the
runtime never auto-widens capabilities to satisfy a skill (07 §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

from .registry import SkillRevision


@dataclass(frozen=True)
class SkillDependencyContext:
    """Runtime surface a skill may rely on (per conversation)."""

    available_tools: frozenset[str] = frozenset()
    granted_capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.available_tools, frozenset):
            raise ValueError("available_tools must be a frozenset")
        if not isinstance(self.granted_capabilities, frozenset):
            raise ValueError("granted_capabilities must be a frozenset")


@dataclass(frozen=True)
class DependencyReport:
    """Dependency check outcome for one skill revision."""

    satisfied: bool
    missing_tools: Tuple[str, ...] = ()
    missing_capabilities: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_tools = tuple(sorted(set(self.missing_tools)))
        normalized_caps = tuple(sorted(set(self.missing_capabilities)))
        object.__setattr__(self, "missing_tools", normalized_tools)
        object.__setattr__(self, "missing_capabilities", normalized_caps)

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.missing_tools:
            parts.append("缺少工具：" + ", ".join(self.missing_tools))
        if self.missing_capabilities:
            parts.append("缺少能力：" + ", ".join(self.missing_capabilities))
        return "；".join(parts)


def check_skill_dependencies(
    revision: SkillRevision,
    context: SkillDependencyContext,
) -> DependencyReport:
    """Check one revision's declared dependencies against a runtime surface.

    A skill with no declared dependencies is always satisfied. Missing
    dependencies never widen the runtime surface; they just gate the skill
    (SK-3, 07 §2.2).
    """
    missing_tools = tuple(
        tool
        for tool in revision.required_tools
        if tool not in context.available_tools
    )
    missing_capabilities = tuple(
        capability
        for capability in revision.required_capabilities
        if capability not in context.granted_capabilities
    )
    return DependencyReport(
        satisfied=not missing_tools and not missing_capabilities,
        missing_tools=missing_tools,
        missing_capabilities=missing_capabilities,
    )


def dependency_satisfied_skills(
    revisions: Tuple[SkillRevision, ...],
    context: SkillDependencyContext,
) -> Tuple[SkillRevision, ...]:
    """Filter revisions to those whose dependencies are satisfied."""
    return tuple(
        revision
        for revision in revisions
        if check_skill_dependencies(revision, context).satisfied
    )


__all__ = [
    "DependencyReport",
    "SkillDependencyContext",
    "check_skill_dependencies",
    "dependency_satisfied_skills",
]
