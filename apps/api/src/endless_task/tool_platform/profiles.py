from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)

CAPABILITY_PROFILE_SCHEMA_VERSION = 1

STANDARD_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "workspace.delete",
        "process.spawn",
        "process.signal",
        "network.outbound",
        "external.action",
        "agent.delegate",
        "session.query",
        "memory.read",
        "memory.write",
    }
)
_CREDENTIAL_CAPABILITY_PREFIX = "credential.use:"
_CREDENTIAL_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_capability(value: str, *, field_name: str = "capability") -> str:
    capability = require_identifier(value, field_name=field_name)
    if capability in STANDARD_CAPABILITIES:
        return capability
    if capability.startswith(_CREDENTIAL_CAPABILITY_PREFIX):
        profile = capability.removeprefix(_CREDENTIAL_CAPABILITY_PREFIX)
        if _CREDENTIAL_PROFILE.fullmatch(profile):
            return capability
    raise AgentPlatformError(
        "unsupported_capability",
        f"{field_name} is not part of the capability vocabulary",
    )


def normalize_capabilities(
    values: Iterable[str],
    *,
    field_name: str,
) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise AgentPlatformError(
            "invalid_capability_set",
            f"{field_name} must be a collection of capabilities",
        )
    try:
        return frozenset(
            validate_capability(value, field_name=field_name) for value in values
        )
    except TypeError as error:
        raise AgentPlatformError(
            "invalid_capability_set",
            f"{field_name} must be a collection of capabilities",
        ) from error


def intersect_capability_layers(
    *layers: Optional[Iterable[str]],
) -> frozenset[str]:
    normalized = [
        normalize_capabilities(layer, field_name="capability_layer")
        for layer in layers
        if layer is not None
    ]
    if not normalized:
        return frozenset()
    effective = set(normalized[0])
    for layer in normalized[1:]:
        effective.intersection_update(layer)
    return frozenset(effective)


@dataclass(frozen=True)
class CapabilityProfile:
    name: str
    allowed_capabilities: frozenset[str]
    denied_capabilities: frozenset[str] = frozenset()
    description: Optional[str] = None
    schema_version: int = CAPABILITY_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CAPABILITY_PROFILE_SCHEMA_VERSION,
            protocol="capability_profile",
        )
        object.__setattr__(
            self,
            "name",
            require_identifier(self.name, field_name="profile_name", max_length=64),
        )
        object.__setattr__(
            self,
            "allowed_capabilities",
            normalize_capabilities(
                self.allowed_capabilities,
                field_name="allowed_capability",
            ),
        )
        object.__setattr__(
            self,
            "denied_capabilities",
            normalize_capabilities(
                self.denied_capabilities,
                field_name="denied_capability",
            ),
        )
        if self.description is not None:
            object.__setattr__(
                self,
                "description",
                require_text(
                    self.description,
                    field_name="profile_description",
                    max_length=512,
                ),
            )

    @property
    def capabilities(self) -> frozenset[str]:
        return self.allowed_capabilities - self.denied_capabilities

    def allows(self, required_capabilities: Iterable[str]) -> bool:
        required = normalize_capabilities(
            required_capabilities,
            field_name="required_capability",
        )
        return required <= self.capabilities


@dataclass(frozen=True)
class CapabilityContext:
    profile: CapabilityProfile
    requested_capabilities: Optional[frozenset[str]] = None
    deployment_capabilities: Optional[frozenset[str]] = None
    backend_capabilities: Optional[frozenset[str]] = None
    user_id: Optional[str] = None
    workspace_id: Optional[str] = None
    conversation_id: Optional[str] = None
    run_id: Optional[str] = None
    schema_version: int = CAPABILITY_PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=CAPABILITY_PROFILE_SCHEMA_VERSION,
            protocol="capability_context",
        )
        if not isinstance(self.profile, CapabilityProfile):
            raise AgentPlatformError(
                "invalid_capability_context",
                "Capability context requires a capability profile",
            )
        for field_name in (
            "requested_capabilities",
            "deployment_capabilities",
            "backend_capabilities",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    normalize_capabilities(value, field_name=field_name),
                )
        for field_name in (
            "user_id",
            "workspace_id",
            "conversation_id",
            "run_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name=field_name),
                )

    @property
    def effective_capabilities(self) -> frozenset[str]:
        return intersect_capability_layers(
            self.profile.capabilities,
            self.requested_capabilities,
            self.deployment_capabilities,
            self.backend_capabilities,
        )

    def allows(self, required_capabilities: Iterable[str]) -> bool:
        required = normalize_capabilities(
            required_capabilities,
            field_name="required_capability",
        )
        return required <= self.effective_capabilities


class CapabilityProfileResolver(Protocol):
    def resolve(self, name: str) -> CapabilityProfile: ...

    def profiles(self) -> tuple[CapabilityProfile, ...]: ...


class InMemoryCapabilityProfileRegistry:
    def __init__(self, profiles: Iterable[CapabilityProfile] = ()) -> None:
        self._profiles: dict[str, CapabilityProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(
        self,
        profile: CapabilityProfile,
        *,
        override: bool = False,
    ) -> CapabilityProfile:
        if not isinstance(profile, CapabilityProfile):
            raise AgentPlatformError(
                "invalid_capability_profile",
                "Profile registry accepts CapabilityProfile values only",
            )
        if not isinstance(override, bool):
            raise AgentPlatformError(
                "invalid_profile_override",
                "Profile override flag must be boolean",
            )
        if profile.name in self._profiles and not override:
            raise AgentPlatformError(
                "duplicate_capability_profile",
                f"Capability profile is already registered: {profile.name}",
            )
        self._profiles[profile.name] = profile
        return profile

    def resolve(self, name: str) -> CapabilityProfile:
        normalized = require_identifier(name, field_name="profile_name", max_length=64)
        profile = self._profiles.get(normalized)
        if profile is None:
            raise AgentPlatformError(
                "capability_profile_unavailable",
                "Capability profile is unavailable",
            )
        return profile

    def profiles(self) -> tuple[CapabilityProfile, ...]:
        return tuple(self._profiles[name] for name in sorted(self._profiles))


BUILTIN_CAPABILITY_PROFILES = (
    CapabilityProfile(
        name="chat",
        allowed_capabilities=frozenset(
            {"workspace.read", "session.query", "memory.read"}
        ),
        description="Interactive chat with read-only local context.",
    ),
    CapabilityProfile(
        name="work",
        allowed_capabilities=STANDARD_CAPABILITIES,
        description="Primary agent work profile; approval remains a separate gate.",
    ),
    CapabilityProfile(
        name="subagent_readonly",
        allowed_capabilities=frozenset(
            {"workspace.read", "session.query", "memory.read"}
        ),
        description="Read-only delegated research without external effects.",
    ),
    CapabilityProfile(
        name="subagent_workspace",
        allowed_capabilities=frozenset(
            {
                "workspace.read",
                "workspace.write",
                "process.spawn",
                "process.signal",
                "session.query",
                "memory.read",
            }
        ),
        description="Delegated work inside an isolated workspace.",
    ),
    CapabilityProfile(
        name="unattended",
        allowed_capabilities=frozenset(),
        description="Fail-closed base profile; deployments must explicitly allow capabilities.",
    ),
)


def create_builtin_profile_registry() -> InMemoryCapabilityProfileRegistry:
    return InMemoryCapabilityProfileRegistry(BUILTIN_CAPABILITY_PROFILES)