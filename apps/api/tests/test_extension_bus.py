from __future__ import annotations

import unittest
from typing import Any, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError
from endless_task.extensions import (
    ApprovalPolicyExtension,
    AuditExtension,
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
    InProcessExtensionBus,
    OutputSpillExtension,
    SensitiveValueRedactionExtension,
)
from endless_task.runtime_ledger import TraceContext

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")


def event(
    hook: ExtensionHook,
    *,
    mode: ExtensionEventMode,
    payload: Optional[Mapping[str, Any]] = None,
    event_id: str | None = None,
) -> ExtensionEvent:
    return ExtensionEvent(
        event_id=event_id or f"evt_{hook.value}",
        hook=hook,
        mode=mode,
        trace=TRACE,
        payload=dict(payload or {}),
    )


def approval_event(
    *,
    approval_mode: str,
    unattended: bool,
    tool_name: str = "write_file",
) -> ExtensionEvent:
    return event(
        ExtensionHook.BEFORE_TOOL,
        mode=ExtensionEventMode.SAFETY_DECISION,
        payload={
            "tool_name": tool_name,
            "approval_mode": approval_mode,
            "unattended": unattended,
        },
    )


class StubExtension:
    def __init__(
        self,
        extension_id: str,
        *,
        mode: ExtensionEventMode,
        kind: Optional[ExtensionDecisionKind] = None,
        hooks=(
            ExtensionHook.BEFORE_TOOL,
            ExtensionHook.AFTER_TOOL,
        ),
        may_transform_arguments: bool = False,
        raise_on_hook: Optional[ExtensionHook] = None,
        replacement: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.extension_id = extension_id
        self.mode = mode
        self.hooks = frozenset(hooks)
        self.may_transform_arguments = may_transform_arguments
        self.kind = kind
        self.raise_on_hook = raise_on_hook
        self.replacement = replacement
        self.calls: list[str] = []

    async def handle(self, event: ExtensionEvent) -> Optional[ExtensionDecision]:
        if event.hook not in self.hooks:
            return None
        self.calls.append(event.hook.value)
        if self.raise_on_hook is event.hook:
            raise RuntimeError(f"{self.extension_id} failed")
        if self.kind is None:
            return None
        needs_replacement = self.kind in {
            ExtensionDecisionKind.REPLACE_ARGUMENTS,
            ExtensionDecisionKind.REPLACE_RESULT,
        }
        return ExtensionDecision(
            extension_id=self.extension_id,
            kind=self.kind,
            reason=f"reason_{self.extension_id}",
            replacement=(self.replacement or {"value": 1}) if needs_replacement else None,
        )


class InProcessExtensionBusTest(unittest.IsolatedAsyncioTestCase):
    async def test_observation_event_only_invokes_observation_extensions(self) -> None:
        bus = InProcessExtensionBus()
        observer = StubExtension(
            "observer",
            mode=ExtensionEventMode.OBSERVATION,
        )
        decider = StubExtension(
            "decider",
            mode=ExtensionEventMode.SAFETY_DECISION,
            kind=ExtensionDecisionKind.DENY,
        )
        bus.register(observer)
        bus.register(decider)

        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.OBSERVATION)
        )

        self.assertEqual(["before_tool"], observer.calls)
        self.assertEqual([], decider.calls)
        self.assertIsNone(result.decision)
        self.assertEqual((), result.diagnostics)

    async def test_safety_event_invokes_observation_and_safety_extensions(self) -> None:
        bus = InProcessExtensionBus()
        observer = StubExtension(
            "observer",
            mode=ExtensionEventMode.OBSERVATION,
        )
        decider = StubExtension(
            "decider",
            mode=ExtensionEventMode.SAFETY_DECISION,
            kind=ExtensionDecisionKind.ALLOW,
        )
        bus.register(observer)
        bus.register(decider)

        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )

        self.assertEqual(["before_tool"], observer.calls)
        self.assertEqual(["before_tool"], decider.calls)
        self.assertIsNotNone(result.decision)
        self.assertEqual(ExtensionDecisionKind.ALLOW, result.decision.kind)

    async def test_pre_tool_merge_deny_wins_over_ask_and_allow(self) -> None:
        bus = InProcessExtensionBus()
        for name, kind in (
            ("allow_a", ExtensionDecisionKind.ALLOW),
            ("ask_b", ExtensionDecisionKind.ASK),
            ("deny_c", ExtensionDecisionKind.DENY),
        ):
            bus.register(
                StubExtension(
                    name,
                    mode=ExtensionEventMode.SAFETY_DECISION,
                    kind=kind,
                )
            )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual("deny_c", result.decision.extension_id)

    async def test_pre_tool_merge_ask_wins_over_allow(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension("allow", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.ALLOW)
        )
        bus.register(
            StubExtension("ask", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.ASK)
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.ASK, result.decision.kind)
        self.assertEqual("ask", result.decision.extension_id)

    async def test_post_tool_merge_block_wins_over_replace_and_accept(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension("accept", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.ACCEPT)
        )
        bus.register(
            StubExtension("replace", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.REPLACE_RESULT)
        )
        bus.register(
            StubExtension("block", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.BLOCK_RESULT)
        )
        result = await bus.dispatch(
            event(ExtensionHook.AFTER_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.BLOCK_RESULT, result.decision.kind)

    async def test_same_kind_merge_is_deterministic_in_registration_order(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension("allow_one", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.ALLOW)
        )
        bus.register(
            StubExtension("allow_two", mode=ExtensionEventMode.SAFETY_DECISION, kind=ExtensionDecisionKind.ALLOW)
        )
        first = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        second = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        # Same-kind ties resolve to the earliest registered extension.
        self.assertEqual("allow_one", first.decision.extension_id)
        self.assertEqual(first.decision, second.decision)

    async def test_observation_failure_is_isolated_and_never_closes_tool(self) -> None:
        bus = InProcessExtensionBus()
        failing = StubExtension(
            "failing_observer",
            mode=ExtensionEventMode.OBSERVATION,
            raise_on_hook=ExtensionHook.BEFORE_TOOL,
        )
        healthy = StubExtension(
            "healthy_observer",
            mode=ExtensionEventMode.OBSERVATION,
        )
        decider = StubExtension(
            "decider",
            mode=ExtensionEventMode.SAFETY_DECISION,
            kind=ExtensionDecisionKind.ALLOW,
        )
        bus.register(failing)
        bus.register(healthy)
        bus.register(decider)

        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )

        self.assertEqual(["before_tool"], healthy.calls)
        self.assertEqual(ExtensionDecisionKind.ALLOW, result.decision.kind)
        self.assertEqual(1, len(result.diagnostics))
        self.assertEqual("extension_error", result.diagnostics[0].code)

    async def test_safety_extension_failure_fails_closed_deny(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "fragile",
                mode=ExtensionEventMode.SAFETY_DECISION,
                kind=ExtensionDecisionKind.ALLOW,
                raise_on_hook=ExtensionHook.BEFORE_TOOL,
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual("extension_failure_fail_closed", result.decision.reason)
        self.assertEqual(1, len(result.diagnostics))

    async def test_safety_extension_failure_fails_closed_block_result(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "fragile",
                mode=ExtensionEventMode.SAFETY_DECISION,
                raise_on_hook=ExtensionHook.AFTER_TOOL,
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.AFTER_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.BLOCK_RESULT, result.decision.kind)
        self.assertEqual("extension_failure_fail_closed", result.decision.reason)

    async def test_invalid_decision_kind_for_stage_fails_closed(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "wrong_stage",
                mode=ExtensionEventMode.SAFETY_DECISION,
                kind=ExtensionDecisionKind.ACCEPT,
                hooks=frozenset({ExtensionHook.BEFORE_TOOL}),
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual(1, len(result.diagnostics))
        self.assertEqual(
            "invalid_extension_decision_stage",
            result.diagnostics[0].code,
        )

    async def test_observation_extension_returning_decision_is_ignored(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "misbehaving_observer",
                mode=ExtensionEventMode.OBSERVATION,
                kind=ExtensionDecisionKind.ALLOW,
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.OBSERVATION)
        )
        self.assertIsNone(result.decision)
        self.assertEqual(1, len(result.diagnostics))
        self.assertEqual(
            "observation_extension_returned_decision",
            result.diagnostics[0].code,
        )

    async def test_untrusted_argument_replacement_is_denied(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "untrusted",
                mode=ExtensionEventMode.SAFETY_DECISION,
                kind=ExtensionDecisionKind.REPLACE_ARGUMENTS,
                replacement={"path": "/tmp/safe.txt"},
                may_transform_arguments=False,
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual(1, len(result.diagnostics))
        self.assertEqual("untrusted_argument_replacement_denied", result.diagnostics[0].code)

    async def test_trusted_argument_replacement_is_honored(self) -> None:
        bus = InProcessExtensionBus()
        replacement = {"path": "/tmp/safe.txt"}
        bus.register(
            StubExtension(
                "trusted",
                mode=ExtensionEventMode.SAFETY_DECISION,
                kind=ExtensionDecisionKind.REPLACE_ARGUMENTS,
                replacement=replacement,
                may_transform_arguments=True,
            )
        )
        result = await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(ExtensionDecisionKind.REPLACE_ARGUMENTS, result.decision.kind)
        self.assertEqual(replacement, dict(result.decision.replacement or {}))

    async def test_safety_decision_on_non_tool_hook_is_rejected(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            StubExtension(
                "decider",
                mode=ExtensionEventMode.SAFETY_DECISION,
                hooks=frozenset({ExtensionHook.SESSION_START}),
                kind=ExtensionDecisionKind.DENY,
            )
        )
        with self.assertRaises(AgentPlatformError):
            await bus.dispatch(
                event(ExtensionHook.SESSION_START, mode=ExtensionEventMode.SAFETY_DECISION)
            )

    def test_registration_validation_and_subscription_scoping(self) -> None:
        bus = InProcessExtensionBus()
        extension = StubExtension(
            "scoped",
            mode=ExtensionEventMode.SAFETY_DECISION,
            kind=ExtensionDecisionKind.ALLOW,
            hooks=frozenset({ExtensionHook.AFTER_TOOL}),
        )
        registration = bus.register(extension)
        self.assertEqual(("after_tool",), registration.hooks)
        self.assertEqual(1, len(bus.registrations()))
        with self.assertRaises(AgentPlatformError):
            bus.register(extension)
        with self.assertRaises(AgentPlatformError):
            bus.register(
                StubExtension(
                    "bad_mode",
                    mode="observation",
                    hooks=frozenset({ExtensionHook.BEFORE_TOOL}),
                )
            )

    async def test_registration_hooks_limit_dispatch(self) -> None:
        bus = InProcessExtensionBus()
        decider = StubExtension(
            "only_after",
            mode=ExtensionEventMode.SAFETY_DECISION,
            kind=ExtensionDecisionKind.BLOCK_RESULT,
            hooks=frozenset({ExtensionHook.AFTER_TOOL}),
        )
        bus.register(decider)
        await bus.dispatch(
            event(ExtensionHook.BEFORE_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual([], decider.calls)
        await bus.dispatch(
            event(ExtensionHook.AFTER_TOOL, mode=ExtensionEventMode.SAFETY_DECISION)
        )
        self.assertEqual(["after_tool"], decider.calls)


class ApprovalPolicyExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def test_forbidden_unattended_is_denied_in_unattended_run(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        result = await bus.dispatch(
            approval_event(approval_mode="forbidden_unattended", unattended=True)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual("forbidden_unattended", result.decision.reason)

    async def test_forbidden_unattended_asks_in_attended_run(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        result = await bus.dispatch(
            approval_event(approval_mode="forbidden_unattended", unattended=False)
        )
        self.assertEqual(ExtensionDecisionKind.ASK, result.decision.kind)

    async def test_required_and_ask_produce_approval_question(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        for approval_mode in ("required", "ask"):
            with self.subTest(approval_mode=approval_mode):
                result = await bus.dispatch(
                    approval_event(approval_mode=approval_mode, unattended=True)
                )
                self.assertEqual(ExtensionDecisionKind.ASK, result.decision.kind)

    async def test_auto_is_allowed(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        result = await bus.dispatch(
            approval_event(approval_mode="auto", unattended=False)
        )
        self.assertEqual(ExtensionDecisionKind.ALLOW, result.decision.kind)

    async def test_unknown_approval_mode_fails_closed(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        result = await bus.dispatch(
            approval_event(approval_mode="sometimes", unattended=False)
        )
        self.assertEqual(ExtensionDecisionKind.DENY, result.decision.kind)
        self.assertEqual(1, len(result.diagnostics))


class AuditExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def test_audit_records_bounded_event_summaries(self) -> None:
        bus = InProcessExtensionBus()
        audit = AuditExtension(max_records=2)
        bus.register(audit)
        for index in range(3):
            await bus.dispatch(
                event(
                    ExtensionHook.BEFORE_TOOL,
                    mode=ExtensionEventMode.SAFETY_DECISION,
                    payload={"tool_name": f"tool_{index}"},
                    event_id=f"evt_{index}",
                )
            )
        records = audit.records()
        self.assertEqual(2, len(records))
        self.assertEqual("evt_2", records[-1]["event_id"])
        self.assertEqual("before_tool", records[0]["hook"])
        self.assertEqual("tool_1", records[0]["tool_name"])
        audit.clear()
        self.assertEqual((), audit.records())


class OutputSpillExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def test_oversized_output_is_replaced_with_spill_reference(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(OutputSpillExtension())
        content = "x" * 120
        result = await bus.dispatch(
            event(
                ExtensionHook.AFTER_TOOL,
                mode=ExtensionEventMode.SAFETY_DECISION,
                payload={
                    "tool_name": "read_file",
                    "content": content,
                    "max_characters": 100,
                    "spill_reference": "spill://run_1/read_file",
                },
            )
        )
        self.assertEqual(ExtensionDecisionKind.REPLACE_RESULT, result.decision.kind)
        replacement = dict(result.decision.replacement or {})
        self.assertTrue(replacement["is_truncated"])
        # The first max_characters remain intact and a spill marker is appended.
        self.assertTrue(replacement["content"].startswith("x" * 100))
        self.assertIn("spill://run_1/read_file", replacement["content"])
        self.assertIn("截断", replacement["content"])

    async def test_within_limit_is_accepted(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(OutputSpillExtension())
        result = await bus.dispatch(
            event(
                ExtensionHook.AFTER_TOOL,
                mode=ExtensionEventMode.SAFETY_DECISION,
                payload={
                    "tool_name": "read_file",
                    "content": "short",
                    "max_characters": 100,
                },
            )
        )
        self.assertEqual(ExtensionDecisionKind.ACCEPT, result.decision.kind)

    async def test_malformed_payload_fails_closed_block(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(OutputSpillExtension())
        result = await bus.dispatch(
            event(
                ExtensionHook.AFTER_TOOL,
                mode=ExtensionEventMode.SAFETY_DECISION,
                payload={"tool_name": "read_file"},
            )
        )
        self.assertEqual(ExtensionDecisionKind.BLOCK_RESULT, result.decision.kind)
        self.assertEqual(1, len(result.diagnostics))


class SensitiveValueRedactionExtensionTest(unittest.IsolatedAsyncioTestCase):
    async def test_configured_secrets_are_redacted_without_leaking(self) -> None:
        secret = "sk-live-1234567890"
        bus = InProcessExtensionBus()
        bus.register(SensitiveValueRedactionExtension(redact_values=(secret, "other-secret")))
        result = await bus.dispatch(
            event(
                ExtensionHook.AFTER_TOOL,
                mode=ExtensionEventMode.SAFETY_DECISION,
                payload={
                    "tool_name": "run_shell",
                    "content": f"curl -H 'Authorization: {secret}' https://example.com other-secret",
                },
            )
        )
        self.assertEqual(ExtensionDecisionKind.REPLACE_RESULT, result.decision.kind)
        replacement = dict(result.decision.replacement or {})
        self.assertNotIn(secret, replacement["content"])
        self.assertNotIn(secret, result.decision.reason)
        self.assertTrue(replacement["redacted"])

    async def test_clean_content_is_accepted(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(SensitiveValueRedactionExtension(redact_values=("sk-secret",)))
        result = await bus.dispatch(
            event(
                ExtensionHook.AFTER_TOOL,
                mode=ExtensionEventMode.SAFETY_DECISION,
                payload={"tool_name": "read_file", "content": "plain content"},
            )
        )
        self.assertEqual(ExtensionDecisionKind.ACCEPT, result.decision.kind)


if __name__ == "__main__":
    unittest.main()
