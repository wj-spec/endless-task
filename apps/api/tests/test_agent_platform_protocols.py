from __future__ import annotations

import unittest

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    FakeAgentKernel,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import (
    AgentPlatformError,
    EffectOutcome,
    EffectReceipt,
    SafeDiagnostic,
)
from endless_task.context_engine import ContextBudget
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutcome,
    ChildOutputSchema,
    ChildStatus,
    SpawnSpec,
    WorkspaceMode,
    intersect_capabilities,
)
from endless_task.execution_env import ExecutionPolicy, NetworkMode
from endless_task.extensions import (
    ExtensionDecision,
    ExtensionDecisionKind,
    merge_post_tool_decisions,
    merge_pre_tool_decisions,
)
from endless_task.runtime_ledger import (
    CanonicalUsage,
    FakeRuntimeLedger,
    RuntimeLedgerEvent,
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)
from endless_task.tool_platform import (
    ApprovalPolicy,
    IdempotencyPolicy,
    RetryPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolOutcome,
    ToolOutcomeStatus,
)


def trace_context() -> TraceContext:
    return TraceContext(
        trace_id="trace_1",
        run_id="run_1",
        correlation_id="correlation_1",
        span_id="span_1",
    )


class AgentPlatformProtocolTest(unittest.IsolatedAsyncioTestCase):
    def test_tool_v2_freezes_schema_and_rejects_wrong_version(self) -> None:
        definition = ToolDefinitionV2(
            name="read_file",
            description="Read a workspace file.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            output_schema={"type": "object"},
            effect=ToolEffect.READ_ONLY,
            approval=ApprovalPolicy.AUTO,
            execution_mode=ToolExecutionMode.PARALLEL,
            idempotency=IdempotencyPolicy.SAFE,
            retry_policy=RetryPolicy(max_attempts=2, retryable_error_codes=frozenset({"busy"})),
            required_capabilities=frozenset({"workspace.read"}),
        )

        self.assertEqual(2, definition.protocol_version)
        self.assertEqual("object", definition.input_schema["type"])
        with self.assertRaises(TypeError):
            definition.input_schema["type"] = "string"
        with self.assertRaises(AgentPlatformError) as raised:
            ToolDefinitionV2(
                name="read_file",
                description="Read a workspace file.",
                input_schema={"type": "object"},
                protocol_version=99,
            )
        self.assertEqual("unsupported_protocol_version", raised.exception.code)

    def test_tool_outcome_requires_structured_failure_diagnostic(self) -> None:
        failure = ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.FAILED,
            content="File not found.",
            diagnostic=SafeDiagnostic(
                code="file_not_found",
                safe_message="File not found.",
                retryable=False,
            ),
        )
        self.assertEqual("file_not_found", failure.diagnostic.code)

        with self.assertRaises(ValueError):
            ToolOutcome(
                call_id="call_1",
                status=ToolOutcomeStatus.FAILED,
                content="File not found.",
            )

    def test_effect_receipt_distinguishes_committed_and_unknown(self) -> None:
        committed = EffectReceipt(
            effect_id="effect_1",
            tool_call_id="call_1",
            effect_type="workspace_write",
            target="notes.md",
            started_at="2026-09-02T00:00:00Z",
            committed_at="2026-09-02T00:00:01Z",
            outcome=EffectOutcome.COMMITTED,
            backend="local",
            safe_summary="Updated notes.md",
        )
        unknown = EffectReceipt(
            effect_id="effect_2",
            tool_call_id="call_2",
            effect_type="external_action",
            target="remote_service",
            started_at="2026-09-02T00:00:00Z",
            outcome=EffectOutcome.UNKNOWN,
            backend="remote",
            safe_summary="Remote outcome could not be confirmed",
        )

        self.assertEqual(EffectOutcome.COMMITTED, committed.ref.outcome)
        self.assertEqual(EffectOutcome.UNKNOWN, unknown.ref.outcome)
        with self.assertRaises(AgentPlatformError):
            EffectReceipt(
                effect_id="effect_3",
                tool_call_id="call_3",
                effect_type="workspace_write",
                target="notes.md",
                started_at="2026-09-02T00:00:00Z",
                outcome=EffectOutcome.COMMITTED,
                backend="local",
                safe_summary="Missing commit timestamp",
            )

    def test_extension_decisions_use_deterministic_precedence(self) -> None:
        allow = ExtensionDecision(
            extension_id="audit",
            kind=ExtensionDecisionKind.ALLOW,
            reason="No issue found",
        )
        ask = ExtensionDecision(
            extension_id="approval",
            kind=ExtensionDecisionKind.ASK,
            reason="User confirmation is required",
        )
        deny = ExtensionDecision(
            extension_id="policy",
            kind=ExtensionDecisionKind.DENY,
            reason="Capability denied",
        )
        self.assertIs(deny, merge_pre_tool_decisions((allow, deny, ask)))

        replace = ExtensionDecision(
            extension_id="redaction",
            kind=ExtensionDecisionKind.REPLACE_RESULT,
            reason="Sensitive fields were removed",
            replacement={"content": "redacted"},
        )
        block = ExtensionDecision(
            extension_id="guard",
            kind=ExtensionDecisionKind.BLOCK_RESULT,
            reason="Unsafe result",
        )
        self.assertIs(block, merge_post_tool_decisions((replace, block)))

    def test_context_budget_reserves_output_and_safety_margin(self) -> None:
        budget = ContextBudget(
            window_tokens=32_768,
            reserved_output_tokens=2_048,
            safety_margin_tokens=512,
        )
        self.assertEqual(30_208, budget.input_budget)
        with self.assertRaises(ValueError):
            ContextBudget(window_tokens=100, reserved_output_tokens=100)

    def test_execution_policy_refuses_ambiguous_network_constraints(self) -> None:
        policy = ExecutionPolicy(
            workspace_root="/workspace",
            read_allow_paths=("/workspace",),
            write_allow_paths=(),
            network_mode=NetworkMode.ALLOW_HOSTS,
            allowed_hosts=("example.test",),
        )
        self.assertEqual(("example.test",), policy.allowed_hosts)

        with self.assertRaises(ValueError):
            ExecutionPolicy(
                workspace_root="/workspace",
                read_allow_paths=("/workspace",),
                write_allow_paths=(),
                network_mode=NetworkMode.ALLOW_HOSTS,
            )

    def test_delegation_capabilities_can_only_narrow(self) -> None:
        parent = frozenset({"workspace.read", "workspace.write", "network.outbound"})
        effective = intersect_capabilities(
            parent,
            frozenset({"workspace.read", "credential.use:prod"}),
            frozenset({"workspace.read"}),
            frozenset({"workspace.read", "workspace.write"}),
            frozenset({"workspace.read", "network.outbound"}),
        )
        self.assertEqual(frozenset({"workspace.read"}), effective)
        self.assertTrue(effective <= parent)

        spec = SpawnSpec(
            task="Inspect the module without modifying files.",
            expected_output=ChildOutputSchema(
                name="inspection",
                schema={"type": "object"},
            ),
            capability_profile="subagent_readonly",
            requested_capabilities=frozenset({"workspace.read"}),
            tool_allowlist=("read_file",),
            model_policy=ChildModelPolicy(),
            context_policy=ChildContextPolicy(file_refs=("src/module.py",)),
            workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            timeout_seconds=60,
            parent_run_id="run_1",
            parent_tool_call_id="call_1",
        )
        self.assertEqual(WorkspaceMode.READ_ONLY_SHARED, spec.workspace_mode)

        outcome = ChildOutcome(
            child_run_id="child_1",
            status=ChildStatus.COMPLETED,
            summary="Inspection completed.",
        )
        self.assertEqual(ChildStatus.COMPLETED, outcome.status)

    async def test_agent_kernel_fake_preserves_run_correlation(self) -> None:
        trace = trace_context()
        outcome = RunOutcome(
            run_id="run_1",
            status=RunOutcomeStatus.COMPLETED,
            trace=trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="Done.",
            ),
        )
        kernel = FakeAgentKernel(outcome=outcome)
        command = RunCommand(
            run_id="run_1",
            conversation_id="conversation_1",
            lane_id="lane_1",
            trigger_entry_id="entry_1",
            message=AgentMessage(role=AgentMessageRole.USER, content="Do the work."),
            trace=trace,
            capability_profile="work",
        )

        self.assertIs(outcome, await kernel.run(command))
        self.assertEqual([command], kernel.commands)

    def test_trace_schema_requires_allowlisted_attributes(self) -> None:
        span = SpanSpec(
            trace=trace_context(),
            kind=SpanKind.TOOL,
            name="tool.call",
            started_at="2026-09-02T00:00:00Z",
            monotonic_started=1.0,
            attributes={"tool_name": "read_file", "status": "running"},
        )
        self.assertEqual("read_file", span.attributes["tool_name"])
        with self.assertRaises(ValueError):
            SpanSpec(
                trace=trace_context(),
                kind=SpanKind.TOOL,
                name="tool.call",
                started_at="2026-09-02T00:00:00Z",
                monotonic_started=1.0,
                attributes={"raw_arguments": {"secret": "value"}},
            )

    async def test_fake_runtime_ledger_records_each_protocol_channel(self) -> None:
        trace = trace_context()
        event = RuntimeLedgerEvent(
            event_id="event_1",
            event_type="tool.started",
            occurred_at="2026-09-02T00:00:00Z",
            trace=trace,
            data={"tool": "read_file"},
        )
        span = SpanSpec(
            trace=trace,
            kind=SpanKind.TOOL,
            name="tool.call",
            started_at="2026-09-02T00:00:00Z",
            monotonic_started=1.0,
            attributes={"tool_name": "read_file", "status": "running"},
        )
        receipt = EffectReceipt(
            effect_id="effect_1",
            tool_call_id="call_1",
            effect_type="workspace_write",
            target="notes.md",
            started_at="2026-09-02T00:00:00Z",
            committed_at="2026-09-02T00:00:01Z",
            outcome=EffectOutcome.COMMITTED,
            backend="fake",
            safe_summary="Updated notes.md",
        )
        usage = CanonicalUsage(
            provider="fake",
            model="fixture-model",
            input_tokens=10,
            output_tokens=5,
            cached_input_tokens=2,
            reasoning_tokens=1,
            request_count=1,
            occurred_at="2026-09-02T00:00:01Z",
            trace=trace,
        )
        ledger = FakeRuntimeLedger()

        await ledger.append_event(event)
        handle = ledger.start_span(span)
        await handle.end(
            SpanStatus.COMPLETED,
            ended_at="2026-09-02T00:00:01Z",
            monotonic_ended=2.0,
        )
        await ledger.record_effect(receipt, trace)
        await ledger.record_usage(usage)

        self.assertEqual([event], ledger.events)
        self.assertEqual([(span, handle)], ledger.spans)
        self.assertEqual(trace, handle.context)
        self.assertEqual(
            [(SpanStatus.COMPLETED, "2026-09-02T00:00:01Z", 2.0, None)],
            handle.completions,
        )
        self.assertEqual([(receipt, trace)], ledger.effects)
        self.assertEqual([usage], ledger.usage)


if __name__ == "__main__":
    unittest.main()