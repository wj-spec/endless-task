"""Provider schema projection for the version 2 tool platform.

AP-102 (M1). Turns an authorized :class:`ToolSurface` into the schema payload a
model provider can consume, without widening capability admission:

- description / parameter size / schema depth limits,
- per-provider keyword support (annotations are removable, semantic keywords
  are only removable for safe read-only tools, everything else is excluded),
- a stable projection fingerprint for trace and cache correlation.

The engine never silently relaxes a high-risk tool schema: an effectful tool
whose schema cannot be represented faithfully is excluded with a diagnostic
instead of being exposed in degraded form.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Protocol

from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

from endless_task.agent_platform import (
    AgentPlatformError,
    freeze_json_object,
    plain_json,
    require_identifier,
    require_protocol_version,
    require_text,
)

from .catalog import ToolSurface
from .protocol import ApprovalPolicy, ToolDefinitionV2, ToolEffect

PROJECTION_SCHEMA_VERSION = 1
_PROJECTION_PROTOCOL_NAME = "tool_schema_projection"
_PROJECTION_PROFILE_PROTOCOL_NAME = "provider_schema_profile"

_DEFAULT_DIALECT = "draft2020-12"

#: Keywords that carry no validation semantics. Removing them never loosens the
#: schema, so they may be dropped for any tool when a provider does not support
#: them (the drop is always recorded in a diagnostic).
DEFAULT_ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)

#: Keywords whose value maps arbitrary names to nested schemas. The container's
#: keys are not schema keywords, so only the values are walked.
_MAP_SCHEMA_CONTAINERS = frozenset(
    {
        "$defs",
        "definitions",
        "dependentSchemas",
        "patternProperties",
        "properties",
    }
)

#: Keywords whose value is a list of nested schemas.
_LIST_SCHEMA_CONTAINERS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})

#: Keywords whose value is a single nested schema (a boolean schema is allowed).
_NODE_SCHEMA_CONTAINERS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "items",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)

_FINGERPRINT_HEX = frozenset("0123456789abcdef")


class ProjectionDecision(str, Enum):
    INCLUDED = "included"
    DEGRADED = "degraded"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class ProviderSchemaProfile:
    """Declared schema capability of a model provider endpoint.

    ``supported_keywords=None`` means the provider accepts the full JSON Schema
    vocabulary of :attr:`dialect` and no keyword filtering happens. When a
    keyword set is given, any keyword present in a tool schema that is not in
    the set is handled according to its kind.
    """

    name: str
    dialect: str = _DEFAULT_DIALECT
    supported_keywords: Optional[frozenset[str]] = None
    annotation_keywords: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_ANNOTATION_KEYWORDS)
    )
    max_parameters_bytes: int = 65_536
    max_parameters_depth: int = 16
    max_description_characters: int = 1_024
    degrade_safe_tool_constraints: bool = False
    schema_version: int = PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROJECTION_SCHEMA_VERSION,
            protocol=_PROJECTION_PROFILE_PROTOCOL_NAME,
        )
        object.__setattr__(
            self,
            "name",
            require_identifier(
                self.name,
                field_name="provider_schema_profile_name",
                max_length=64,
            ),
        )
        object.__setattr__(
            self,
            "dialect",
            require_text(
                self.dialect,
                field_name="schema_dialect",
                max_length=128,
            ),
        )
        for field_name, values in (
            ("supported_keywords", self.supported_keywords),
            ("annotation_keywords", self.annotation_keywords),
        ):
            if values is None:
                continue
            if isinstance(values, (str, bytes)):
                raise AgentPlatformError(
                    "invalid_provider_schema_profile",
                    f"{field_name} must be a collection of keywords",
                )
            try:
                normalized = frozenset(
                    require_identifier(keyword, field_name=field_name)
                    for keyword in values
                )
            except TypeError as error:
                raise AgentPlatformError(
                    "invalid_provider_schema_profile",
                    f"{field_name} must be a collection of keywords",
                ) from error
            object.__setattr__(self, field_name, normalized)
        for field_name in (
            "max_parameters_bytes",
            "max_parameters_depth",
            "max_description_characters",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise AgentPlatformError(
                    "invalid_provider_schema_profile",
                    f"{field_name} must be a positive integer",
                )
        if not isinstance(self.degrade_safe_tool_constraints, bool):
            raise AgentPlatformError(
                "invalid_provider_schema_profile",
                "degrade_safe_tool_constraints must be boolean",
            )


def tool_may_degrade(
    definition: ToolDefinitionV2,
    profile: ProviderSchemaProfile,
) -> bool:
    """Whether an unsupported constraint may be dropped for this tool.

    Degradation is only permitted for read-only, auto-approved tools. Effectful
    tools must be excluded (fail closed) instead of silently widened.
    """
    if not isinstance(profile, ProviderSchemaProfile):
        raise AgentPlatformError(
            "invalid_provider_schema_profile",
            "Tool degradation requires a provider schema profile",
        )
    if not isinstance(definition, ToolDefinitionV2):
        raise AgentPlatformError(
            "invalid_tool_definition",
            "Tool degradation requires a tool definition",
        )
    return (
        profile.degrade_safe_tool_constraints
        and definition.effect is ToolEffect.READ_ONLY
        and definition.approval is ApprovalPolicy.AUTO
    )


@dataclass(frozen=True)
class SchemaProjectionDiagnostic:
    """Decision record for one tool. Nothing is dropped or excluded silently."""

    tool_name: str
    decision: ProjectionDecision
    code: str
    safe_message: str
    removed_keywords: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROJECTION_SCHEMA_VERSION,
            protocol=_PROJECTION_PROTOCOL_NAME,
        )
        object.__setattr__(
            self,
            "tool_name",
            require_identifier(self.tool_name, field_name="tool_name", max_length=64),
        )
        if not isinstance(self.decision, ProjectionDecision):
            raise AgentPlatformError(
                "invalid_projection_diagnostic",
                "Projection decision must use a protocol enum value",
            )
        object.__setattr__(
            self,
            "code",
            require_identifier(
                self.code,
                field_name="projection_diagnostic_code",
                max_length=64,
            ),
        )
        object.__setattr__(
            self,
            "safe_message",
            require_text(
                self.safe_message,
                field_name="projection_diagnostic_message",
                max_length=2_048,
            ),
        )
        try:
            normalized_keywords = tuple(
                sorted(
                    {
                        require_identifier(
                            keyword,
                            field_name="removed_keyword",
                            max_length=64,
                        )
                        for keyword in self.removed_keywords
                    }
                )
            )
        except TypeError as error:
            raise AgentPlatformError(
                "invalid_projection_diagnostic",
                "removed_keywords must be a collection of keywords",
            ) from error
        object.__setattr__(self, "removed_keywords", normalized_keywords)
        object.__setattr__(
            self,
            "details",
            freeze_json_object(self.details, field_name="details"),
        )


@dataclass(frozen=True)
class ProjectedToolSchema:
    """Provider-facing projection of one registered tool."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    fingerprint: str
    decision: ProjectionDecision
    diagnostic: Optional[SchemaProjectionDiagnostic] = None
    schema_version: int = PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROJECTION_SCHEMA_VERSION,
            protocol=_PROJECTION_PROTOCOL_NAME,
        )
        object.__setattr__(
            self,
            "name",
            require_identifier(self.name, field_name="tool_name", max_length=64),
        )
        object.__setattr__(
            self,
            "description",
            require_text(
                self.description,
                field_name="tool_description",
                max_length=4_096,
            ),
        )
        object.__setattr__(
            self,
            "input_schema",
            _validated_schema(self.input_schema, field_name="input_schema"),
        )
        if not isinstance(self.decision, ProjectionDecision):
            raise AgentPlatformError(
                "invalid_projected_tool",
                "Projected tool decision must use a protocol enum value",
            )
        if self.decision is ProjectionDecision.EXCLUDED:
            raise AgentPlatformError(
                "invalid_projected_tool",
                "Excluded tools do not produce a projected schema",
            )
        fingerprint = require_identifier(
            self.fingerprint,
            field_name="projection_fingerprint",
            max_length=64,
        )
        if len(fingerprint) != 64 or any(
            character not in _FINGERPRINT_HEX for character in fingerprint
        ):
            raise AgentPlatformError(
                "invalid_projection_fingerprint",
                "Projection fingerprint must be a sha256 hex digest",
            )
        object.__setattr__(self, "fingerprint", fingerprint)
        if self.decision is ProjectionDecision.DEGRADED and self.diagnostic is None:
            raise AgentPlatformError(
                "invalid_projected_tool",
                "Degraded projected tools require a diagnostic",
            )
        if self.diagnostic is not None:
            if not isinstance(self.diagnostic, SchemaProjectionDiagnostic):
                raise AgentPlatformError(
                    "invalid_projected_tool",
                    "Projected tool diagnostic must use the protocol type",
                )
            if self.diagnostic.tool_name != self.name:
                raise AgentPlatformError(
                    "invalid_projected_tool",
                    "Projected tool diagnostic must match the tool name",
                )


