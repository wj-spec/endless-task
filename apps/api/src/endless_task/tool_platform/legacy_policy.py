"""Audited per-tool policy overrides for legacy built-in tools (AP-105a).

M0's ``LegacyToolAdapter`` applied one generic mapping to every v1 tool
(always ``parallel``, capability derived from effect only). AP-105a replaces
that with an audited table for the nine production built-in tools so their
v2 definitions carry the execution mode, idempotency and capability claims
the v1 coordinator never declared.

Every entry below is derived from the real registered definition
(name/effect/approval/timeout/output limit) and from the tool's documented
side effects; the table is locked by tests that introspect the actual tool
classes, so a renamed or repurposed built-in tool fails loudly instead of
silently losing policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
)
from endless_task.tooling import (
    ToolApprovalMode as LegacyApprovalMode,
    ToolEffect as LegacyToolEffect,
)

from .profiles import normalize_capabilities
from .protocol import (
    ApprovalPolicy,
    IdempotencyPolicy,
    ToolEffect,
    ToolExecutionMode,
)

LEGACY_TOOL_POLICY_SCHEMA_VERSION = 1

# Tool names of the production built-in set registered in api/app.py.
BUILTIN_LEGACY_TOOL_NAMES = (
    "read_text_file",
    "read_artifact",
    "read_workspace_file",
    "list_workspace_dir",
    "read_skill_file",
    "write_workspace_file",
    "delete_workspace_file",
    "run_shell",
    "update_plan",
)

#: Capabilities an unbound (no local directory) conversation must lose.
#: Mirrors workspace_runtime/visibility.WORKSPACE_TOOLS semantics: workspace
#: file tools and the shell disappear when no workspace directory is bound.
UNBOUND_WORKSPACE_DENIED_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "workspace.delete",
        "process.spawn",
        "process.signal",
    }
)


@dataclass(frozen=True)
class LegacyToolPolicy:
    tool_name: str
    execution_mode: ToolExecutionMode
    idempotency: IdempotencyPolicy
    required_capabilities: frozenset[str] = frozenset()
    schema_version: int = LEGACY_TOOL_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=LEGACY_TOOL_POLICY_SCHEMA_VERSION,
            protocol="legacy_tool_policy",
        )
        object.__setattr__(
            self,
            "tool_name",
            require_identifier(self.tool_name, field_name="tool_name", max_length=64),
        )
        if not isinstance(self.execution_mode, ToolExecutionMode) or not isinstance(
            self.idempotency, IdempotencyPolicy
        ):
            raise AgentPlatformError(
                "invalid_legacy_tool_policy",
                "Policy enums must use protocol values",
            )
        object.__setattr__(
            self,
            "required_capabilities",
            normalize_capabilities(
                self.required_capabilities,
                field_name="required_capability",
            ),
        )


#: Audited policy per production built-in tool (AP-105a).
#: Idempotency rationale: full-content writes are repeatable with identical
#: arguments (key_required); deletes and shell commands are not repeatable
#: (unsafe); plan updates mutate internal run state and are treated unknown
#: until proven replay-safe; read-only tools are safe.
BUILTIN_LEGACY_TOOL_POLICIES = (
    LegacyToolPolicy(
        tool_name="read_text_file",
        execution_mode=ToolExecutionMode.PARALLEL,
        idempotency=IdempotencyPolicy.SAFE,
        required_capabilities=frozenset({"session.query"}),
    ),
    LegacyToolPolicy(
        tool_name="read_artifact",
        execution_mode=ToolExecutionMode.PARALLEL,
        idempotency=IdempotencyPolicy.SAFE,
        required_capabilities=frozenset({"session.query"}),
    ),
    LegacyToolPolicy(
        tool_name="read_workspace_file",
        execution_mode=ToolExecutionMode.PARALLEL,
        idempotency=IdempotencyPolicy.SAFE,
        required_capabilities=frozenset({"workspace.read"}),
    ),
    LegacyToolPolicy(
        tool_name="list_workspace_dir",
        execution_mode=ToolExecutionMode.PARALLEL,
        idempotency=IdempotencyPolicy.SAFE,
        required_capabilities=frozenset({"workspace.read"}),
    ),
    LegacyToolPolicy(
        tool_name="read_skill_file",
        execution_mode=ToolExecutionMode.PARALLEL,
        idempotency=IdempotencyPolicy.SAFE,
        required_capabilities=frozenset({"session.query"}),
    ),
    LegacyToolPolicy(
        tool_name="write_workspace_file",
        execution_mode=ToolExecutionMode.PATH_SCOPED,
        idempotency=IdempotencyPolicy.KEY_REQUIRED,
        required_capabilities=frozenset({"workspace.write"}),
    ),
    LegacyToolPolicy(
        tool_name="delete_workspace_file",
        execution_mode=ToolExecutionMode.PATH_SCOPED,
        idempotency=IdempotencyPolicy.UNSAFE,
        required_capabilities=frozenset({"workspace.write", "workspace.delete"}),
    ),
    LegacyToolPolicy(
        tool_name="run_shell",
        execution_mode=ToolExecutionMode.EXCLUSIVE,
        idempotency=IdempotencyPolicy.UNSAFE,
        required_capabilities=frozenset(
            {
                "workspace.write",
                "process.spawn",
                "process.signal",
                "external.action",
            }
        ),
    ),
    LegacyToolPolicy(
        tool_name="update_plan",
        execution_mode=ToolExecutionMode.SEQUENTIAL,
        idempotency=IdempotencyPolicy.UNKNOWN,
        required_capabilities=frozenset(),
    ),
)


def builtin_legacy_tool_policy(
    tool_name: str,
) -> Optional[LegacyToolPolicy]:
    """Policy for a built-in tool name, or ``None`` for unknown names."""
    normalized = require_identifier(tool_name, field_name="tool_name", max_length=64)
    for policy in BUILTIN_LEGACY_TOOL_POLICIES:
        if policy.tool_name == normalized:
            return policy
    return None


def capability_grant_for_workspace_binding(
    bound: bool,
    allowed_capabilities: frozenset[str],
) -> frozenset[str]:
    """Granted capabilities for a conversation workspace binding.

    ``bound=True`` keeps every allowed capability; ``bound=False`` removes the
    workspace/process capabilities the legacy conversation predicate hides
    (see ``UNBOUND_WORKSPACE_DENIED_CAPABILITIES``).
    """
    allowed = normalize_capabilities(
        allowed_capabilities,
        field_name="allowed_capability",
    )
    if bound:
        return allowed
    return allowed - UNBOUND_WORKSPACE_DENIED_CAPABILITIES


def adapt_legacy_effect(
    effect: LegacyToolEffect,
) -> ToolEffect:
    if effect is LegacyToolEffect.READ_ONLY:
        return ToolEffect.READ_ONLY
    if effect is LegacyToolEffect.LOCAL_WRITE:
        return ToolEffect.LOCAL_WRITE
    if effect is LegacyToolEffect.EXTERNAL_ACTION:
        return ToolEffect.EXTERNAL_ACTION
    raise AgentPlatformError(
        "invalid_legacy_tool_policy",
        "Legacy tool effect is not part of the v2 vocabulary",
    )


def adapt_legacy_approval(
    approval: LegacyApprovalMode,
) -> ApprovalPolicy:
    if approval is LegacyApprovalMode.AUTO:
        return ApprovalPolicy.AUTO
    if approval is LegacyApprovalMode.REQUIRED:
        return ApprovalPolicy.REQUIRED
    raise AgentPlatformError(
        "invalid_legacy_tool_policy",
        "Legacy approval mode is not part of the v2 vocabulary",
    )


__all__ = [
    "BUILTIN_LEGACY_TOOL_NAMES",
    "BUILTIN_LEGACY_TOOL_POLICIES",
    "LEGACY_TOOL_POLICY_SCHEMA_VERSION",
    "UNBOUND_WORKSPACE_DENIED_CAPABILITIES",
    "LegacyToolPolicy",
    "adapt_legacy_approval",
    "adapt_legacy_effect",
    "builtin_legacy_tool_policy",
    "capability_grant_for_workspace_binding",
]
