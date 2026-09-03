from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)

from .profiles import CapabilityContext, normalize_capabilities
from .protocol import AgentToolV2, ToolDefinitionV2, ToolEffect

TOOL_CATALOG_SCHEMA_VERSION = 1
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolScope(str, Enum):
    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    AGENT_RUN = "agent_run"


_SCOPE_PRIORITY = {
    ToolScope.BUILTIN: 0,
    ToolScope.USER: 1,
    ToolScope.WORKSPACE: 2,
    ToolScope.CONVERSATION: 3,
    ToolScope.AGENT_RUN: 4,
}


class ToolProvenanceKind(str, Enum):
    BUILTIN = "builtin"
    USER = "user"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    AGENT_RUN = "agent_run"
    MCP = "mcp"
    LEGACY_ADAPTER = "legacy_adapter"


@dataclass(frozen=True)
class ToolProvenance:
    kind: ToolProvenanceKind
    source_id: str
    revision: Optional[str] = None
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_provenance",
        )
        if not isinstance(self.kind, ToolProvenanceKind):
            raise AgentPlatformError(
                "invalid_tool_provenance",
                "Tool provenance kind must use a protocol enum value",
            )
        object.__setattr__(
            self,
            "source_id",
            require_identifier(self.source_id, field_name="source_id"),
        )
        if self.revision is not None:
            object.__setattr__(
                self,
                "revision",
                require_identifier(self.revision, field_name="revision"),
            )


@dataclass(frozen=True)
class ToolRegistration:
    registration_id: str
    tool: AgentToolV2
    scope: ToolScope
    provenance: ToolProvenance
    generation: int
    scope_id: Optional[str] = None
    override_declared: bool = False
    replaced_registration_id: Optional[str] = None
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_registration",
        )
        object.__setattr__(
            self,
            "registration_id",
            require_identifier(
                self.registration_id,
                field_name="registration_id",
            ),
        )
        _validate_tool(self.tool)
        if not isinstance(self.scope, ToolScope):
            raise AgentPlatformError(
                "invalid_tool_scope",
                "Tool scope must use a protocol enum value",
            )
        object.__setattr__(
            self,
            "scope_id",
            _validate_scope_id(self.scope, self.scope_id),
        )
        if not isinstance(self.provenance, ToolProvenance):
            raise AgentPlatformError(
                "invalid_tool_provenance",
                "Tool registration requires provenance",
            )
        if not isinstance(self.generation, int) or self.generation <= 0:
            raise AgentPlatformError(
                "invalid_catalog_generation",
                "Catalog generation must be a positive integer",
            )
        if not isinstance(self.override_declared, bool):
            raise AgentPlatformError(
                "invalid_tool_override",
                "Tool override flag must be boolean",
            )
        if self.replaced_registration_id is not None:
            object.__setattr__(
                self,
                "replaced_registration_id",
                require_identifier(
                    self.replaced_registration_id,
                    field_name="replaced_registration_id",
                ),
            )

    @property
    def definition(self) -> ToolDefinitionV2:
        return self.tool.definition


@dataclass(frozen=True)
class ToolSurfaceRequest:
    context: CapabilityContext
    allowlist: Optional[frozenset[str]] = None
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_surface_request",
        )
        if not isinstance(self.context, CapabilityContext):
            raise AgentPlatformError(
                "invalid_tool_surface_request",
                "Tool surface request requires a capability context",
            )
        if self.allowlist is not None:
            if isinstance(self.allowlist, (str, bytes)):
                raise AgentPlatformError(
                    "invalid_tool_allowlist",
                    "Tool allowlist must be a collection of names",
                )
            try:
                normalized = frozenset(
                    _validate_tool_name(name) for name in self.allowlist
                )
            except TypeError as error:
                raise AgentPlatformError(
                    "invalid_tool_allowlist",
                    "Tool allowlist must be a collection of names",
                ) from error
            object.__setattr__(self, "allowlist", normalized)


