"""M4A production review: true concurrent batch over the real kernel.

Closes the parity gap noted in 06 §20.4 / §25.5: scheduler concurrency
was proven on fake kernels; the production adapter conformance proved a
single child. This E2E runs :class:`InProcessChildScheduler` over
:class:`RuntimeV2AgentKernel` on a real sqlite repository with up to 4
children in flight through one kernel/executor-builder pair.

The executor builder hands each child run its own scripted provider from
a thread-safe pool (each child gets a distinct response queue), so the
shared-provider pop race that blocked the earlier fake attempt is gone.
Assertions: all children complete with distinct persisted runs, child
conversations are delegation-tagged (product-hidden), and a failing
child does not terminate its siblings (collect-all).
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from typing import AsyncIterator, Optional, Sequence

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.delegation import (
    ChildBatchLimits,
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    InProcessChildCoordinator,
    InProcessChildScheduler,
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
    AgentRunExecutor,
    RuntimeV2AgentKernel,
    RunStatus,
    ToolExecutionLimits,
    TranscriptEntryType,
)
from endless_task.runtime_ledger import TraceContext
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolRegistry

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "memory.read",
        "session.query",
    }
)


class ProviderPool:
    """Hands each stream request a fresh response queue (per-child).

    An entry may be the ``FAILING`` sentinel: its stream raises a
    structured ProviderError so the executor records a failed child run
    (collect-all path).
    """

    name = "scripted-pool"

    FAILING = object()

    def __init__(self, queues: Sequence[object]) -> None:
        self._queues: list[object] = list(queues)
        self.name = "scripted-pool"
        self._lock = threading.Lock()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        with self._lock:
            if not self._queues:
                raise AssertionError("Provider pool exhausted")
            group = self._queues.pop(0)
        if group is ProviderPool.FAILING:
            raise ProviderError(
                "provider_unavailable",
                "模型服务不可用。",
                retryable=True,
            )
        for event in group:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


def child_spec(index: int) -> SpawnSpec:
    return SpawnSpec(
        task=f"调查模块 {index}",
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
        parent_tool_call_id=f"call_{index}",
    )


class ConcurrentRealKernelBatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "runtime.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    async def asyncTearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _coordinator(self, provider) -> InProcessChildCoordinator:
        async def conversation_factory(child_run_id: str, *, workspace_id=None):
            return self.chat_repository.create_delegation_conversation(
                child_run_id
            )

        def executor_builder() -> AgentRunExecutor:
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

        kernel = RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=conversation_factory,
            executor_builder=executor_builder,
        )
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

    @staticmethod
    def _completed_provider_queue() -> list[ProviderStreamEvent]:
        return [
            ProviderTextDelta("完成"),
            ProviderCompleted(
                finish_reason="stop",
                input_tokens=5,
                output_tokens=2,
            ),
        ]

    async def test_four_children_complete_with_distinct_persisted_runs(self) -> None:
        provider = ProviderPool(
            [self._completed_provider_queue() for _ in range(4)]
        )
        coordinator = self._coordinator(provider)
        scheduler = InProcessChildScheduler(
            coordinator,
            limits=ChildBatchLimits(
                max_concurrent_children=4,
                max_children_per_parent=8,
            ),
        )
        summary = await scheduler.run_batch(
            [child_spec(index) for index in range(4)]
        )
        self.assertEqual(4, len(summary.completed))
        self.assertEqual(4, len(coordinator.tracked_child_run_ids))
        run_ids = [outcome.child_run_id for outcome in summary.outcomes]
        self.assertEqual(len(run_ids), len(set(run_ids)))
        for run_id in run_ids:
            run = self.repository.get_run(run_id)
            self.assertEqual(RunStatus.COMPLETED, run.status)
            entries = self.repository.list_entries(run.lane_id)
            self.assertEqual(TranscriptEntryType.USER_MESSAGE, entries[0].type)
            self.assertEqual(TranscriptEntryType.ASSISTANT_MESSAGE, entries[-1].type)

    async def test_child_conversations_are_product_hidden(self) -> None:
        provider = ProviderPool(
            [self._completed_provider_queue() for _ in range(2)]
        )
        coordinator = self._coordinator(provider)
        scheduler = InProcessChildScheduler(coordinator)
        await scheduler.run_batch([child_spec(index) for index in range(2)])
        visible = {
            item.id for item in self.chat_repository.list_conversations()
        }
        for record in coordinator.children:
            run = self.repository.get_run(record.child_run_id)
            self.assertNotIn(run.conversation_id, visible)

    async def test_collect_all_keeps_siblings_on_child_failure(self) -> None:
        queue = self._completed_provider_queue()
        provider = ProviderPool(
            [queue, ProviderPool.FAILING, queue, queue]
        )
        coordinator = self._coordinator(provider)
        scheduler = InProcessChildScheduler(
            coordinator,
            limits=ChildBatchLimits(
                max_concurrent_children=4,
                max_children_per_parent=8,
            ),
        )
        summary = await scheduler.run_batch(
            [child_spec(index) for index in range(4)]
        )
        # The failing child raises ProviderError -> its run fails; the
        # siblings still complete (collect-all).
        self.assertEqual(3, len(summary.completed))
        self.assertEqual(1, len(summary.failed))
        failed = summary.failed[0]
        self.assertTrue(
            any(d.code == "provider_unavailable" for d in failed.diagnostics)
        )


if __name__ == "__main__":
    unittest.main()
