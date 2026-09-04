"""M4A DR-1 slice 4: coordinator/scheduler over the production adapter E2E.

First end-to-end proof that the delegation boundary drives the **real**
runtime-v2 execution surface (not a fake kernel): an
:class:`InProcessChildCoordinator` receives a :class:`RuntimeV2AgentKernel`
and a SpawnSpec, the child runs as a genuine repository run through
:class:`AgentRunExecutor`, and the projected ChildOutcome is returned.

This integration deliberately does NOT add an app-level flag or
composition-root wiring yet: the only real consumer of delegation today
would be the spawn_agent tool (DR-2), so a flag would be dead
configuration (02 11.4 discipline). Child conversations are created by
the injected conversation factory; product-visibility filtering belongs
to the composition-root slice.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildRunStatus,
    InProcessChildCoordinator,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RuntimeV2AgentKernel,
    ToolExecutionLimits,
    TranscriptEntryType,
)
from endless_task.runtime_ledger import TraceContext
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "session.query",
        "memory.read",
    }
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class FailingProvider:
    name = "failing"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        raise ProviderError(
            "provider_unavailable",
            "模型服务不可用。",
            retryable=True,
        )
        yield  # pragma: no cover - makes this an async generator


def child_spec(tool_call_id: str = "call_1") -> SpawnSpec:
    return SpawnSpec(
        task="调查并总结",
        expected_output=ChildOutputSchema(
            name="research_output",
            schema={"type": "object"},
        ),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read", "session.query"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=120.0,
        parent_run_id="run_parent_1",
        parent_tool_call_id=tool_call_id,
    )


class CoordinatorOverProductionKernelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.executors_created = 0

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _conversation_factory(self):
        async def factory():
            return self.chat_repository.create_conversation()

        return factory

    def _executor_builder(self, provider):
        def build() -> AgentRunExecutor:
            self.executors_created += 1
            return AgentRunExecutor(
                repository=self.repository,
                provider=provider,
                tool_registry=ToolRegistry(),
                model="scripted-model",
                tool_execution_limits=ToolExecutionLimits(
                    max_concurrent_calls=1,
                    max_calls_per_turn=4,
                ),
            )

        return build

    def _kernel(self, provider) -> RuntimeV2AgentKernel:
        return RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=self._conversation_factory(),
            executor_builder=self._executor_builder(provider),
        )

    def _coordinator(self, kernel) -> InProcessChildCoordinator:
        return InProcessChildCoordinator(
            kernel,
            parent_run_id="run_parent_1",
            parent_capabilities=PARENT_CAPABILITIES,
            parent_trace=TraceContext(
                trace_id="trace_parent_1",
                run_id="run_parent_1",
                correlation_id="corr_1",
            ),
        )

    def test_completed_child_runs_on_real_executor(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("调查完成，结论如下。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            child_run_id = await coordinator.spawn(child_spec())
            outcome = await coordinator.query(child_run_id)
            return child_run_id, outcome

        child_run_id, outcome = asyncio.run(scenario())
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual(child_run_id, outcome.child_run_id)
        self.assertIn("调查完成", outcome.summary)
        # The child really ran on the production surface.
        run = self.repository.get_run(child_run_id)
        from endless_task.runtime_v2 import RunStatus

        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, self.executors_created)
        record = coordinator.children[0]
        self.assertEqual(ChildRunStatus.COMPLETED, record.status)
        self.assertTrue(record.decision.read_only)

    def test_failed_child_returns_failed_outcome_with_diagnostic(self) -> None:
        coordinator = self._coordinator(self._kernel(FailingProvider()))

        async def scenario():
            child_run_id = await coordinator.spawn(child_spec())
            return await coordinator.query(child_run_id)

        outcome = asyncio.run(scenario())
        self.assertEqual("failed", outcome.status.value)
        self.assertTrue(
            any(d.code == "provider_unavailable" for d in outcome.diagnostics)
        )
        self.assertEqual(1, len(coordinator.children))

    def test_child_prompt_lands_as_user_entry_in_real_conversation(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            return await coordinator.spawn(child_spec())

        child_run_id = asyncio.run(scenario())
        run = self.repository.get_run(child_run_id)
        entries = self.repository.list_entries(run.lane_id)
        self.assertEqual(TranscriptEntryType.USER_MESSAGE, entries[0].type)
        self.assertEqual(Actor.USER, entries[0].actor)
        self.assertIn("调查并总结", entries[0].payload["content"])
        self.assertEqual(TranscriptEntryType.ASSISTANT_MESSAGE, entries[-1].type)

    def test_spawn_with_duplicate_parent_tool_call_rejected(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("一"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            await coordinator.spawn(child_spec(tool_call_id="call_1"))
            with self.assertRaises(Exception) as caught:
                await coordinator.spawn(child_spec(tool_call_id="call_1"))
            return caught.exception

        error = asyncio.run(scenario())
        self.assertEqual("delegation_duplicate_spawn", error.code)
        self.assertEqual(1, len(coordinator.tracked_child_run_ids))

    def test_two_distinct_children_run_on_real_executor(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("第一份"),
                    ProviderCompleted(finish_reason="stop"),
                ),
                (
                    ProviderTextDelta("第二份"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            first = await coordinator.spawn(child_spec(tool_call_id="call_a"))
            second = await coordinator.spawn(child_spec(tool_call_id="call_b"))
            return first, second

        first, second = asyncio.run(scenario())
        self.assertNotEqual(first, second)
        self.assertEqual(2, self.executors_created)
        self.assertEqual(2, len(coordinator.children))
        from endless_task.runtime_v2 import RunStatus

        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(first).status)
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(second).status)


if __name__ == "__main__":
    unittest.main()