@dataclass(frozen=True)
class ToolSurface:
    registrations: tuple[ToolRegistration, ...]
    catalog_generation: int
    profile_name: str
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_surface",
        )
        if not isinstance(self.catalog_generation, int) or self.catalog_generation < 0:
            raise AgentPlatformError(
                "invalid_catalog_generation",
                "Catalog generation must be a non-negative integer",
            )
        object.__setattr__(
            self,
            "profile_name",
            require_identifier(self.profile_name, field_name="profile_name"),
        )
        ordered = tuple(sorted(self.registrations, key=lambda item: item.definition.name))
        if len({item.definition.name for item in ordered}) != len(ordered):
            raise AgentPlatformError(
                "invalid_tool_surface",
                "Tool surface cannot contain duplicate names",
            )
        object.__setattr__(self, "registrations", ordered)

    @property
    def definitions(self) -> tuple[ToolDefinitionV2, ...]:
        return tuple(item.definition for item in self.registrations)


@dataclass(frozen=True)
class ToolSummary:
    name: str
    description: str
    required_capabilities: frozenset[str]
    scope: ToolScope
    provenance: ToolProvenance
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_summary",
        )
        object.__setattr__(self, "name", _validate_tool_name(self.name))
        object.__setattr__(
            self,
            "description",
            require_text(
                self.description,
                field_name="tool_description",
                max_length=1_024,
            ),
        )
        object.__setattr__(
            self,
            "required_capabilities",
            normalize_capabilities(
                self.required_capabilities,
                field_name="required_capability",
            ),
        )
        if not isinstance(self.scope, ToolScope):
            raise AgentPlatformError(
                "invalid_tool_scope",
                "Tool scope must use a protocol enum value",
            )
        if not isinstance(self.provenance, ToolProvenance):
            raise AgentPlatformError(
                "invalid_tool_provenance",
                "Tool summary requires provenance",
            )


@dataclass(frozen=True)
class ToolSearchQuery:
    context: CapabilityContext
    query: str
    limit: int = 20
    allowlist: Optional[frozenset[str]] = None
    schema_version: int = TOOL_CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=TOOL_CATALOG_SCHEMA_VERSION,
            protocol="tool_search_query",
        )
        if not isinstance(self.context, CapabilityContext):
            raise AgentPlatformError(
                "invalid_tool_search_query",
                "Tool search query requires a capability context",
            )
        object.__setattr__(
            self,
            "query",
            require_text(self.query, field_name="tool_query", max_length=512),
        )
        if not isinstance(self.limit, int) or not 1 <= self.limit <= 100:
            raise AgentPlatformError(
                "invalid_tool_search_limit",
                "Tool search limit must be between 1 and 100",
            )
        if self.allowlist is not None:
            surface_request = ToolSurfaceRequest(
                context=self.context,
                allowlist=self.allowlist,
            )
            object.__setattr__(self, "allowlist", surface_request.allowlist)


class ToolCatalog(Protocol):
    @property
    def generation(self) -> int: ...

    def register(
        self,
        tool: AgentToolV2,
        *,
        scope: ToolScope,
        provenance: ToolProvenance,
        scope_id: Optional[str] = None,
        override: bool = False,
    ) -> ToolRegistration: ...

    def replace_provenance(
        self,
        *,
        kind: ToolProvenanceKind,
        source_id: str,
        tools: Iterable[AgentToolV2],
        scope: ToolScope,
        scope_id: str,
        revision: Optional[str] = None,
        override: bool = False,
    ) -> tuple[ToolRegistration, ...]: ...

    def resolve(self, name: str, context: CapabilityContext) -> AgentToolV2: ...

    def surface(self, request: ToolSurfaceRequest) -> ToolSurface: ...

    def search(self, query: ToolSearchQuery) -> tuple[ToolSummary, ...]: ...