@dataclass(frozen=True)
class ToolProjectionReport:
    """Deterministic outcome of projecting one tool surface."""

    profile_name: str
    dialect: str
    catalog_generation: int
    projected: tuple[ProjectedToolSchema, ...]
    excluded: tuple[SchemaProjectionDiagnostic, ...]
    fingerprint: str
    schema_version: int = PROJECTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=PROJECTION_SCHEMA_VERSION,
            protocol=_PROJECTION_PROTOCOL_NAME,
        )
        object.__setattr__(
            self,
            "profile_name",
            require_identifier(
                self.profile_name,
                field_name="provider_schema_profile_name",
                max_length=64,
            ),
        )
        object.__setattr__(
            self,
            "dialect",
            require_text(self.dialect, field_name="schema_dialect", max_length=128),
        )
        if (
            not isinstance(self.catalog_generation, int)
            or isinstance(self.catalog_generation, bool)
            or self.catalog_generation < 0
        ):
            raise AgentPlatformError(
                "invalid_catalog_generation",
                "Catalog generation must be a non-negative integer",
            )
        for entry in self.projected:
            if not isinstance(entry, ProjectedToolSchema):
                raise AgentPlatformError(
                    "invalid_projection_report",
                    "Projected tools must use the protocol type",
                )
        for entry in self.excluded:
            if not isinstance(entry, SchemaProjectionDiagnostic):
                raise AgentPlatformError(
                    "invalid_projection_report",
                    "Excluded entries must use the protocol type",
                )
        object.__setattr__(
            self,
            "projected",
            tuple(
                sorted(self.projected, key=lambda item: item.name),
            ),
        )
        object.__setattr__(
            self,
            "excluded",
            tuple(
                sorted(self.excluded, key=lambda item: item.tool_name),
            ),
        )
        projected_names = {item.name for item in self.projected}
        excluded_names = {item.tool_name for item in self.excluded}
        duplicates = projected_names & excluded_names
        if duplicates:
            raise AgentPlatformError(
                "invalid_projection_report",
                "A tool cannot be both projected and excluded",
            )
        fingerprint = require_identifier(
            self.fingerprint,
            field_name="projection_fingerprint",
            max_length=64,
        )
        if len(fingerprint) != 64 or any(
            character not in _FINGERPRINT_HEX for character in fingerprint
        ):
            raise AgentPlatformError(
                "invalid_projection_fingerprint",
                "Projection fingerprint must be a sha256 hex digest",
            )
        object.__setattr__(self, "fingerprint", fingerprint)

    @property
    def projected_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.projected)

    @property
    def excluded_names(self) -> tuple[str, ...]:
        return tuple(item.tool_name for item in self.excluded)


