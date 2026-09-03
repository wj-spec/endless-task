from __future__ import annotations

import asyncio
import unittest
from typing import Any, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError
from endless_task.extensions import (
    ApprovalPolicyExtension,
    ExtensionDecision,
    ExtensionDecisionKind,
    ExtensionEvent,
    ExtensionEventMode,
    ExtensionHook,
    InProcessExtensionBus,
    SensitiveValueRedactionExtension,
)
from endless_task.tool_platform import (
    ApprovalPolicy,
    ToolBatchRequest,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolNext,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolScheduler,
    canonical_path,
    chain_tool_middleware,
    spill_outcome,
    ResolvedToolCall,
    DefaultToolPipeline,
    ScheduledCall,
)

COMPLETED = ToolOutcomeStatus.COMPLETED
READ_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "lines": {"type": "integer", "minimum": 1},
    },
    "required": ["path"],
    "additionalProperties": False,
}


class CancellationStub:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError()

    async def wait(self) -> None:
        if self._cancelled:
            raise asyncio.CancelledError()


def make_request(
    call_id: str,
    tool_name: str,
    *,
    arguments: Optional[Mapping[str, Any]] = None,
    cancelled: bool = False,
) -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id=call_id,
        tool_name=tool_name,
        arguments=dict(arguments or {}),
        conversation_id="conv_1",
        run_id="run_1",
        model_turn_id="turn_1",
        correlation_id="corr_1",
        cancellation=CancellationStub(cancelled),
        created_at="2026-09-03T00:00:00Z",
    )


def ok_outcome(call_id: str, content: str) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        status=COMPLETED,
        content=content,
    )


