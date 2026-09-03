from __future__ import annotations

import unittest

from endless_task.agent_platform import EffectOutcome, EffectReceipt
from endless_task.context_engine import (
    ContextBudget,
    ContextRequest,
    LegacyContextEngineAdapter,
    assert_context_parity,
)
from endless_task.execution_env import (
    ExecutionPolicy,
    FakeExecutionEnvironment,
    FileMutationOperation,
    FileMutationRequest,
    FileReadResult,
    ReadFileRequest,
    assert_execution_environment_conformance,
)
from endless_task.files import StoredTextFile, UploadedTextFile
from endless_task.files.read_tool import ReadTextFileTool
from endless_task.runtime import ApproximateTokenEstimator, FakeProvider
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import ProviderMessage, ProviderRequest
from endless_task.runtime_ledger import TraceContext
from endless_task.tool_platform import (
    ApprovalPolicy,
    FakeTool,
    IdempotencyPolicy,
    LegacyToolAdapter,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
    assert_tool_conformance,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect as LegacyToolEffect,
    ToolError,
    ToolResult,
)


class LegacyEchoTool:
    definition = ToolDefinition(
        name="legacy_echo",
        description="Echo a value through the legacy tool contract.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        effect=LegacyToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=12,
        max_output_characters=4_096,
    )

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    async def execute(self, call, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.calls.append(call)
        value = call.require_argument("value", str)
        return ToolResult(
            tool_call_id=call.id,
            content=value,
            structured_content={"echo": value},
        )


class LegacyFailureTool:
    definition = ToolDefinition(
        name="legacy_failure",
        description="Raise a normalized legacy failure.",
        input_schema={"type": "object"},
    )

    async def execute(self, call, cancellation_token):
        del call, cancellation_token
        raise ToolError(
            "legacy_busy",
            "Legacy tool is temporarily busy.",
            retryable=True,
        )


class StaticTextFileRepository:
    def __init__(self, stored: StoredTextFile) -> None:
        self._stored = stored

    def get_file(self, *, conversation_id: str, file_id: str) -> StoredTextFile:
        if (
            conversation_id != self._stored.metadata.conversation_id
            or file_id != self._stored.metadata.id
        ):
            raise AssertionError("Unexpected fixture lookup")
        return self._stored


class AgentPlatformCompatibilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.trace = TraceContext(
            trace_id="trace_1",
            run_id="run_1",
            correlation_id="correlation_1",
            span_id="span_1",
            model_turn_id="model_turn_1",
        )

    def tool_request(self, name: str, arguments=None) -> ToolExecutionRequest:
        return ToolExecutionRequest(
            call_id="call_1",
            tool_name=name,
            arguments=arguments or {},
            conversation_id="conversation_1",
            run_id="run_1",
            model_turn_id="model_turn_1",
            correlation_id="correlation_1",
            cancellation=CancellationToken(),
            created_at="2026-09-02T00:00:00Z",
            legacy_turn_id="turn_1",
            legacy_response_variant_id="variant_1",
        )

    async def test_legacy_tool_definition_and_result_keep_v1_behavior(self) -> None:
        legacy = LegacyEchoTool()
        adapter = LegacyToolAdapter(legacy)

        self.assertEqual("legacy_echo", adapter.definition.name)
        self.assertEqual(ToolEffect.READ_ONLY, adapter.definition.effect)
        self.assertEqual(ApprovalPolicy.AUTO, adapter.definition.approval)
        self.assertEqual(ToolExecutionMode.PARALLEL, adapter.definition.execution_mode)
        self.assertEqual(IdempotencyPolicy.SAFE, adapter.definition.idempotency)
        self.assertEqual(12, adapter.definition.timeout_seconds)
        self.assertEqual(4_096, adapter.definition.max_output_characters)

        request = self.tool_request("legacy_echo", {"value": "same-result"})
        outcome = await assert_tool_conformance(adapter, request)

        self.assertEqual(ToolOutcomeStatus.COMPLETED, outcome.status)
        self.assertEqual("same-result", outcome.content)
        self.assertEqual("same-result", outcome.structured_content["echo"])
        self.assertEqual("turn_1", legacy.calls[0].turn_id)
        self.assertEqual("variant_1", legacy.calls[0].response_variant_id)

    async def test_builtin_read_tool_matches_legacy_and_adapter_paths(self) -> None:
        stored = StoredTextFile(
            metadata=UploadedTextFile(
                id="file_1",
                conversation_id="conversation_1",
                original_name="notes.md",
                media_type="text/markdown",
                byte_size=18,
                sha256="fixture_sha256",
                created_at="2026-09-02T00:00:00Z",
            ),
            content="first\nsecond\nthird",
        )
        tool = ReadTextFileTool(StaticTextFileRepository(stored))
        request = self.tool_request(
            "read_text_file",
            {"file_id": "file_1", "start_line": 2, "line_count": 1},
        )
        legacy_call = ToolCall(
            id="call_1",
            conversation_id="conversation_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            tool_name="read_text_file",
            arguments=request.arguments,
            status=ToolCallStatus.CREATED,
            created_at=request.created_at,
        )

        legacy_result = await tool.execute(legacy_call, CancellationToken())
        adapted_result = await LegacyToolAdapter(tool).execute(request)

        self.assertEqual(ToolOutcomeStatus.COMPLETED, adapted_result.status)
        self.assertEqual(legacy_result.tool_call_id, adapted_result.call_id)
        self.assertEqual(legacy_result.content, adapted_result.content)
        self.assertEqual(legacy_result.structured_content, adapted_result.structured_content)
        self.assertEqual(legacy_result.is_truncated, adapted_result.is_truncated)
        self.assertEqual(legacy_result.terminate, adapted_result.terminate)
        self.assertIsNone(adapted_result.diagnostic)

    async def test_fake_tool_captures_requests_and_propagates_cancellation(self) -> None:
        outcome = ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.COMPLETED,
            content="fixture result",
            structured_content={"source": "fake"},
        )
        tool = FakeTool(
            definition=ToolDefinitionV2(
                name="fixture_tool",
                description="Deterministic tool fixture.",
                input_schema={"type": "object"},
            ),
            outcome=outcome,
        )
        request = self.tool_request("fixture_tool")

        self.assertIs(outcome, await assert_tool_conformance(tool, request))
        self.assertEqual([request], tool.requests)

        cancelled = self.tool_request("fixture_tool")
        cancelled.cancellation.cancel()
        with self.assertRaises(RuntimeCancelled):
            await tool.execute(cancelled)
        self.assertEqual([request], tool.requests)

    async def test_legacy_tool_error_is_normalized_without_raw_exception(self) -> None:
        adapter = LegacyToolAdapter(LegacyFailureTool())
        outcome = await adapter.execute(self.tool_request("legacy_failure"))

        self.assertEqual(ToolOutcomeStatus.FAILED, outcome.status)
        self.assertEqual("legacy_busy", outcome.diagnostic.code)
        self.assertTrue(outcome.diagnostic.retryable)
        self.assertEqual("Legacy tool is temporarily busy.", outcome.content)

    async def test_legacy_tool_cancellation_is_not_normalized_as_failure(self) -> None:
        adapter = LegacyToolAdapter(LegacyEchoTool())
        request = self.tool_request("legacy_echo", {"value": "cancelled"})
        request.cancellation.cancel()

        with self.assertRaises(RuntimeCancelled):
            await adapter.execute(request)

    async def test_legacy_context_adapter_preserves_messages_exactly(self) -> None:
        messages = (
            ProviderMessage(role="system", content="System baseline"),
            ProviderMessage(role="user", content="Inspect the repository"),
        )
        estimator = ApproximateTokenEstimator()
        engine = LegacyContextEngineAdapter(
            lambda request: messages,
            estimate_tokens=estimator.estimate_messages,
        )
        request = ContextRequest(
            run_id="run_1",
            model_turn_id="model_turn_1",
            lane_id="lane_1",
            budget=ContextBudget(
                window_tokens=8_192,
                reserved_output_tokens=1_024,
            ),
            trace=self.trace,
        )

        first = await assert_context_parity(engine, request, messages)
        second = await assert_context_parity(engine, request, messages)

        self.assertEqual(messages, first.messages)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(estimator.estimate_messages(messages), first.estimated_tokens)

    async def test_fake_execution_environment_passes_conformance_harness(self) -> None:
        policy = ExecutionPolicy(
            workspace_root="/workspace",
            read_allow_paths=("/workspace",),
            write_allow_paths=("/workspace",),
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
        backend = FakeExecutionEnvironment(
            file_read_result=FileReadResult(
                path="notes.md",
                content="before",
                content_hash="hash_before",
            ),
            mutation_receipt=receipt,
        )
        read_request = ReadFileRequest(path="notes.md", policy=policy, trace=self.trace)
        mutation_request = FileMutationRequest(
            effect_id="effect_1",
            tool_call_id="call_1",
            path="notes.md",
            operation=FileMutationOperation.WRITE,
            policy=policy,
            trace=self.trace,
            requested_at="2026-09-02T00:00:00Z",
            content="after",
        )

        result = await assert_execution_environment_conformance(
            backend,
            read_request=read_request,
            mutation_request=mutation_request,
        )
        self.assertIs(receipt, result)
        self.assertEqual([read_request], backend.reads)
        self.assertEqual([mutation_request], backend.mutations)

    async def test_existing_fake_provider_remains_the_protocol_fake(self) -> None:
        provider = FakeProvider(chunks=("one", "two"), input_tokens=3, output_tokens=2)
        request = ProviderRequest(
            request_id="request_1",
            model="fake-model",
            messages=(ProviderMessage(role="user", content="hello"),),
            max_output_tokens=32,
        )
        events = [event async for event in provider.stream(request, CancellationToken())]

        self.assertEqual("fake", provider.name)
        self.assertEqual([request], provider.requests)
        self.assertEqual("one", events[0].text)
        self.assertEqual("two", events[1].text)
        self.assertEqual(3, events[2].input_tokens)
        self.assertEqual(2, events[2].output_tokens)


if __name__ == "__main__":
    unittest.main()