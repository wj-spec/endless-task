"""Built-in extensions shipped with the Agent Platform (AP-103, M1).

All built-ins are plain data-in/data-out handlers over versioned
``ExtensionEvent`` payloads. They never import the AgentKernel, concrete
tools, storage or API layers, so they can be composed into any pipeline that
emits the payload conventions below.

Payload conventions (keys are read strictly; malformed payloads raise
``AgentPlatformError`` so safety extensions fail closed):

- ``before_tool`` (approval): ``tool_name``, ``approval_mode`` in
  ``auto|ask|required|forbidden_unattended``, ``unattended`` boolean.
- ``after_tool`` (spill): ``tool_name``, ``content``, ``max_characters``,
  optional ``spill_reference``.
- ``after_tool`` (redaction): ``tool_name``, ``content``.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError

from .bus import AgentExtension
from .protocol import (
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
)

_APPROVAL_AUTO = "auto"
_APPROVAL_ASK = "ask"
_APPROVAL_REQUIRED = "required"
_APPROVAL_FORBIDDEN_UNATTENDED = "forbidden_unattended"
_REDACTION_PLACEHOLDER = "[REDACTED]"


class ApprovalPolicyExtension:
    """Before-tool safety gate driven by the tool approval policy payload.

    - ``forbidden_unattended`` under an unattended run is denied,
    - ``ask`` / ``required`` produce an approval question,
    - ``auto`` is allowed.
    """

    extension_id = "builtin.approval_policy"
    mode = ExtensionEventMode.SAFETY_DECISION
    hooks = frozenset({ExtensionHook.BEFORE_TOOL})
    may_transform_arguments = False

    async def handle(
        self,
        event: ExtensionEvent,
    ) -> Optional[ExtensionDecision]:
        if event.hook is not ExtensionHook.BEFORE_TOOL:
            return None
        payload = _payload(event)
        approval_mode = _require_text(payload, "approval_mode")
        tool_name = _require_text(payload, "tool_name")
        unattended = _require_bool(payload, "unattended")
        if approval_mode == _APPROVAL_FORBIDDEN_UNATTENDED:
            if unattended:
                return ExtensionDecision(
                    extension_id=self.extension_id,
                    kind=ExtensionDecisionKind.DENY,
                    reason="forbidden_unattended",
                )
            # Attended sessions may run the tool, but only with approval.
            return ExtensionDecision(
                extension_id=self.extension_id,
                kind=ExtensionDecisionKind.ASK,
                reason="approval_required",
            )
        if approval_mode in (_APPROVAL_ASK, _APPROVAL_REQUIRED):
            return ExtensionDecision(
                extension_id=self.extension_id,
                kind=ExtensionDecisionKind.ASK,
                reason="approval_required",
            )
        if approval_mode == _APPROVAL_AUTO:
            return ExtensionDecision(
                extension_id=self.extension_id,
                kind=ExtensionDecisionKind.ALLOW,
                reason="auto_allowed",
            )
        raise AgentPlatformError(
            "invalid_extension_payload",
            "approval_mode is not part of the approval vocabulary",
            details={"tool_name": tool_name},
        )


class AuditExtension:
    """Observation extension that records a bounded, JSON-safe event summary.

    The durable audit ledger belongs to M6 (RuntimeLedger); this extension only
    provides an in-memory, bounded, testable record for development.
    """

    extension_id = "builtin.audit"
    mode = ExtensionEventMode.OBSERVATION
    may_transform_arguments = False

    def __init__(
        self,
        *,
        hooks: Collection[ExtensionHook] = frozenset(
            {ExtensionHook.BEFORE_TOOL, ExtensionHook.AFTER_TOOL}
        ),
        max_records: int = 1_000,
    ) -> None:
        if isinstance(hooks, (str, bytes)):
            raise AgentPlatformError(
                "invalid_extension",
                "Audit hooks must be a collection of ExtensionHook values",
            )
        self.hooks = frozenset(hooks)
        if not isinstance(max_records, int) or max_records <= 0:
            raise AgentPlatformError(
                "invalid_extension",
                "Audit max_records must be positive",
            )
        self._max_records = max_records
        self._records: list[Mapping[str, Any]] = []

    async def handle(
        self,
        event: ExtensionEvent,
    ) -> Optional[ExtensionDecision]:
        if event.hook not in self.hooks:
            return None
        record: dict[str, Any] = {
            "event_id": event.event_id,
            "hook": event.hook.value,
            "mode": event.mode.value,
        }
        payload = event.payload
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str):
            record["tool_name"] = tool_name
        self._records.append(record)
        if len(self._records) > self._max_records:
            del self._records[: len(self._records) - self._max_records]
        return None

    def records(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._records)

    def clear(self) -> None:
        self._records.clear()


class OutputSpillExtension:
    """After-tool safety gate that replaces oversized output with a reference.

    Content is never silently discarded: the replacement carries a truncation
    marker and an optional spill reference for the full result.
    """

    extension_id = "builtin.output_spill"
    mode = ExtensionEventMode.SAFETY_DECISION
    hooks = frozenset({ExtensionHook.AFTER_TOOL})
    may_transform_arguments = False

    async def handle(
        self,
        event: ExtensionEvent,
    ) -> Optional[ExtensionDecision]:
        if event.hook is not ExtensionHook.AFTER_TOOL:
            return None
        payload = _payload(event)
        content = _require_text(payload, "content")
        max_characters = _require_int(payload, "max_characters")
        spill_reference = payload.get("spill_reference")
        if spill_reference is not None and not isinstance(spill_reference, str):
            raise AgentPlatformError(
                "invalid_extension_payload",
                "spill_reference must be text",
            )
        if len(content) <= max_characters:
            return ExtensionDecision(
                extension_id=self.extension_id,
                kind=ExtensionDecisionKind.ACCEPT,
                reason="within_output_limit",
            )
        truncated = content[:max_characters]
        if spill_reference:
            marker = f"\n…[输出超长已截断：完整结果见 {spill_reference}]"
        else:
            marker = "\n…[输出超长已截断]"
        return ExtensionDecision(
            extension_id=self.extension_id,
            kind=ExtensionDecisionKind.REPLACE_RESULT,
            reason="output_spilled",
            replacement={
                "content": truncated + marker,
                "is_truncated": True,
                "spill_reference": spill_reference,
            },
        )


class SensitiveValueRedactionExtension:
    """After-tool safety gate that redacts configured secret values.

    Redaction values are configured by the caller and never appear in
    decisions or diagnostics.
    """

    extension_id = "builtin.sensitive_redaction"
    mode = ExtensionEventMode.SAFETY_DECISION
    hooks = frozenset({ExtensionHook.AFTER_TOOL})
    may_transform_arguments = False

    def __init__(self, *, redact_values: Collection[str]) -> None:
        if isinstance(redact_values, (str, bytes)):
            raise AgentPlatformError(
                "invalid_extension",
                "redact_values must be a collection of strings",
            )
        values = tuple(redact_values)
        if not all(isinstance(value, str) and value for value in values):
            raise AgentPlatformError(
                "invalid_extension",
                "redact_values must be non-empty strings",
            )
        self._redact_values = tuple(sorted(set(values)))
        self.hooks = frozenset({ExtensionHook.AFTER_TOOL})

    async def handle(
        self,
        event: ExtensionEvent,
    ) -> Optional[ExtensionDecision]:
        if event.hook is not ExtensionHook.AFTER_TOOL:
            return None
        payload = _payload(event)
        content = _require_text(payload, "content")
        redacted = content
        for value in self._redact_values:
            redacted = redacted.replace(value, _REDACTION_PLACEHOLDER)
        if redacted == content:
            return ExtensionDecision(
                extension_id=self.extension_id,
                kind=ExtensionDecisionKind.ACCEPT,
                reason="no_sensitive_values",
            )
        return ExtensionDecision(
            extension_id=self.extension_id,
            kind=ExtensionDecisionKind.REPLACE_RESULT,
            reason="sensitive_values_redacted",
            replacement={"content": redacted, "redacted": True},
        )


def _payload(event: ExtensionEvent) -> Mapping[str, Any]:
    payload = event.payload
    if not isinstance(payload, Mapping):
        raise AgentPlatformError(
            "invalid_extension_payload",
            "Extension payload must be a JSON object",
        )
    return payload


def _require_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AgentPlatformError(
            "invalid_extension_payload",
            f"{key} must be non-empty text",
        )
    return value


def _require_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AgentPlatformError(
            "invalid_extension_payload",
            f"{key} must be a positive integer",
        )
    return value


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise AgentPlatformError(
            "invalid_extension_payload",
            f"{key} must be boolean",
        )
    return value