class RecordingTool:
    """Tool that records execution order and optional concurrency windows."""

    def __init__(
        self,
        name: str,
        *,
        mode: ToolExecutionMode = ToolExecutionMode.PARALLEL,
        approval: ApprovalPolicy = ApprovalPolicy.AUTO,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        sleep_seconds: float = 0.0,
        max_output_characters: int = 200_000,
        content: str | None = None,
        structured_content: Any = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.definition = ToolDefinitionV2(
            name=name,
            description=f"Use {name}.",
            input_schema=dict(input_schema or {"type": "object"}),
            output_schema=dict(output_schema) if output_schema else None,
            effect=effect,
            approval=approval,
            execution_mode=mode,
            max_output_characters=max_output_characters,
        )
        self._sleep = sleep_seconds
        self._content = content
        self._structured = structured_content
        self._raise_error = raise_error
        self.executions: list[str] = []
        self.windows: list[tuple[float, float]] = []
        self.last_arguments: Mapping[str, Any] = {}

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        self.last_arguments = request.arguments
        started = asyncio.get_running_loop().time()
        self.executions.append(request.call_id)
        if self._sleep:
            await asyncio.sleep(self._sleep)
        ended = asyncio.get_running_loop().time()
        self.windows.append((started, ended))
        if self._raise_error is not None:
            raise self._raise_error
        return ToolOutcome(
            call_id=request.call_id,
            status=COMPLETED,
            content=self._content if self._content is not None else f"done:{request.call_id}",
            structured_content=self._structured,
        )


def resolved(
    tool: RecordingTool,
    request: ToolExecutionRequest,
    *,
    spill_reference: str | None = None,
) -> ResolvedToolCall:
    return ResolvedToolCall(request=request, tool=tool, spill_reference=spill_reference)


def batch(*calls: ResolvedToolCall, unattended: bool = False) -> ToolBatchRequest:
    return ToolBatchRequest(calls=calls, unattended=unattended)


def pipeline(**overrides) -> DefaultToolPipeline:
    values = {}
    values.update(overrides)
    return DefaultToolPipeline(**values)


def completed_statuses(result) -> list[str]:
    return [outcome.status.value for outcome in result.outcomes]


class ExtensionStub:
    def __init__(
        self,
        extension_id: str,
        *,
        kind: ExtensionDecisionKind,
        hook: ExtensionHook,
        may_transform_arguments: bool = False,
        replacement: Mapping[str, Any] | None = None,
        reason: str = "stub",
    ) -> None:
        self.extension_id = extension_id
        self.mode = ExtensionEventMode.SAFETY_DECISION
        self.hooks = frozenset({hook})
        self.may_transform_arguments = may_transform_arguments
        self.kind = kind
        self.replacement = replacement
        self.reason = reason

    async def handle(self, event: ExtensionEvent) -> Optional[ExtensionDecision]:
        return ExtensionDecision(
            extension_id=self.extension_id,
            kind=self.kind,
            reason=self.reason,
            replacement=self.replacement,
        )


class SchedulingSemanticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_run_concurrently(self) -> None:
        tool = RecordingTool("par", sleep_seconds=0.08)
        result = await pipeline().execute(
            batch(
                resolved(tool, make_request("c1", "par")),
                resolved(tool, make_request("c2", "par")),
                resolved(tool, make_request("c3", "par")),
            )
        )
        self.assertEqual(["completed", "completed", "completed"], completed_statuses(result))
        self.assertEqual(3, len(tool.executions))
        first_start = min(start for start, _ in tool.windows)
        last_end = max(end for _, end in tool.windows)
        # Sequential execution would take >= 0.24s; concurrent < 0.20s.
        self.assertLess(last_end - first_start, 0.20)

    async def test_bounded_concurrency_serializes_under_cap(self) -> None:
        tool = RecordingTool("cap", sleep_seconds=0.05)
        scheduler = ToolScheduler(max_concurrent=1)
        result = await pipeline(scheduler=scheduler).execute(
            batch(
                resolved(tool, make_request("c1", "cap")),
                resolved(tool, make_request("c2", "cap")),
                resolved(tool, make_request("c3", "cap")),
            )
        )
        self.assertEqual(3, len(result.outcomes))
        first_start = min(start for start, _ in tool.windows)
        last_end = max(end for _, end in tool.windows)
        self.assertGreaterEqual(last_end - first_start, 0.14)

    async def test_sequential_keeps_model_order(self) -> None:
        tool = RecordingTool("seq", mode=ToolExecutionMode.SEQUENTIAL, sleep_seconds=0.01)
        result = await pipeline().execute(
            batch(
                resolved(tool, make_request("c1", "seq")),
                resolved(tool, make_request("c2", "seq")),
            )
        )
        self.assertEqual(["c1", "c2"], tool.executions)
        self.assertEqual(["c1", "c2"], [outcome.call_id for outcome in result.outcomes])

    async def test_exclusive_runs_after_parallel_and_single(self) -> None:
        parallel_tool = RecordingTool("par", sleep_seconds=0.04)
        exclusive_tool = RecordingTool(
            "excl", mode=ToolExecutionMode.EXCLUSIVE, sleep_seconds=0.03
        )
        result = await pipeline().execute(
            batch(
                resolved(parallel_tool, make_request("p1", "par")),
                resolved(exclusive_tool, make_request("x1", "excl")),
                resolved(parallel_tool, make_request("p2", "par")),
            )
        )
        self.assertEqual(
            ["p1", "x1", "p2"],
            [outcome.call_id for outcome in result.outcomes],
        )
        exclusive_window = exclusive_tool.windows[0]
        for start, end in parallel_tool.windows:
            self.assertTrue(
                exclusive_window[1] <= start or exclusive_window[0] >= end,
                "exclusive tool must not overlap parallel tools",
            )

    async def test_path_scoped_serializes_same_path_and_parallelizes_different_paths(
        self,
    ) -> None:
        tool = RecordingTool(
            "path", mode=ToolExecutionMode.PATH_SCOPED, sleep_seconds=0.05
        )
        await pipeline().execute(
            batch(
                resolved(tool, make_request("a1", "path", arguments={"path": "/a"})),
                resolved(tool, make_request("a2", "path", arguments={"path": "/a"})),
                resolved(tool, make_request("b1", "path", arguments={"path": "/b"})),
            )
        )

        def window_of(call_id: str) -> tuple[float, float]:
            index = tool.executions.index(call_id)
            return tool.windows[index]

        window_a1, window_a2, window_b1 = (
            window_of("a1"),
            window_of("a2"),
            window_of("b1"),
        )
        # Same path must serialize (no overlap); different path may overlap.
        self.assertTrue(
            window_a1[1] <= window_a2[0] or window_a2[1] <= window_a1[0],
            "same-path calls must not overlap",
        )
        first_start = min(window_a1[0], window_b1[0])
        last_end = max(window_a2[1], window_b1[1])
        # Same-path serialization alone takes ~0.10s; a distinct path may
        # overlap, so the whole batch stays well under sequential time.
        self.assertLess(last_end - first_start, 0.18)

    async def test_cancelled_call_is_aborted_before_dispatch(self) -> None:
        tool = RecordingTool("cancelled")
        result = await pipeline().execute(
            batch(resolved(tool, make_request("c1", "cancelled", cancelled=True)))
        )
        outcome = result.outcomes[0]
        self.assertEqual("cancelled", outcome.status.value)
        self.assertEqual("aborted_before_dispatch", outcome.diagnostic.code)
        self.assertEqual([], tool.executions)

    async def test_outcomes_commit_in_model_order_under_parallel(self) -> None:
        fast = RecordingTool("fast", sleep_seconds=0.02)
        slow = RecordingTool("slow", sleep_seconds=0.08)
        result = await pipeline().execute(
            batch(
                resolved(slow, make_request("first", "slow")),
                resolved(fast, make_request("second", "fast")),
            )
        )
        self.assertEqual(["first", "second"], [o.call_id for o in result.outcomes])


class PipelineStageTest(unittest.IsolatedAsyncioTestCase):
    async def test_input_schema_violation_fails_closed(self) -> None:
        tool = RecordingTool("reader", input_schema=READ_TOOL_SCHEMA)
        result = await pipeline().execute(
            batch(resolved(tool, make_request("c1", "reader", arguments={"path": 5})))
        )
        outcome = result.outcomes[0]
        self.assertEqual("failed", outcome.status.value)
        self.assertEqual("invalid_tool_arguments", outcome.diagnostic.code)
        self.assertEqual([], tool.executions)

    async def test_output_schema_violation_fails_when_enabled(self) -> None:
        tool = RecordingTool(
            "structured",
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            structured_content={"ok": "not-a-bool"},
        )
        result = await pipeline().execute(
            batch(resolved(tool, make_request("c1", "structured")))
        )
        self.assertEqual("failed", result.outcomes[0].status.value)
        self.assertEqual("output_schema_violation", result.outcomes[0].diagnostic.code)

    async def test_output_schema_validation_can_be_disabled(self) -> None:
        tool = RecordingTool(
            "structured",
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            structured_content={"ok": "not-a-bool"},
        )
        result = await pipeline(validate_output_schema=False).execute(
            batch(resolved(tool, make_request("c1", "structured")))
        )
        self.assertEqual("completed", result.outcomes[0].status.value)

    async def test_raised_tool_maps_to_unknown_outcome(self) -> None:
        tool = RecordingTool("boom", raise_error=RuntimeError("kaboom"))
        result = await pipeline().execute(
            batch(resolved(tool, make_request("c1", "boom")))
        )
        outcome = result.outcomes[0]
        self.assertEqual("unknown", outcome.status.value)
        self.assertEqual("tool_execution_unknown", outcome.diagnostic.code)

    async def test_deny_extension_blocks_execution(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            ExtensionStub(
                "denier",
                kind=ExtensionDecisionKind.DENY,
                hook=ExtensionHook.BEFORE_TOOL,
            )
        )
        tool = RecordingTool("reader")
        result = await pipeline(extension_bus=bus).execute(
            batch(resolved(tool, make_request("c1", "reader")))
        )
        outcome = result.outcomes[0]
        self.assertEqual("rejected", outcome.status.value)
        self.assertEqual("denied_by_extension", outcome.diagnostic.code)
        self.assertEqual([], tool.executions)

    async def test_ask_without_handler_fails_closed(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        tool = RecordingTool(
            "writer",
            approval=ApprovalPolicy.REQUIRED,
            effect=ToolEffect.LOCAL_WRITE,
        )
        result = await pipeline(extension_bus=bus).execute(
            batch(resolved(tool, make_request("c1", "writer")))
        )
        outcome = result.outcomes[0]
        self.assertEqual("rejected", outcome.status.value)
        self.assertEqual("approval_required", outcome.diagnostic.code)
        self.assertEqual([], tool.executions)

    async def test_ask_with_handler_allows_execution(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(ApprovalPolicyExtension())
        tool = RecordingTool(
            "writer",
            approval=ApprovalPolicy.REQUIRED,
            effect=ToolEffect.LOCAL_WRITE,
        )
        handler_asked = []

        class YesHandler:
            async def ask(self, request, definition, reason) -> bool:
                handler_asked.append(reason)
                return True

        result = await pipeline(extension_bus=bus, approval_handler=YesHandler()).execute(
            batch(resolved(tool, make_request("c1", "writer")))
        )
        self.assertEqual("completed", result.outcomes[0].status.value)
        self.assertEqual(["approval_required"], handler_asked)

    async def test_trusted_argument_replacement_is_applied_to_execution(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            ExtensionStub(
                "replacer",
                kind=ExtensionDecisionKind.REPLACE_ARGUMENTS,
                hook=ExtensionHook.BEFORE_TOOL,
                may_transform_arguments=True,
                replacement={"path": "/safe/path.txt"},
            )
        )
        tool = RecordingTool("writer", input_schema=READ_TOOL_SCHEMA)
        result = await pipeline(extension_bus=bus).execute(
            batch(
                resolved(
                    tool,
                    make_request(
                        "c1", "writer", arguments={"path": "/unsafe/path.txt"}
                    ),
                )
            )
        )
        self.assertEqual("completed", result.outcomes[0].status.value)
        self.assertEqual("/safe/path.txt", tool.last_arguments["path"])

    async def test_after_redaction_replaces_result_content(self) -> None:
        bus = InProcessExtensionBus()
        secret = "sk-live-abc"
        bus.register(SensitiveValueRedactionExtension(redact_values=(secret,)))
        tool = RecordingTool("shell", content=f"curl {secret}")
        result = await pipeline(extension_bus=bus).execute(
            batch(resolved(tool, make_request("c1", "shell")))
        )
        outcome = result.outcomes[0]
        self.assertEqual("completed", outcome.status.value)
        self.assertIn("[REDACTED]", outcome.content)
        self.assertNotIn(secret, outcome.content)

    async def test_after_block_result_rejects_outcome(self) -> None:
        bus = InProcessExtensionBus()
        bus.register(
            ExtensionStub(
                "blocker",
                kind=ExtensionDecisionKind.BLOCK_RESULT,
                hook=ExtensionHook.AFTER_TOOL,
            )
        )
        tool = RecordingTool("reader", content="content")
        result = await pipeline(extension_bus=bus).execute(
            batch(resolved(tool, make_request("c1", "reader")))
        )
        outcome = result.outcomes[0]
        self.assertEqual("rejected", outcome.status.value)
        self.assertEqual("result_blocked_by_extension", outcome.diagnostic.code)

    async def test_observation_failure_does_not_block_execution(self) -> None:
        class RaisingObserver:
            extension_id = "observer"
            mode = ExtensionEventMode.OBSERVATION
            hooks = frozenset({ExtensionHook.BEFORE_TOOL})
            may_transform_arguments = False

            async def handle(self, event: ExtensionEvent) -> None:
                raise RuntimeError("audit backend down")

        bus = InProcessExtensionBus()
        bus.register(RaisingObserver())
        tool = RecordingTool("reader")
        result = await pipeline(extension_bus=bus).execute(
            batch(resolved(tool, make_request("c1", "reader")))
        )
        self.assertEqual("completed", result.outcomes[0].status.value)
        self.assertEqual(["c1"], tool.executions)

    async def test_fallback_spill_truncates_oversized_completed_outcome(self) -> None:
        tool = RecordingTool(
            "big",
            content="x" * 500,
            max_output_characters=100,
        )
        result = await pipeline().execute(
            batch(
                resolved(
                    tool,
                    make_request("c1", "big"),
                    spill_reference="spill://run_1/big",
                )
            )
        )
        outcome = result.outcomes[0]
        self.assertEqual("completed", outcome.status.value)
        self.assertTrue(outcome.is_truncated)
        self.assertTrue(outcome.content.startswith("x" * 100))
        self.assertIn("spill://run_1/big", outcome.content)

    async def test_around_hooks_compose_outermost_first(self) -> None:
        calls: list[str] = []

        class AroundHook:
            def __init__(self, label: str) -> None:
                self.label = label

            async def around_tool(self, event: ExtensionEvent, next: ToolNext) -> ToolOutcome:
                calls.append(f"{self.label}:before")
                outcome = await next()
                calls.append(f"{self.label}:after")
                return outcome

        tool = RecordingTool("reader")
        result = await pipeline(
            around_hooks=(AroundHook("outer"), AroundHook("inner")),
        ).execute(batch(resolved(tool, make_request("c1", "reader"))))
        self.assertEqual("completed", result.outcomes[0].status.value)
        self.assertEqual(
            ["outer:before", "inner:before", "inner:after", "outer:after"],
            calls,
        )


class PipelineUnitTest(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_chain_replacement(self) -> None:
        class ReplacingMiddleware:
            async def __call__(self, event: object, next: ToolNext) -> ToolOutcome:
                await next()
                return ok_outcome("replaced", "wrapper-result")

        terminal_called = []

        async def terminal() -> ToolOutcome:
            terminal_called.append(True)
            return ok_outcome("terminal", "original")

        chain = chain_tool_middleware((ReplacingMiddleware(),), terminal)
        outcome = await chain(object())
        self.assertEqual("wrapper-result", outcome.content)
        self.assertEqual([True], terminal_called)

    def test_canonical_path_extraction(self) -> None:
        self.assertEqual("/a", canonical_path({"path": "/a"}))
        self.assertEqual("/a", canonical_path({"file_path": "/a"}))
        self.assertIsNone(canonical_path({"path": 3}))
        self.assertIsNone(canonical_path({}))

    def test_spill_outcome_keeps_completed_short_content(self) -> None:
        outcome = ok_outcome("c1", "short")
        spilled = spill_outcome(outcome, max_characters=100)
        self.assertFalse(spilled.is_truncated)
        self.assertEqual("short", spilled.content)

    def test_batch_requires_matching_tool_name(self) -> None:
        tool = RecordingTool("reader")
        with self.assertRaises(AgentPlatformError):
            resolved(tool, make_request("c1", "other_name"))


class SchedulerDirectTest(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_direct_ordered_results_and_abort(self) -> None:
        async def run(call_id: str) -> ToolOutcome:
            return ok_outcome(call_id, f"done:{call_id}")

        scheduler = ToolScheduler()
        outcomes = await scheduler.execute(
            (
                ScheduledCall(
                    index=0,
                    call_id="c1",
                    tool_name="tool",
                    mode=ToolExecutionMode.SEQUENTIAL,
                    runner=lambda: run("c1"),
                ),
                ScheduledCall(
                    index=1,
                    call_id="c2",
                    tool_name="tool",
                    mode=ToolExecutionMode.SEQUENTIAL,
                    runner=lambda: run("c2"),
                ),
            )
        )
        self.assertEqual(["c1", "c2"], [o.call_id for o in outcomes])
        self.assertEqual(["done:c1", "done:c2"], [o.content for o in outcomes])


if __name__ == "__main__":
    unittest.main()
