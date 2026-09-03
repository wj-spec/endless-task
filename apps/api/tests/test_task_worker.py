from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.domain.models import (
    ReminderStatus,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import (
    AgentRunExecutor,
    RunStatus,
    RuntimeV2ReplayService,
    StaticToolApprovalGate,
    ToolApprovalDecision,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteReminderRepository,
    SqliteRuntimeV2Repository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks.worker import TaskWorker
from endless_task.tooling import ToolRegistry

COMMITMENT = "每周一 09:00 总结上周的项目进展"
RUN_ANSWER = (
    "收到执行请求。本周项目进展如下：完成了任务提案与确认链路，"
    "执行台账也已就位，整体进度符合预期。"
)


class ScriptedProvider:
    name = "scripted-task-worker"

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


class FailingProvider(ScriptedProvider):
    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        cancellation_token.raise_if_cancelled()
        raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        # Unreachable yield keeps this an async generator so the executor can
        # iterate it and surface the ProviderError as a failed run.
        yield  # pragma: no cover


class BlockingProvider(ScriptedProvider):
    """Holds the first run in a running state via an event gate."""

    def __init__(
        self,
        responses: Sequence[Sequence[ProviderStreamEvent]],
        event: asyncio.Event,
    ) -> None:
        super().__init__(responses)
        self.event = event

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        if not self.requests:
            await self.event.wait()
        async for item in super().stream(request, cancellation_token):
            yield item


class TaskWorkerV2Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "worker.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.task_repository = SqliteTaskRepository(self.database)
        self.run_repository = SqliteTaskRunRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_conversation_and_task(self):
        conversation = self.chat_repository.create_conversation()
        task = self.task_repository.create_task(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        )
        return conversation, task

    def _make_worker(self, provider, *, reminder_repository=None):
        executor = AgentRunExecutor(
            repository=self.runtime_v2_repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )
        return TaskWorker(
            task_repository=self.task_repository,
            run_repository=self.run_repository,
            runtime_v2_repository=self.runtime_v2_repository,
            task_executor=executor,
            chat_repository=self.chat_repository,
            reminder_repository=reminder_repository,
        )

    async def test_manual_run_completes_and_surfaces_content(self) -> None:
        conversation, task = self._seed_conversation_and_task()
        provider = ScriptedProvider([[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]])
        worker = self._make_worker(provider)

        run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        self.assertEqual(TaskRunStatus.RUNNING, run.status)

        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.COMPLETED, finished.status)
        # turnId is now the v2 run id (bound via link_turn).
        self.assertIsNotNone(finished.turn_id)
        self.assertIsNotNone(finished.finished_at)

        linked_run = self.runtime_v2_repository.get_run(finished.turn_id)
        self.assertEqual(RunStatus.COMPLETED, linked_run.status)

        replay = RuntimeV2ReplayService(self.runtime_v2_repository).replay_run(
            finished.turn_id
        )
        self.assertEqual(RUN_ANSWER, replay.partial_content)

    async def test_failed_turn_records_failed_run(self) -> None:
        conversation, task = self._seed_conversation_and_task()
        provider = FailingProvider([[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]])
        worker = self._make_worker(provider)

        run = await worker.start(task.id, trigger=TaskRunTrigger.SCHEDULED)
        self.assertEqual(TaskRunStatus.RUNNING, run.status)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.FAILED, finished.status)
        self.assertTrue(finished.retryable)
        self.assertIsNotNone(finished.error)

    async def test_paused_task_is_rejected(self) -> None:
        conversation, task = self._seed_conversation_and_task()
        self.task_repository.pause_task(task.id)
        provider = ScriptedProvider([[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]])
        worker = self._make_worker(provider)

        with self.assertRaises(InvalidStateError):
            await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

    async def test_concurrent_run_conflicts(self) -> None:
        conversation, task = self._seed_conversation_and_task()
        gate = asyncio.Event()
        provider = BlockingProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]], event=gate
        )
        worker = self._make_worker(provider)

        first = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        self.assertEqual(TaskRunStatus.RUNNING, first.status)

        with self.assertRaises(InvalidStateError):
            await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

        gate.set()
        await worker.drain()
        finished = self.run_repository.get_run(first.id)
        self.assertEqual(TaskRunStatus.COMPLETED, finished.status)

    async def test_concurrent_claim_allows_only_one_running_run(self) -> None:
        conversation, task = self._seed_conversation_and_task()

        def claim(index: int):
            repository = SqliteTaskRunRepository(
                Database(self.database.path),
                id_factory=lambda prefix: f"{prefix}_{index}",
            )
            return repository.claim_run(
                task_id=task.id,
                trigger=TaskRunTrigger.MANUAL,
                conversation_id=conversation.id,
            )

        results = await asyncio.gather(
            asyncio.to_thread(claim, 1),
            asyncio.to_thread(claim, 2),
            return_exceptions=True,
        )

        successful = [item for item in results if not isinstance(item, Exception)]
        conflicts = [item for item in results if isinstance(item, InvalidStateError)]
        self.assertEqual(1, len(successful))
        self.assertEqual(1, len(conflicts))
        self.assertEqual(
            1,
            len(
                [
                    run
                    for run in self.run_repository.list_runs(task_id=task.id)
                    if run.status is TaskRunStatus.RUNNING
                ]
            ),
        )

    async def test_reminder_run_completes(self) -> None:
        conversation, task = self._seed_conversation_and_task()
        reminder_repository = SqliteReminderRepository(self.database)
        reminder = reminder_repository.create_reminder(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            due_at="2099-01-01T09:00:00",
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
            source_proposal_id=None,
        )
        provider = ScriptedProvider([[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]])
        worker = self._make_worker(provider, reminder_repository=reminder_repository)

        run = await worker.start_reminder(reminder.id)
        self.assertEqual(TaskRunStatus.RUNNING, run.status)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.COMPLETED, finished.status)
        self.assertIsNotNone(finished.turn_id)
        self.assertEqual(
            ReminderStatus.FIRED,
            reminder_repository.get_reminder(reminder.id).status,
        )


if __name__ == "__main__":
    unittest.main()