class InMemoryToolCatalog:
    def __init__(self) -> None:
        self._generation = 0
        self._id_counter = 0
        self._registrations: dict[
            tuple[str, ToolScope, Optional[str]], ToolRegistration
        ] = {}
        self._history: list[ToolRegistration] = []

    @property
    def generation(self) -> int:
        return self._generation

    def register(
        self,
        tool: AgentToolV2,
        *,
        scope: ToolScope,
        provenance: ToolProvenance,
        scope_id: Optional[str] = None,
        override: bool = False,
    ) -> ToolRegistration:
        _validate_tool(tool)
        if not isinstance(scope, ToolScope):
            raise AgentPlatformError(
                "invalid_tool_scope",
                "Tool scope must use a protocol enum value",
            )
        normalized_scope_id = _validate_scope_id(scope, scope_id)
        if not isinstance(provenance, ToolProvenance):
            raise AgentPlatformError(
                "invalid_tool_provenance",
                "Tool registration requires provenance",
            )
        if not isinstance(override, bool):
            raise AgentPlatformError(
                "invalid_tool_override",
                "Tool override flag must be boolean",
            )

        name = _validate_tool_name(tool.definition.name)
        required = normalize_capabilities(
            tool.definition.required_capabilities,
            field_name="required_capability",
        )
        if tool.definition.effect is not ToolEffect.READ_ONLY and not required:
            raise AgentPlatformError(
                "missing_tool_capability",
                "Effectful tools must declare at least one required capability",
            )

        slot = (name, scope, normalized_scope_id)
        current = self._registrations.get(slot)
        same_name = tuple(
            registration
            for registration in self._registrations.values()
            if registration.definition.name == name and registration is not current
        )
        if current is not None and not override:
            raise AgentPlatformError(
                "duplicate_tool_registration",
                f"Tool is already registered in this scope: {name}",
            )
        self._validate_cross_scope_override(
            name=name,
            scope=scope,
            override=override,
            registrations=same_name,
        )

        generation = self._generation + 1
        self._id_counter += 1
        registration = ToolRegistration(
            registration_id=f"tool_registration_{self._id_counter}",
            tool=tool,
            scope=scope,
            scope_id=normalized_scope_id,
            provenance=provenance,
            generation=generation,
            override_declared=override,
            replaced_registration_id=(
                current.registration_id if current is not None else None
            ),
        )
        self._registrations[slot] = registration
        self._history.append(registration)
        self._generation = generation
        return registration

    def replace_provenance(
        self,
        *,
        kind: ToolProvenanceKind,
        source_id: str,
        tools: Iterable[AgentToolV2],
        scope: ToolScope,
        scope_id: str,
        revision: Optional[str] = None,
        override: bool = False,
    ) -> tuple[ToolRegistration, ...]:
        """Atomically replace every registration of one provenance source.

        AP-105b: MCP servers refresh their tool namespace dynamically; this
        removes all current registrations whose provenance matches
        ``kind``/``source_id`` and registers ``tools`` in one generation
        commit. Any validation failure (duplicate, capability or scope
        conflict) leaves the catalog untouched.
        """
        if not isinstance(kind, ToolProvenanceKind):
            raise AgentPlatformError(
                "invalid_tool_provenance",
                "Namespace replacement requires a provenance kind",
            )
        normalized_source = require_identifier(source_id, field_name="source_id")
        if scope is ToolScope.BUILTIN:
            raise AgentPlatformError(
                "invalid_tool_scope",
                "Namespace replacement requires a non-builtin scope",
            )
        normalized_scope_id = require_identifier(scope_id, field_name="scope_id")
        if not isinstance(override, bool):
            raise AgentPlatformError(
                "invalid_tool_override",
                "Namespace override flag must be boolean",
            )
        try:
            replacement_tools = tuple(tools)
        except TypeError as error:
            raise AgentPlatformError(
                "invalid_namespace_tools",
                "Namespace replacement requires a collection of AgentToolV2 values",
            ) from error
        if any(not isinstance(getattr(tool, "definition", None), ToolDefinitionV2) for tool in replacement_tools):
            raise AgentPlatformError(
                "invalid_namespace_tools",
                "Namespace replacement requires AgentToolV2 values",
            )
        normalized_revision: Optional[str] = None
        if revision is not None:
            normalized_revision = require_identifier(revision, field_name="revision")

        # Phase 1: validate the prospective state without mutating.
        prospective = dict(self._registrations)
        replaced_by_slot: dict[tuple[str, ToolScope, Optional[str]], str] = {}
        for slot, registration in list(prospective.items()):
            if (
                registration.provenance.kind is kind
                and registration.provenance.source_id == normalized_source
            ):
                replaced_by_slot[slot] = registration.registration_id
                del prospective[slot]
        validated: list[tuple[AgentToolV2, Optional[str]]] = []
        for tool in replacement_tools:
            definition = tool.definition
            name = _validate_tool_name(definition.name)
            required = normalize_capabilities(
                definition.required_capabilities,
                field_name="required_capability",
            )
            if definition.effect is not ToolEffect.READ_ONLY and not required:
                raise AgentPlatformError(
                    "missing_tool_capability",
                    "Effectful tools must declare at least one required capability",
                )
            slot = (name, scope, normalized_scope_id)
            current = prospective.get(slot)
            if current is not None and not override:
                raise AgentPlatformError(
                    "duplicate_tool_registration",
                    f"Tool is already registered in this scope: {name}",
                )
            same_name = tuple(
                registration
                for registration in prospective.values()
                if registration.definition.name == name
                and registration is not current
            )
            self._validate_cross_scope_override(
                name=name,
                scope=scope,
                override=override,
                registrations=same_name,
            )
            replaced_id = replaced_by_slot.get(slot)
            if replaced_id is None and current is not None:
                replaced_id = current.registration_id
            validated.append((tool, replaced_id))

        # Phase 2: single-generation atomic commit.
        generation = self._generation + 1
        added: list[ToolRegistration] = []
        for tool, replaced_registration_id in validated:
            definition = tool.definition
            name = definition.name
            self._id_counter += 1
            registration = ToolRegistration(
                registration_id=f"tool_registration_{self._id_counter}",
                tool=tool,
                scope=scope,
                scope_id=normalized_scope_id,
                provenance=ToolProvenance(
                    kind=kind,
                    source_id=normalized_source,
                    revision=normalized_revision,
                ),
                generation=generation,
                override_declared=override,
                replaced_registration_id=replaced_registration_id,
            )
            prospective[(name, scope, normalized_scope_id)] = registration
            added.append(registration)
        self._registrations = prospective
        self._history.extend(added)
        self._generation = generation
        return tuple(added)

    def resolve(self, name: str, context: CapabilityContext) -> AgentToolV2:
        normalized = _validate_tool_name(name)
        if not isinstance(context, CapabilityContext):
            raise AgentPlatformError(
                "invalid_capability_context",
                "Tool resolution requires a capability context",
            )
        registration = self._selected_registrations(context).get(normalized)
        if registration is None or not context.allows(
            registration.definition.required_capabilities
        ):
            raise AgentPlatformError(
                "tool_unavailable",
                "Tool is unavailable in this context",
            )
        return registration.tool

    def surface(self, request: ToolSurfaceRequest) -> ToolSurface:
        if not isinstance(request, ToolSurfaceRequest):
            raise AgentPlatformError(
                "invalid_tool_surface_request",
                "Catalog surface requires a ToolSurfaceRequest",
            )
        selected = self._selected_registrations(request.context)
        registrations = tuple(
            registration
            for name, registration in selected.items()
            if (request.allowlist is None or name in request.allowlist)
            and request.context.allows(
                registration.definition.required_capabilities
            )
        )
        return ToolSurface(
            registrations=registrations,
            catalog_generation=self._generation,
            profile_name=request.context.profile.name,
        )

    def search(self, query: ToolSearchQuery) -> tuple[ToolSummary, ...]:
        if not isinstance(query, ToolSearchQuery):
            raise AgentPlatformError(
                "invalid_tool_search_query",
                "Catalog search requires a ToolSearchQuery",
            )
        surface = self.surface(
            ToolSurfaceRequest(
                context=query.context,
                allowlist=query.allowlist,
            )
        )
        terms = tuple(part for part in query.query.casefold().split() if part)
        ranked: list[tuple[int, ToolSummary]] = []
        for registration in surface.registrations:
            definition = registration.definition
            searchable_name = definition.name.replace("_", " ").casefold()
            searchable = f"{searchable_name} {definition.description.casefold()}"
            if not all(term in searchable for term in terms):
                continue
            score = sum(searchable.count(term) for term in terms)
            if query.query.casefold() == definition.name.casefold():
                score += 100
            elif searchable_name.startswith(query.query.casefold()):
                score += 10
            ranked.append(
                (
                    score,
                    ToolSummary(
                        name=definition.name,
                        description=definition.description,
                        required_capabilities=definition.required_capabilities,
                        scope=registration.scope,
                        provenance=registration.provenance,
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1].name))
        return tuple(summary for _, summary in ranked[: query.limit])

    def registrations(self) -> tuple[ToolRegistration, ...]:
        return tuple(
            sorted(
                self._registrations.values(),
                key=lambda item: (
                    item.definition.name,
                    _SCOPE_PRIORITY[item.scope],
                    item.scope_id or "",
                ),
            )
        )

    def history(self) -> tuple[ToolRegistration, ...]:
        return tuple(self._history)

    @staticmethod
    def _validate_cross_scope_override(
        *,
        name: str,
        scope: ToolScope,
        override: bool,
        registrations: Iterable[ToolRegistration],
    ) -> None:
        priority = _SCOPE_PRIORITY[scope]
        for registration in registrations:
            existing_priority = _SCOPE_PRIORITY[registration.scope]
            if existing_priority == priority:
                continue
            if priority > existing_priority and not override:
                raise AgentPlatformError(
                    "explicit_tool_override_required",
                    f"Higher-scope tool registration must declare override: {name}",
                )
            if priority < existing_priority and not registration.override_declared:
                raise AgentPlatformError(
                    "explicit_tool_override_required",
                    f"Existing higher-scope tool registration lacks override: {name}",
                )

    def _selected_registrations(
        self,
        context: CapabilityContext,
    ) -> dict[str, ToolRegistration]:
        selected: dict[str, ToolRegistration] = {}
        for registration in self._registrations.values():
            if not _scope_applies(registration, context):
                continue
            current = selected.get(registration.definition.name)
            if current is None or _SCOPE_PRIORITY[registration.scope] > _SCOPE_PRIORITY[
                current.scope
            ]:
                selected[registration.definition.name] = registration
        return selected


