"""Tool surface budgeting and discovery planning (AP-106).

When the full tool set of a conversation grows, injecting every schema into
the model costs tokens and hurts selection accuracy. ``plan_tool_surface``
decides whether the full surface fits the budget:

- under budget: every capability-filtered tool is injected,
- over budget: only the core tools and ``search_tools`` are injected and the
  rest becomes discoverable on demand,

without ever widening capability admission — the planner only re-partitions
an already-authorized :class:`ToolSurface`. If the surface is over budget but
``search_tools`` is not registered, planning fails closed instead of silently
dropping discovery.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Collection, Iterable

from endless_task.agent_platform import (
    AgentPlatformError,
    plain_json,
    require_identifier,
    require_protocol_version,
)

from .catalog import ToolRegistration, ToolSurface
from .protocol import ToolDefinitionV2

TOOL_SURFACE_SCHEMA_VERSION = 1

#: Documented heuristic: ~1 token per 3 characters for mixed ASCII/CJK text.
#: Exact token counts belong to provider-usage calibration (M2/M6); this
#: estimator is a monotone, deterministic planning signal only.
_CHARACTERS_PER_TOKEN = 3.0


def estimate_tool_schema_tokens(definition: ToolDefinitionV2) -> int:
    """Estimated model tokens to expose one tool definition."""
    if not isinstance(definition, ToolDefinitionV2):
        raise AgentPlatformError(
            "invalid_tool_definition",
            "Token estimate requires a ToolDefinitionV2",
        )
    try:
        schema_text = json.dumps(
            plain_json(definition.input_schema),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise AgentPlatformError(
            "invalid_tool_schema",
            "Tool schema must be JSON compatible",
        ) from error
    characters = len(definition.description) + len(schema_text)
    return max(1, math.ceil(characters / _CHARACTERS_PER_TOKEN))


def estimate_surface_tokens(
    registrations: Iterable[ToolRegistration],
) -> int:
    """Estimated model tokens for the whole registration surface."""
    total = 0
    for registration in registrations:
        if not isinstance(registration, ToolRegistration):
            raise AgentPlatformError(
                "invalid_tool_registration",
                "Surface estimate requires ToolRegistration values",
            )
        total += estimate_tool_schema_tokens(registration.definition)
    return total


@dataclass(frozen=True)
class SurfaceBudget:
    max_estimated_tokens: int
    core_tool_names: frozenset[str]
    search_tool_name: str
    schema_version: int = TOOL_SURFACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_SURFACE_SCHEMA_VERSION,
            protocol="tool_surface_budget",
        )
        if (
            not isinstance(self.max_estimated_tokens, int)
            or isinstance(self.max_estimated_tokens, bool)
            or self.max_estimated_tokens <= 0
        ):
            raise AgentPlatformError(
                "invalid_surface_budget",
                "Surface budget must be a positive integer of estimated tokens",
            )
        if isinstance(self.core_tool_names, (str, bytes)):
            raise AgentPlatformError(
                "invalid_surface_budget",
                "core_tool_names must be a collection of tool names",
            )
        try:
            normalized_core = frozenset(
                require_identifier(name, field_name="core_tool_name", max_length=64)
                for name in self.core_tool_names
            )
        except TypeError as error:
            raise AgentPlatformError(
                "invalid_surface_budget",
                "core_tool_names must be a collection of tool names",
            ) from error
        object.__setattr__(self, "core_tool_names", normalized_core)
        object.__setattr__(
            self,
            "search_tool_name",
            require_identifier(
                self.search_tool_name,
                field_name="search_tool_name",
                max_length=64,
            ),
        )


@dataclass(frozen=True)
class ToolSurfacePlan:
    """Deterministic partition of an authorized surface for one model turn."""

    inject_full: bool
    injected: tuple[ToolRegistration, ...]
    deferred: tuple[ToolRegistration, ...]
    search_tools_available: bool
    estimated_tokens: int
    over_budget: bool
    schema_version: int = TOOL_SURFACE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_SURFACE_SCHEMA_VERSION,
            protocol="tool_surface_plan",
        )
        if not isinstance(self.inject_full, bool) or not isinstance(
            self.over_budget, bool
        ):
            raise AgentPlatformError(
                "invalid_tool_surface_plan",
                "Surface plan flags must be boolean",
            )
        if not isinstance(self.search_tools_available, bool):
            raise AgentPlatformError(
                "invalid_tool_surface_plan",
                "search_tools_available must be boolean",
            )
        if not isinstance(self.estimated_tokens, int) or self.estimated_tokens < 0:
            raise AgentPlatformError(
                "invalid_tool_surface_plan",
                "Surface plan estimated tokens must be non-negative",
            )
        for field_name in ("injected", "deferred"):
            registrations = getattr(self, field_name)
            if not isinstance(registrations, tuple) or any(
                not isinstance(registration, ToolRegistration)
                for registration in registrations
            ):
                raise AgentPlatformError(
                    "invalid_tool_surface_plan",
                    f"{field_name} must be a tuple of ToolRegistration values",
                )
        injected_names = {item.definition.name for item in self.injected}
        deferred_names = {item.definition.name for item in self.deferred}
        duplicates = injected_names & deferred_names
        if duplicates:
            raise AgentPlatformError(
                "invalid_tool_surface_plan",
                "A tool cannot be both injected and deferred",
            )
        object.__setattr__(
            self,
            "injected",
            tuple(sorted(self.injected, key=lambda item: item.definition.name)),
        )
        object.__setattr__(
            self,
            "deferred",
            tuple(sorted(self.deferred, key=lambda item: item.definition.name)),
        )

    @property
    def injected_names(self) -> tuple[str, ...]:
        return tuple(item.definition.name for item in self.injected)

    @property
    def deferred_names(self) -> tuple[str, ...]:
        return tuple(item.definition.name for item in self.deferred)


def plan_tool_surface(
    surface: ToolSurface,
    budget: SurfaceBudget,
) -> ToolSurfacePlan:
    """Plan which tools a model turn should receive.

    Works on an already capability-filtered surface and never widens it.
    """
    if not isinstance(surface, ToolSurface):
        raise AgentPlatformError(
            "invalid_tool_surface",
            "Surface planning requires a ToolSurface",
        )
    if not isinstance(budget, SurfaceBudget):
        raise AgentPlatformError(
            "invalid_surface_budget",
            "Surface planning requires a SurfaceBudget",
        )
    registrations = surface.registrations
    estimated = estimate_surface_tokens(registrations)
    over_budget = estimated > budget.max_estimated_tokens
    search_names = tuple(
        registration
        for registration in registrations
        if registration.definition.name == budget.search_tool_name
    )
    if over_budget and not search_names:
        raise AgentPlatformError(
            "search_tools_unavailable",
            "Tool surface is over budget but search_tools is not registered",
            details={
                "estimated_tokens": estimated,
                "max_estimated_tokens": budget.max_estimated_tokens,
            },
        )
    if not over_budget:
        return ToolSurfacePlan(
            inject_full=True,
            injected=registrations,
            deferred=(),
            search_tools_available=bool(search_names),
            estimated_tokens=estimated,
            over_budget=False,
        )
    core_names = budget.core_tool_names | {budget.search_tool_name}
    injected = tuple(
        registration
        for registration in registrations
        if registration.definition.name in core_names
    )
    deferred = tuple(
        registration
        for registration in registrations
        if registration.definition.name not in core_names
    )
    return ToolSurfacePlan(
        inject_full=False,
        injected=injected,
        deferred=deferred,
        search_tools_available=True,
        estimated_tokens=estimated,
        over_budget=True,
    )


__all__ = [
    "SurfaceBudget",
    "TOOL_SURFACE_SCHEMA_VERSION",
    "ToolSurfacePlan",
    "estimate_surface_tokens",
    "estimate_tool_schema_tokens",
    "plan_tool_surface",
]
