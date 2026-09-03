"""In-process ExtensionBus implementation for the Agent Platform.

AP-103 (M1). Implements the dispatch runtime behind the frozen
:class:`~endless_task.extensions.protocol.ExtensionBus` contract:

- extensions subscribe to versioned hooks; dispatch only routes versioned
  ``ExtensionEvent`` values and never hands over Gateway/SQLite/FastAPI,
- observation extensions run for every event and their failures are isolated
  and recorded as diagnostics,
- safety extensions run only for ``SAFETY_DECISION`` events; a failure or a
  decision invalid for the hook stage fails closed (deny before tools,
  block the result after tools),
- per-stage decision merge follows the deterministic precedence rules in
  ``decisions.py`` (deny > ask > replace_arguments > allow,
  block_result > replace_result > accept),
- only extensions that declare ``may_transform_arguments`` may replace tool
  arguments; an untrusted ``replace_arguments`` decision fails closed to deny.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    require_identifier,
    require_protocol_version,
)

from .decisions import merge_post_tool_decisions, merge_pre_tool_decisions
from .protocol import (
    EXTENSION_PROTOCOL_VERSION,
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionDispatchResult,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
)

EXTENSION_BUS_SCHEMA_VERSION = 1

_PRE_TOOL_KINDS = frozenset(
    {
        ExtensionDecisionKind.ALLOW,
        ExtensionDecisionKind.REPLACE_ARGUMENTS,
        ExtensionDecisionKind.ASK,
        ExtensionDecisionKind.DENY,
    }
)
_POST_TOOL_KINDS = frozenset(
    {
        ExtensionDecisionKind.ACCEPT,
        ExtensionDecisionKind.REPLACE_RESULT,
        ExtensionDecisionKind.BLOCK_RESULT,
    }
)

#: Hooks whose events may carry safety decisions under the frozen grammar.
_DECISION_HOOKS = frozenset({ExtensionHook.BEFORE_TOOL, ExtensionHook.AFTER_TOOL})


class AgentExtension(Protocol):
    """An extension subscribed to one or more hooks of a bus.

    ``handle`` returns at most one decision; observation extensions must
    return ``None`` and safety extensions must only return decisions valid for
    the hook stage of the event.
    """

    extension_id: str
    mode: ExtensionEventMode
    hooks: frozenset[ExtensionHook]
    #: Whether this extension is trusted to replace tool arguments.
    may_transform_arguments: bool

    async def handle(self, event: ExtensionEvent) -> Optional[ExtensionDecision]: ...


@dataclass(frozen=True)
class ExtensionRegistration:
    registration_id: str
    extension_id: str
    mode: ExtensionEventMode
    hooks: tuple[ExtensionHook, ...]
    schema_version: int = EXTENSION_BUS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=EXTENSION_BUS_SCHEMA_VERSION,
            protocol="extension_registration",
        )
        for field_name in ("registration_id", "extension_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        if not isinstance(self.mode, ExtensionEventMode):
            raise AgentPlatformError(
                "invalid_extension_registration",
                "Extension mode must use a protocol enum value",
            )
        if not self.hooks or any(
            not isinstance(hook, ExtensionHook) for hook in self.hooks
        ):
            raise AgentPlatformError(
                "invalid_extension_registration",
                "Extension registration requires protocol hook values",
            )
        object.__setattr__(
            self,
            "hooks",
            tuple(sorted(self.hooks, key=lambda hook: hook.value)),
        )


class InProcessExtensionBus:
    """Deterministic in-process dispatcher over versioned extension events."""

    def __init__(self) -> None:
        self._subscriptions: dict[ExtensionHook, list[AgentExtension]] = {
            hook: [] for hook in ExtensionHook
        }
        self._registrations: dict[str, tuple[ExtensionRegistration, AgentExtension]] = {}
        self._registration_counter = 0

    def register(
        self,
        extension: AgentExtension,
        *,
        hooks: Optional[Iterable[ExtensionHook]] = None,
    ) -> ExtensionRegistration:
        extension_id = _validate_extension(extension)
        if extension_id in self._registrations:
            raise AgentPlatformError(
                "duplicate_extension",
                f"Extension is already registered: {extension_id}",
            )
        if hooks is None:
            selected_hooks = extension.hooks
        else:
            if isinstance(hooks, (str, bytes)) or not isinstance(hooks, Iterable):
                raise AgentPlatformError(
                    "invalid_extension_registration",
                    "hooks must be a collection of ExtensionHook values",
                )
            try:
                selected_hooks = frozenset(hooks)
            except TypeError as error:
                raise AgentPlatformError(
                    "invalid_extension_registration",
                    "hooks must be a collection of ExtensionHook values",
                ) from error
        if not selected_hooks or any(
            not isinstance(hook, ExtensionHook) for hook in selected_hooks
        ):
            raise AgentPlatformError(
                "invalid_extension_registration",
                "hooks must contain ExtensionHook values",
            )
        normalized_hooks = tuple(
            sorted(selected_hooks, key=lambda hook: hook.value)
        )
        self._registration_counter += 1
        registration = ExtensionRegistration(
            registration_id=f"extension_registration_{self._registration_counter}",
            extension_id=extension_id,
            mode=extension.mode,
            hooks=normalized_hooks,
        )
        self._registrations[extension_id] = (registration, extension)
        for hook in normalized_hooks:
            self._subscriptions[hook].append(extension)
        return registration

    async def dispatch(self, event: ExtensionEvent) -> ExtensionDispatchResult:
        if not isinstance(event, ExtensionEvent):
            raise AgentPlatformError(
                "invalid_extension_event",
                "Extension bus dispatch requires an ExtensionEvent",
            )
        if event.mode is ExtensionEventMode.SAFETY_DECISION:
            if event.hook not in _DECISION_HOOKS:
                raise AgentPlatformError(
                    "invalid_extension_event_mode",
                    "Safety-decision events are only supported for tool hooks",
                    details={"hook": event.hook.value},
                )
            return await self._dispatch_safety(event)
        return await self._dispatch_observation(event)

    def registrations(self) -> tuple[ExtensionRegistration, ...]:
        return tuple(
            sorted(
                (registration for registration, _ in self._registrations.values()),
                key=lambda item: item.extension_id,
            )
        )

    def extension(self, extension_id: str) -> AgentExtension:
        normalized = require_identifier(extension_id, field_name="extension_id")
        registration = self._registrations.get(normalized)
        if registration is None:
            raise AgentPlatformError(
                "extension_unavailable",
                "Extension is not registered on this bus",
            )
        return registration[1]

    async def _dispatch_safety(
        self,
        event: ExtensionEvent,
    ) -> ExtensionDispatchResult:
        diagnostics: list[SafeDiagnostic] = []
        decisions: list[ExtensionDecision] = []
        for extension in self._subscriptions[event.hook]:
            if extension.mode is ExtensionEventMode.OBSERVATION:
                # Observation extensions may record a safety event, but a
                # failure must never close the tool stage.
                observation_diagnostic = await _run_observation(extension, event)
                if observation_diagnostic is not None:
                    diagnostics.append(observation_diagnostic)
                continue
            try:
                decision = await extension.handle(event)
            except Exception as error:
                diagnostics.append(_failure_diagnostic(extension, error))
                return ExtensionDispatchResult(
                    decision=_fail_closed(extension, event.hook),
                    diagnostics=tuple(diagnostics),
                )
            if decision is None:
                continue
            try:
                _validate_kind_for_stage(decision.kind, event.hook)
            except AgentPlatformError as error:
                diagnostics.append(_stage_diagnostic(extension, error))
                return ExtensionDispatchResult(
                    decision=_fail_closed(extension, event.hook),
                    diagnostics=tuple(diagnostics),
                )
            decisions.append(decision)
        merged = _merge_stage(decisions, event.hook)
        if merged is None:
            return ExtensionDispatchResult(
                decision=None,
                diagnostics=tuple(diagnostics),
            )
        if not _may_replace_arguments(self._registrations, merged):
            diagnostics.append(
                _warning_diagnostic(
                    "untrusted_argument_replacement_denied",
                    "Only trusted extensions may replace tool arguments; "
                    "the request was denied instead.",
                )
            )
            return ExtensionDispatchResult(
                decision=ExtensionDecision(
                    extension_id=merged.extension_id,
                    kind=ExtensionDecisionKind.DENY,
                    reason="untrusted_argument_replacement",
                ),
                diagnostics=tuple(diagnostics),
            )
        return ExtensionDispatchResult(
            decision=merged,
            diagnostics=tuple(diagnostics),
        )

    async def _dispatch_observation(
        self,
        event: ExtensionEvent,
    ) -> ExtensionDispatchResult:
        diagnostics: list[SafeDiagnostic] = []
        for extension in self._subscriptions[event.hook]:
            if extension.mode is not ExtensionEventMode.OBSERVATION:
                continue
            diagnostic = await _run_observation(extension, event)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
        return ExtensionDispatchResult(decision=None, diagnostics=tuple(diagnostics))


async def _run_observation(
    extension: AgentExtension,
    event: ExtensionEvent,
) -> Optional[SafeDiagnostic]:
    """Run an observation extension, isolating failures and misuse."""
    try:
        decision = await extension.handle(event)
    except Exception as error:
        return _failure_diagnostic(extension, error)
    if decision is not None:
        return _warning_diagnostic(
            "observation_extension_returned_decision",
            "Observation extensions must not return decisions.",
        )
    return None


def _validate_extension(extension: AgentExtension) -> str:
    extension_id = getattr(extension, "extension_id", None)
    extension_id = require_identifier(extension_id, field_name="extension_id")
    if not isinstance(getattr(extension, "mode", None), ExtensionEventMode):
        raise AgentPlatformError(
            "invalid_extension",
            "Extension requires a protocol event mode",
        )
    hooks = getattr(extension, "hooks", None)
    if not isinstance(hooks, frozenset) or not hooks or any(
        not isinstance(hook, ExtensionHook) for hook in hooks
    ):
        raise AgentPlatformError(
            "invalid_extension",
            "Extension requires a non-empty frozenset of hooks",
        )
    if not callable(getattr(extension, "handle", None)):
        raise AgentPlatformError(
            "invalid_extension",
            "Extension requires an async handle(event) method",
        )
    if not isinstance(getattr(extension, "may_transform_arguments", None), bool):
        raise AgentPlatformError(
            "invalid_extension",
            "may_transform_arguments must be boolean",
        )
    return extension_id


def _validate_kind_for_stage(
    kind: ExtensionDecisionKind,
    hook: ExtensionHook,
) -> None:
    allowed = (
        _PRE_TOOL_KINDS if hook is ExtensionHook.BEFORE_TOOL else _POST_TOOL_KINDS
    )
    if hook not in _DECISION_HOOKS or kind not in allowed:
        raise AgentPlatformError(
            "invalid_extension_decision_stage",
            "Decision kind is not valid for this hook stage",
            details={"hook": hook.value, "decisionKind": kind.value},
        )


def _merge_stage(
    decisions: list[ExtensionDecision],
    hook: ExtensionHook,
) -> Optional[ExtensionDecision]:
    if hook is ExtensionHook.BEFORE_TOOL:
        return merge_pre_tool_decisions(decisions)
    if hook is ExtensionHook.AFTER_TOOL:
        return merge_post_tool_decisions(decisions)
    return None


def _may_replace_arguments(
    registrations: dict[str, tuple[ExtensionRegistration, AgentExtension]],
    decision: ExtensionDecision,
) -> bool:
    if decision.kind is not ExtensionDecisionKind.REPLACE_ARGUMENTS:
        return True
    registration = registrations.get(decision.extension_id)
    return registration is not None and bool(registration[1].may_transform_arguments)


def _fail_closed(
    extension: AgentExtension,
    hook: ExtensionHook,
) -> ExtensionDecision:
    kind = (
        ExtensionDecisionKind.DENY
        if hook is ExtensionHook.BEFORE_TOOL
        else ExtensionDecisionKind.BLOCK_RESULT
    )
    return ExtensionDecision(
        extension_id=extension.extension_id,
        kind=kind,
        reason="extension_failure_fail_closed",
    )


def _failure_diagnostic(
    extension: AgentExtension,
    error: Exception,
) -> SafeDiagnostic:
    message = getattr(error, "safe_message", None)
    return SafeDiagnostic(
        code="extension_error",
        safe_message=message if isinstance(message, str) else "扩展处理事件时发生错误。",
        retryable=False,
        details={
            "extension_id": extension.extension_id,
            "error_type": type(error).__name__,
        },
    )


def _stage_diagnostic(
    extension: AgentExtension,
    error: AgentPlatformError,
) -> SafeDiagnostic:
    return SafeDiagnostic(
        code="invalid_extension_decision_stage",
        safe_message=error.safe_message,
        retryable=False,
        details={
            "extension_id": extension.extension_id,
            "error_code": error.code,
        },
    )


def _warning_diagnostic(code: str, safe_message: str) -> SafeDiagnostic:
    return SafeDiagnostic(code=code, safe_message=safe_message, retryable=False)