def _validate_tool(tool: AgentToolV2) -> None:
    definition = getattr(tool, "definition", None)
    if not isinstance(definition, ToolDefinitionV2) or not callable(
        getattr(tool, "execute", None)
    ):
        raise AgentPlatformError(
            "invalid_agent_tool",
            "Catalog accepts AgentToolV2 implementations only",
        )


def _validate_tool_name(value: str) -> str:
    name = require_identifier(value, field_name="tool_name", max_length=64)
    if not _TOOL_NAME.fullmatch(name):
        raise AgentPlatformError(
            "invalid_tool_name",
            "Tool name must be lowercase snake_case and at most 64 characters",
        )
    return name


def _validate_scope_id(scope: ToolScope, scope_id: Optional[str]) -> Optional[str]:
    if scope is ToolScope.BUILTIN:
        if scope_id is not None:
            raise AgentPlatformError(
                "invalid_tool_scope",
                "Builtin tool scope cannot have a scope identifier",
            )
        return None
    if scope_id is None:
        raise AgentPlatformError(
            "invalid_tool_scope",
            f"{scope.value} tool scope requires a scope identifier",
        )
    return require_identifier(scope_id, field_name="scope_id")


def _scope_applies(
    registration: ToolRegistration,
    context: CapabilityContext,
) -> bool:
    if registration.scope is ToolScope.BUILTIN:
        return True
    context_scope_id = {
        ToolScope.USER: context.user_id,
        ToolScope.WORKSPACE: context.workspace_id,
        ToolScope.CONVERSATION: context.conversation_id,
        ToolScope.AGENT_RUN: context.run_id,
    }[registration.scope]
    return context_scope_id is not None and registration.scope_id == context_scope_id