class ToolSchemaProjection(Protocol):
    """Projects an authorized tool surface against a provider schema profile."""

    def project(
        self,
        surface: ToolSurface,
        profile: ProviderSchemaProfile,
    ) -> ToolProjectionReport: ...


def schema_fingerprint(schema: Mapping[str, Any]) -> str:
    """Stable sha256 fingerprint of one JSON Schema (key order independent)."""
    validated = _validated_schema(schema, field_name="input_schema")
    return _sha256(plain_json(validated))


class DefaultToolSchemaProjection:
    """Conservative schema projection: size caps, keyword policy, fingerprint."""

    def project(
        self,
        surface: ToolSurface,
        profile: ProviderSchemaProfile,
    ) -> ToolProjectionReport:
        if not isinstance(surface, ToolSurface):
            raise AgentPlatformError(
                "invalid_tool_surface",
                "Schema projection requires a ToolSurface",
            )
        if not isinstance(profile, ProviderSchemaProfile):
            raise AgentPlatformError(
                "invalid_provider_schema_profile",
                "Schema projection requires a provider schema profile",
            )
        projected: list[ProjectedToolSchema] = []
        excluded: list[SchemaProjectionDiagnostic] = []
        for registration in surface.registrations:
            definition = registration.definition
            gate = self._gate_tool(definition, profile)
            if gate is not None:
                excluded.append(gate)
                continue
            try:
                item = self._project_keywords(definition, profile)
            except AgentPlatformError as error:
                # A profile/tool mismatch must never crash the projection; it
                # becomes a structured exclusion with a safe diagnostic.
                excluded.append(
                    SchemaProjectionDiagnostic(
                        tool_name=definition.name,
                        decision=ProjectionDecision.EXCLUDED,
                        code="projection_failed",
                        safe_message=(
                            "Tool schema could not be projected for this "
                            "provider schema profile."
                        ),
                        details={"reason": error.code},
                    )
                )
                continue
            if item is None:
                excluded.append(self._exclude_incompatible(definition, profile))
            else:
                projected.append(item)
        fingerprint = projection_fingerprint(
            profile,
            catalog_generation=surface.catalog_generation,
            projected=projected,
            excluded=excluded,
        )
        return ToolProjectionReport(
            profile_name=profile.name,
            dialect=profile.dialect,
            catalog_generation=surface.catalog_generation,
            projected=tuple(projected),
            excluded=tuple(excluded),
            fingerprint=fingerprint,
        )

    @staticmethod
    def _gate_tool(
        definition: ToolDefinitionV2,
        profile: ProviderSchemaProfile,
    ) -> Optional[SchemaProjectionDiagnostic]:
        """Size gates that run before any keyword handling."""
        description_length = len(definition.description)
        if description_length > profile.max_description_characters:
            return SchemaProjectionDiagnostic(
                tool_name=definition.name,
                decision=ProjectionDecision.EXCLUDED,
                code="description_too_long",
                safe_message=(
                    "Tool description exceeds the provider schema profile limit."
                ),
                details={
                    "max_description_characters": profile.max_description_characters,
                    "actual": description_length,
                },
            )
        schema = plain_json(definition.input_schema)
        size = len(_canonical_bytes(schema))
        if size > profile.max_parameters_bytes:
            return SchemaProjectionDiagnostic(
                tool_name=definition.name,
                decision=ProjectionDecision.EXCLUDED,
                code="schema_too_large",
                safe_message="Tool input schema exceeds the provider size limit.",
                details={
                    "max_parameters_bytes": profile.max_parameters_bytes,
                    "actual": size,
                },
            )
        depth = _schema_depth(schema)
        if depth > profile.max_parameters_depth:
            return SchemaProjectionDiagnostic(
                tool_name=definition.name,
                decision=ProjectionDecision.EXCLUDED,
                code="schema_too_deep",
                safe_message="Tool input schema exceeds the provider depth limit.",
                details={
                    "max_parameters_depth": profile.max_parameters_depth,
                    "actual": depth,
                },
            )
        return None

    @staticmethod
    def _project_keywords(
        definition: ToolDefinitionV2,
        profile: ProviderSchemaProfile,
    ) -> Optional[ProjectedToolSchema]:
        """Keyword policy; returns None when the tool must be excluded."""
        schema = plain_json(definition.input_schema)
        decision = ProjectionDecision.INCLUDED
        diagnostic: Optional[SchemaProjectionDiagnostic] = None
        unsupported_semantic = _unsupported_semantic_keywords(schema, profile)
        unsupported_annotations = _unsupported_annotations(schema, profile)
        if unsupported_semantic:
            if not tool_may_degrade(definition, profile):
                return None
            schema = _without_keywords(
                schema,
                frozenset(unsupported_semantic) | frozenset(unsupported_annotations),
            )
            decision = ProjectionDecision.DEGRADED
            diagnostic = SchemaProjectionDiagnostic(
                tool_name=definition.name,
                decision=decision,
                code="constraints_removed",
                safe_message=(
                    "Tool schema degraded: unsupported provider keywords "
                    "were removed from a safe read-only tool."
                ),
                removed_keywords=tuple(
                    unsupported_semantic + unsupported_annotations
                ),
                details={
                    "removed_keywords": list(
                        unsupported_semantic + unsupported_annotations
                    )
                },
            )
        elif unsupported_annotations:
            schema = _without_keywords(schema, frozenset(unsupported_annotations))
            diagnostic = SchemaProjectionDiagnostic(
                tool_name=definition.name,
                decision=decision,
                code="annotations_removed",
                safe_message=(
                    "Unsupported provider annotation keywords were removed "
                    "from the tool schema."
                ),
                removed_keywords=tuple(unsupported_annotations),
                details={"removed_keywords": list(unsupported_annotations)},
            )
        return ProjectedToolSchema(
            name=definition.name,
            description=definition.description,
            input_schema=_validated_schema(schema, field_name="input_schema"),
            fingerprint=schema_fingerprint(schema),
            decision=decision,
            diagnostic=diagnostic,
        )

    @staticmethod
    def _exclude_incompatible(
        definition: ToolDefinitionV2,
        profile: ProviderSchemaProfile,
    ) -> SchemaProjectionDiagnostic:
        schema = plain_json(definition.input_schema)
        return SchemaProjectionDiagnostic(
            tool_name=definition.name,
            decision=ProjectionDecision.EXCLUDED,
            code="incompatible_provider_schema",
            safe_message=(
                "Tool schema uses keywords the provider does not support; the "
                "tool is excluded rather than silently weakened."
            ),
            details={
                "unsupported_keywords": list(
                    _unsupported_semantic_keywords(schema, profile)
                )
            },
        )


def project_tool_surface(
    surface: ToolSurface,
    profile: ProviderSchemaProfile,
    *,
    projection: Optional[ToolSchemaProjection] = None,
) -> ToolProjectionReport:
    engine = projection if projection is not None else DefaultToolSchemaProjection()
    if not callable(getattr(engine, "project", None)):
        raise AgentPlatformError(
            "invalid_projection_engine",
            "Tool projection requires a ToolSchemaProjection implementation",
        )
    return engine.project(surface, profile)


def projection_fingerprint(
    profile: ProviderSchemaProfile,
    *,
    catalog_generation: int,
    projected: Iterable[ProjectedToolSchema],
    excluded: Iterable[SchemaProjectionDiagnostic],
) -> str:
    """Stable fingerprint of one whole projection run.

    Changes when the provider profile, catalog generation, an included schema,
    or an exclusion changes, so trace and cache consumers can key on it.
    """
    if not isinstance(profile, ProviderSchemaProfile):
        raise AgentPlatformError(
            "invalid_provider_schema_profile",
            "Projection fingerprint requires a provider schema profile",
        )
    if (
        not isinstance(catalog_generation, int)
        or isinstance(catalog_generation, bool)
        or catalog_generation < 0
    ):
        raise AgentPlatformError(
            "invalid_catalog_generation",
            "Catalog generation must be a non-negative integer",
        )
    projected_items = sorted(
        (
            {
                "name": item.name,
                "decision": item.decision.value,
                "schema": plain_json(item.input_schema),
            }
            for item in projected
        ),
        key=lambda item: item["name"],
    )
    excluded_items = sorted(
        (
            {
                "name": item.tool_name,
                "decision": item.decision.value,
                "code": item.code,
                "removed_keywords": list(item.removed_keywords),
            }
            for item in excluded
        ),
        key=lambda item: item["name"],
    )
    payload = {
        "provider_schema_profile": profile.name,
        "dialect": profile.dialect,
        "catalog_generation": catalog_generation,
        "projected": projected_items,
        "excluded": excluded_items,
    }
    return _sha256(payload)


def _sha256(value: Any) -> str:
    try:
        canonical = json.dumps(
            plain_json(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise AgentPlatformError(
            "invalid_projection_fingerprint",
            "Projection fingerprint input must be JSON compatible",
        ) from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        canonical = json.dumps(
            plain_json(value),
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
    return canonical.encode("utf-8")


def _schema_depth(value: Any, depth: int = 1) -> int:
    if isinstance(value, Mapping):
        nested = [_schema_depth(nested_value, depth + 1) for nested_value in value.values()]
        return max(nested, default=depth)
    if isinstance(value, (list, tuple)):
        nested = [_schema_depth(item, depth + 1) for item in value]
        return max(nested, default=depth)
    return depth


def _collect_schema_keywords(schema: Any, seen: set[str]) -> None:
    """Collect every JSON Schema keyword key present in ``schema``.

    Only actual schema nodes are inspected: property-name maps (``properties``
    and friends) contribute their values, never their keys, and raw value
    keywords (``enum``, ``examples`` and friends) are not descended into.
    """
    if not isinstance(schema, dict):
        return
    for keyword, value in schema.items():
        seen.add(keyword)
        if keyword in _MAP_SCHEMA_CONTAINERS and isinstance(value, dict):
            for nested in value.values():
                _collect_schema_keywords(nested, seen)
        elif keyword in _LIST_SCHEMA_CONTAINERS and isinstance(value, list):
            for nested in value:
                _collect_schema_keywords(nested, seen)
        elif keyword in _NODE_SCHEMA_CONTAINERS:
            if isinstance(value, dict):
                _collect_schema_keywords(value, seen)
            elif isinstance(value, list):
                # Draft-07 tuple form of items/prefixItems.
                for nested in value:
                    _collect_schema_keywords(nested, seen)


def _unsupported_semantic_keywords(
    schema: Any,
    profile: ProviderSchemaProfile,
) -> list[str]:
    """Keywords present but unsupported that carry validation semantics."""
    if profile.supported_keywords is None:
        return []
    present: set[str] = set()
    _collect_schema_keywords(schema, present)
    return sorted(
        keyword
        for keyword in present
        if keyword not in profile.supported_keywords
        and keyword not in profile.annotation_keywords
    )


def _unsupported_annotations(
    schema: Any,
    profile: ProviderSchemaProfile,
) -> list[str]:
    """Keywords present but unsupported that are pure annotations."""
    if profile.supported_keywords is None:
        return []
    present: set[str] = set()
    _collect_schema_keywords(schema, present)
    return sorted(
        keyword
        for keyword in present
        if keyword in profile.annotation_keywords
        and keyword not in profile.supported_keywords
    )


def _without_keywords(schema: Any, removed: frozenset[str]) -> Any:
    """Return a deep copy of ``schema`` without the removed keyword keys."""
    if not isinstance(schema, dict):
        return plain_json(schema)
    rebuilt: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword in removed:
            continue
        if keyword in _MAP_SCHEMA_CONTAINERS and isinstance(value, dict):
            rebuilt[keyword] = {
                name: _without_keywords(nested, removed)
                for name, nested in value.items()
            }
        elif keyword in _LIST_SCHEMA_CONTAINERS and isinstance(value, list):
            rebuilt[keyword] = [
                _without_keywords(nested, removed) if isinstance(nested, (dict, bool)) else nested
                for nested in value
            ]
        elif keyword in _NODE_SCHEMA_CONTAINERS:
            if isinstance(value, (dict, bool)):
                rebuilt[keyword] = _without_keywords(value, removed)
            elif isinstance(value, list):
                # Draft-07 tuple form of items/prefixItems.
                rebuilt[keyword] = [
                    _without_keywords(nested, removed)
                    if isinstance(nested, (dict, bool))
                    else nested
                    for nested in value
                ]
            else:
                rebuilt[keyword] = plain_json(value)
        else:
            rebuilt[keyword] = plain_json(value)
    return rebuilt


def _validated_schema(value: Mapping[str, Any], *, field_name: str) -> Mapping[str, Any]:
    frozen = freeze_json_object(value, field_name=field_name)
    plain = plain_json(frozen)
    if plain.get("type") != "object":
        raise AgentPlatformError(
            "invalid_tool_schema",
            f"{field_name} root type must be object",
        )
    try:
        validator_for(plain).check_schema(plain)
    except SchemaError as error:
        raise AgentPlatformError(
            "invalid_tool_schema",
            f"{field_name} must be valid JSON Schema",
        ) from error
    return MappingProxyType(dict(plain))
