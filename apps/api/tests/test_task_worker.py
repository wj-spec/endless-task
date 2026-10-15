from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.api.serialization import conversation_json
from endless_task.domain.models import (
    ReminderStatus,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.domain.repositories import InvalidStateError, NotFoundError
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
    SqliteWorkspaceRepository,
)
from endless_task.storage.sqlite_provider_profile_repository import (
    ProviderProfileDraft,
    SqliteProviderProfileRepository,
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


class BookkeepingFailureTest(unittest.IsolatedAsyncioTestCase):
    """切片 10 终审修复 W-G：登记句柄 → spawn 之间的异常不许漏句柄 / 停 running。

    现状：`start` / `start_reminder` 先 `claim_run`（task_run 落 running）再
    `_register_cancellation`，然后才做记账（建执行分支、`link_conversation`、
    `mark_fired`）与 spawn。中间任何一处抛错都会留下**两个**坏状态：

    - `_cancel_tokens` 里的取消句柄永不释放（`cancel()` 对一个永远不会开始的 run
      说"已受理"，`in_flight()` 永远为真）；
    - `task_runs` 永远停在 `running`（占着同一 task 的单飞名额，用户看到"永远在跑"）。
    """

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "bookkeeping.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.task_repository = SqliteTaskRepository(self.database)
        self.run_repository = SqliteTaskRunRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.reminder_repository = SqliteReminderRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_conversation_and_task(self):
        conversation = self.chat_repository.create_conversation()
        # 源会话里先有一条 v1 turn：`_ensure_branch` 才会真的开出执行分支，
        # `link_conversation` 才会被走到（否则它根本不被调用，测不到这条路径）。
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="seed-turn",
            content="你好",
        )
        task = self.task_repository.create_task(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        )
        return conversation, task

    def _make_worker(self, provider) -> TaskWorker:
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
            reminder_repository=self.reminder_repository,
        )

    async def test_start_failure_between_handle_and_spawn_finalizes_the_run(self) -> None:
        """`start`：`link_conversation` 抛错 → 句柄回收 + task_run 落终态。"""
        conversation, task = self._seed_conversation_and_task()
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        def explode(run_id: str, conversation_id: str):
            raise RuntimeError("link_conversation exploded")

        self.run_repository.link_conversation = explode  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

        runs = self.run_repository.list_runs(task_id=task.id)
        self.assertEqual(1, len(runs))
        finishing = runs[0]
        self.assertEqual(TaskRunStatus.FAILED, finishing.status)
        self.assertEqual("这次执行没有开始：准备执行落点时出错。", finishing.error)
        self.assertFalse(worker.in_flight(finishing.id), "取消句柄必须已释放")
        self.assertFalse(worker.cancel(finishing.id), "没有在飞句柄时必须如实返回 False")
        # 没有真的跑：provider 一次都没被调用
        self.assertEqual([], provider.requests)
        # 也没有留下任何 v2 run 行（spawn 从未发生）
        self.assertEqual((), self.runtime_v2_repository.list_runs())

    async def test_start_reminder_failure_between_handle_and_spawn_finalizes_the_run(
        self,
    ) -> None:
        """`start_reminder`：`mark_fired` 抛错 → 句柄回收 + task_run 落终态。"""
        conversation, task = self._seed_conversation_and_task()
        reminder = self.reminder_repository.create_reminder(
            title="整理文档",
            commitment="整理这份文档",
            due_at="2099-01-01T09:00:00",
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
            source_proposal_id=None,
        )
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        def explode(reminder_id: str):
            raise RuntimeError("mark_fired exploded")

        self.reminder_repository.mark_fired = explode  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await worker.start_reminder(reminder.id)

        runs = self.run_repository.list_runs(task_id=reminder.id)
        self.assertEqual(1, len(runs))
        finishing = runs[0]
        self.assertEqual(TaskRunStatus.FAILED, finishing.status)
        self.assertEqual("这次提醒没有开始：准备执行落点时出错。", finishing.error)
        self.assertFalse(worker.in_flight(finishing.id), "取消句柄必须已释放")
        self.assertFalse(worker.cancel(finishing.id))
        self.assertEqual([], provider.requests)
        self.assertEqual((), self.runtime_v2_repository.list_runs())
        # 提醒仍是 pending（mark_fired 没成功），调度器下次还能选中它——
        # 这是既有语义，本修复不动它
        self.assertEqual(
            ReminderStatus.PENDING,
            self.reminder_repository.get_reminder(reminder.id).status,
        )


class RetryableCaliberTest(unittest.IsolatedAsyncioTestCase):
    """切片 10 收尾：提醒的 `retryable` 口径必须处处一致（无重跑入口即不为真）。

    W-C 只改了"提醒撞活跃运行"的分派分支，而同一波新加的
    `_finalize_bookkeeping_failure` 硬编码 `retryable=True`、提醒的泛型失败分支仍
    透传 `outcome.retryable`（submit/exec 失败给的是 True）→ 提醒的 run 行又落
    `retryable=True`。这个字段**下发给客户端**，而提醒**没有重跑入口**（R6，
    `mark_fired` 之后调度器再也不会选中它），所以这是同一处自相矛盾。

    两条用例各锁一条路径，且都**同时**断言任务侧保持 `True`（不许误伤既有重试语义）。
    """

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "retryable-caliber.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.task_repository = SqliteTaskRepository(self.database)
        self.run_repository = SqliteTaskRunRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.reminder_repository = SqliteReminderRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_conversation_and_task(self):
        conversation = self.chat_repository.create_conversation()
        # 源会话里先有一条 v1 turn，`link_conversation` 才会被走到。
        self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="seed-turn",
            content="你好",
        )
        task = self.task_repository.create_task(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        )
        return conversation, task

    def _make_worker(self, provider) -> TaskWorker:
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
            reminder_repository=self.reminder_repository,
        )

    def _create_reminder(self, conversation_id: str):
        return self.reminder_repository.create_reminder(
            title="整理文档",
            commitment="整理这份文档",
            due_at="2099-01-01T09:00:00",
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
            source_proposal_id=None,
        )

    async def test_bookkeeping_failure_retryable_is_per_subject(self) -> None:
        """`_finalize_bookkeeping_failure`：提醒落 False，任务仍 True（防误伤）。"""
        conversation, task = self._seed_conversation_and_task()
        reminder = self._create_reminder(conversation.id)
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        # ---- 提醒侧：记账步骤（mark_fired）抛错 -----------------------------
        def explode_mark_fired(reminder_id: str):
            raise RuntimeError("mark_fired exploded")

        self.reminder_repository.mark_fired = explode_mark_fired  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await worker.start_reminder(reminder.id)

        reminder_run = self.run_repository.list_runs(task_id=reminder.id)[0]
        self.assertEqual(TaskRunStatus.FAILED, reminder_run.status)
        self.assertFalse(
            reminder_run.retryable,
            "提醒没有重跑入口（R6），记账失败也不许说 retryable=True",
        )

        # ---- 任务侧：同一函数、同一路径，重试语义照旧 -----------------------
        def explode_link(run_id: str, conversation_id: str):
            raise RuntimeError("link_conversation exploded")

        self.run_repository.link_conversation = explode_link  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

        task_run = self.run_repository.list_runs(task_id=task.id)[0]
        self.assertEqual(TaskRunStatus.FAILED, task_run.status)
        self.assertTrue(
            task_run.retryable,
            "任务侧必须保持既有的可重试语义（TaskScheduler._retry_attempt）",
        )
        self.assertEqual([], provider.requests, "两条路径都没有真的跑")

    async def test_execute_reminder_generic_failure_is_not_retryable(self) -> None:
        """`_execute_reminder` 泛型失败分支：提醒落 False，任务仍 True（防误伤）。"""
        conversation, task = self._seed_conversation_and_task()
        reminder = self._create_reminder(conversation.id)
        # `FailingProvider` 让这一轮真的失败；`_map_status` 给的 outcome 是
        # `FAILED, retryable=True` → 走 `_execute_reminder` 的泛型失败分支。
        worker = self._make_worker(FailingProvider([]))

        run = await worker.start_reminder(reminder.id)
        await worker.drain()

        reminder_run = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.FAILED, reminder_run.status)
        self.assertFalse(
            reminder_run.retryable,
            "提醒没有重跑入口（R6），泛型失败也不许说 retryable=True",
        )
        self.assertEqual(
            ReminderStatus.FIRED,
            self.reminder_repository.get_reminder(reminder.id).status,
            "提醒到时即 fired（既有语义）——正是没人会重跑它的根因",
        )

        # ---- 任务侧：同一条泛型失败分支，重试语义照旧 -----------------------
        task_worker = self._make_worker(FailingProvider([]))
        task_run = await task_worker.start(task.id, trigger=TaskRunTrigger.SCHEDULED)
        await task_worker.drain()

        finished_task = self.run_repository.get_run(task_run.id)
        self.assertEqual(TaskRunStatus.FAILED, finished_task.status)
        self.assertTrue(
            finished_task.retryable,
            "任务侧必须保持既有的可重试语义（TaskScheduler._retry_attempt）",
        )


class ExecutionConversationTest(unittest.IsolatedAsyncioTestCase):
    """批次 B Task 1（ADR-1 C 形状）：到点执行落进**自己的执行会话**。

    修复前：`_ensure_branch` 调 v1 `create_branch`，而生产代码里 v1 `turns` 零写入者
    → 必抛 `InvalidStateError` → 被无日志吞成 `None` → `【到点执行】…` 落进**用户的
    日常会话**；提醒路还额外把目标硬编码回源会话。

    这里的每条用例都写明"怎么红"：把实现改回旧形态（`create_branch` / 无标记列 /
    硬编码源会话 / 无日志 `return None`）就会红在相应的那条断言上。
    """

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "execution-conversation.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.task_repository = SqliteTaskRepository(self.database)
        self.run_repository = SqliteTaskRunRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.reminder_repository = SqliteReminderRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_source(self):
        conversation = self.chat_repository.create_conversation()
        task = self.task_repository.create_task(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        )
        return conversation, task

    def _seed_reminder(self, conversation_id: str):
        return self.reminder_repository.create_reminder(
            title="整理文档",
            commitment="整理这份文档",
            due_at="2099-01-01T09:00:00",
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
            source_proposal_id=None,
        )

    def _make_worker(self, provider) -> TaskWorker:
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
            reminder_repository=self.reminder_repository,
        )

    def _conversation_columns(self, conversation_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
        self.assertIsNotNone(row, f"会话不存在：{conversation_id}")
        return dict(row)

    def _user_submissions(self, conversation_id: str) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM v2_transcript_entries
                WHERE conversation_id = ? AND actor = 'user'
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return [row["payload_json"] for row in rows]

    def _conversations_owned_by(self, owner_id: str) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations WHERE internal_owner_id = ?",
                (owner_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- ① 到点执行落在独立会话（今天会红：落在源会话） -------------------
    async def test_scheduled_execution_lands_in_its_own_conversation(self) -> None:
        source, task = self._seed_source()
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        run = await worker.start(task.id, trigger=TaskRunTrigger.SCHEDULED)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.COMPLETED, finished.status)
        # 落点**不是**源会话（旧形态必然相等，因为 branch 被吞成 None）。
        self.assertNotEqual(source.id, finished.conversation_id)

        landing = self.chat_repository.get_conversation(finished.conversation_id)
        self.assertEqual("normal", landing.kind.value)
        self.assertIsNone(landing.parent_conversation_id)
        self.assertIsNone(landing.fork_turn_id)
        self.assertEqual(f"《{task.title}》· 执行记录", landing.title)

        marker = self._conversation_columns(finished.conversation_id)
        self.assertEqual("task_execution", marker["internal_owner_kind"])
        self.assertEqual(task.id, marker["internal_owner_id"])

        # 执行内容落进执行会话，源会话里**没有**这条到点执行
        landing_submissions = self._user_submissions(finished.conversation_id)
        self.assertTrue(
            any("【到点执行】" in item for item in landing_submissions),
            landing_submissions,
        )
        source_submissions = self._user_submissions(source.id)
        self.assertFalse(
            any("【到点执行】" in item for item in source_submissions),
            source_submissions,
        )

    # ---- ② 复用：一个任务一份执行记录（今天会红：每次到点新建） -----------
    async def test_second_run_reuses_the_same_execution_conversation(self) -> None:
        source, task = self._seed_source()
        provider = ScriptedProvider(
            [
                [ProviderTextDelta(RUN_ANSWER), ProviderCompleted()],
                [ProviderTextDelta(RUN_ANSWER), ProviderCompleted()],
            ]
        )
        worker = self._make_worker(provider)

        first = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        await worker.drain()
        second = await worker.start(task.id, trigger=TaskRunTrigger.SCHEDULED)
        await worker.drain()

        first_finished = self.run_repository.get_run(first.id)
        second_finished = self.run_repository.get_run(second.id)
        self.assertEqual(TaskRunStatus.COMPLETED, first_finished.status)
        self.assertEqual(TaskRunStatus.COMPLETED, second_finished.status)
        self.assertNotEqual(source.id, first_finished.conversation_id)
        self.assertEqual(
            first_finished.conversation_id,
            second_finished.conversation_id,
            "同一任务的两次到点执行必须复用同一份执行记录",
        )
        rows = self._conversations_owned_by(task.id)
        self.assertEqual(1, len(rows), rows)
        self.assertEqual(
            [{"kind": "task_execution", "id": task.id}],
            [
                {
                    "kind": row["internal_owner_kind"],
                    "id": row["internal_owner_id"],
                }
                for row in rows
            ],
        )

    # ---- ③ 提醒路：落点会话改用 run.conversation_id（今天会红：硬编码源会话） ----
    async def test_reminder_turn_lands_in_its_own_execution_conversation(self) -> None:
        source, _ = self._seed_source()
        reminder = self._seed_reminder(source.id)
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        run = await worker.start_reminder(reminder.id)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.COMPLETED, finished.status)
        self.assertNotEqual(source.id, finished.conversation_id)
        marker = self._conversation_columns(finished.conversation_id)
        self.assertEqual("reminder_execution", marker["internal_owner_kind"])
        self.assertEqual(reminder.id, marker["internal_owner_id"])
        # `_execute_reminder` 真的把提醒那一轮提交到执行会话（硬编码源会话时，
        # 下面这两条断言一条都不会成立）。
        landing_submissions = self._user_submissions(finished.conversation_id)
        self.assertTrue(
            any("【提醒】" in item for item in landing_submissions),
            landing_submissions,
        )
        self.assertEqual([], self._user_submissions(source.id))
        self.assertEqual(
            ReminderStatus.FIRED,
            self.reminder_repository.get_reminder(reminder.id).status,
        )

    # ---- ④ 失败显式：源会话被删（文案不变 + WARNING 留痕） ----------------
    async def test_source_conversation_deleted_is_explicit_and_keeps_wording(
        self,
    ) -> None:
        source, task = self._seed_source()
        provider = ScriptedProvider([])
        worker = self._make_worker(provider)
        self.chat_repository.delete_conversation(source.id)

        with self.assertLogs("endless_task.tasks.worker", level="WARNING") as captured:
            run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

        self.assertEqual(TaskRunStatus.FAILED, run.status)
        self.assertEqual("来源会话已删除，无法执行该安排。", run.error)
        self.assertFalse(run.retryable)
        self.assertIn("source conversation is gone", "\n".join(captured.output))
        self.assertEqual([], provider.requests)

    # ---- ④b 失败显式：建会话失败**不降级回源会话**（ADR-1 §S1 裁定） -------
    async def test_build_failure_aborts_instead_of_writing_into_the_source(
        self,
    ) -> None:
        """准备不出执行会话 → run 落 FAILED，**源会话里一条都不许有**。

        旧形态（简报原文的"降级"）在这里是 `except → logger.warning → return None`，
        `start` 因此跳过 `link_conversation`，run 的落点停在 `source_conversation_id`
        → `【手动执行】…` 被静默塞进用户的日常会话。ADR-1 §S1 裁定：**任何准备失败都
        不许降级**，收尾为可见的 FAILED（`retryable=True`）+ 如实原因。

        红检查：把 `except ExecutionConversationUnavailable` 的分支删掉（退回"降级"），
        下面 ①③（FAILED / retryable）与"源会话零 entry"三条都会红。
        """
        source, task = self._seed_source()
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        def explode(**kwargs):
            raise InvalidStateError("stub: cannot create execution conversation")

        self.chat_repository.create_internal_conversation = explode  # type: ignore[method-assign]

        with self.assertLogs("endless_task.tasks.worker", level="WARNING") as captured:
            run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)

        # ① 可见的失败 + 可重试（下次到点会重新尝试准备执行会话）
        self.assertEqual(TaskRunStatus.FAILED, run.status)
        self.assertTrue(run.retryable)
        # ③ 如实原因（不是泛化文案，也不假装成功）
        self.assertIn("未能准备执行会话", run.error or "")
        self.assertIn("stub: cannot create execution conversation", run.error or "")
        self.assertIn("可稍后重试", run.error or "")
        # ② 源会话里**没有**这条执行（降级形态会在这里红）
        self.assertEqual([], self._user_submissions(source.id))
        # **没有**真的跑：provider 一次都没被调用、也没有 v2 run 行
        self.assertEqual([], provider.requests)
        self.assertEqual((), self.runtime_v2_repository.list_runs())
        # 必须留痕（不是无日志静默）
        self.assertIn("could not get an execution conversation", "\n".join(captured.output))

    # ---- ④c 提醒侧的同一失败路径也必须显式（不许 mark_fired 就当没发生） ----
    async def test_reminder_build_failure_aborts_and_stays_visible(self) -> None:
        source, _ = self._seed_source()
        reminder = self._seed_reminder(source.id)
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        def explode(**kwargs):
            raise InvalidStateError("stub: cannot create execution conversation")

        self.chat_repository.create_internal_conversation = explode  # type: ignore[method-assign]

        with self.assertLogs("endless_task.tasks.worker", level="WARNING") as captured:
            run = await worker.start_reminder(reminder.id)

        self.assertEqual(TaskRunStatus.FAILED, run.status)
        self.assertIn("未能准备执行会话", run.error or "")
        self.assertIn("stub: cannot create execution conversation", run.error or "")
        # 提醒侧没有重跑入口（R6），与既有失败分支同口径：不落 retryable=True
        self.assertFalse(run.retryable)
        # 提醒到时即 fired（既有语义），但 run 是**可见的失败**，不是"当没发生"
        self.assertEqual(
            ReminderStatus.FIRED,
            self.reminder_repository.get_reminder(reminder.id).status,
        )
        # ② 源会话里没有那条 `【提醒】`（降级形态会在这里红）
        self.assertEqual([], self._user_submissions(source.id))
        self.assertEqual([], provider.requests)
        self.assertIn("could not get an execution conversation", "\n".join(captured.output))

    # ---- ④d §S2：两处"取不到会话"的文案必须能区分 --------------------------
    async def test_source_conversation_gone_keeps_its_own_wording(self) -> None:
        """`_ensure_branch` 取不到**源**会话 → 既有文案与语义**逐字不变**。"""
        source, task = self._seed_source()
        worker = self._make_worker(ScriptedProvider([]))
        self.chat_repository.delete_conversation(source.id)
        with self.assertLogs("endless_task.tasks.worker", level="WARNING") as captured:
            run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        self.assertEqual("来源会话已删除，无法执行该安排。", run.error)
        self.assertIn("source conversation is gone", "\n".join(captured.output))

    async def test_reminder_landing_gone_is_reported_as_landing_not_source(
        self,
    ) -> None:
        """复审 I-2 修：**真驱动 `_execute_reminder`**，锁住它的 §S2 文案接线。

        上一版这条用例把 `target_gone_message="提醒的执行会话已删除…"` **当参数传进**
        `_execute_task_turn` 再断言回显——它测的是"参数回显"，不是 `_execute_reminder`
        的接线。评审 M5 实测：把 `_execute_reminder` 里的文案改回旧的假归因
        「来源会话已删除，无法执行该提醒。」，**31 条相关用例全绿、一条都没红**。

        这里改成真路径：按本批真正的口径建一条执行会话、把一个**仍在 running** 的 run
        指过去（`_finish` 只认 running——已经终态的那一轮会在这里抛
        InvalidStateError，所以不能用 `start_reminder` 跑完的那一轮），删掉它，再真驱动
        `_execute_reminder(run_id, reminder)`。整条链（取 run、`create_message_submission`、
        NotFoundError 分派、`_finish`）全是真的，**没有对 worker 打桩**；提醒仍是
        PENDING，由它自己 `mark_fired`。

        红检查：把 `_execute_reminder` 里的 `target_gone_message` 改回
        「来源会话已删除，无法执行该提醒。」→ 本用例必须红（M5 的复现）。
        """
        source, _ = self._seed_source()
        reminder = self._seed_reminder(source.id)
        worker = self._make_worker(ScriptedProvider([]))

        # 一个仍在 running 的提醒 run，落点按真口径建好后立刻删掉。
        run = self.run_repository.claim_run(
            task_id=reminder.id,
            trigger=TaskRunTrigger.SCHEDULED,
            conversation_id=reminder.source_conversation_id,
        )
        landing = self.chat_repository.create_internal_conversation(
            owner_kind="reminder_execution",
            owner_id=reminder.id,
            title=f"《{reminder.title}》· 执行记录",
        )
        run = self.run_repository.link_conversation(run.id, landing.id)
        self.assertNotEqual(source.id, run.conversation_id)
        self.chat_repository.delete_conversation(landing.id)

        with self.assertLogs("endless_task.tasks.worker", level="WARNING") as captured:
            finished = await worker._execute_reminder(run.id, reminder)

        self.assertEqual(TaskRunStatus.FAILED, finished.status)
        self.assertEqual(
            "提醒的执行会话已删除，无法执行该提醒。", finished.error
        )
        self.assertNotEqual("来源会话已删除，无法执行该提醒。", finished.error)
        self.assertNotIn("来源会话", finished.error or "")
        self.assertIn("landing conversation is gone", "\n".join(captured.output))
        # 提醒没有重跑入口（R6）：这不许被说成 retryable
        self.assertFalse(finished.retryable)
        # 既有 `source_gone` 语义照旧：落点没了也把提醒标成已触发（不卡在 pending）
        self.assertEqual(
            ReminderStatus.FIRED,
            self.reminder_repository.get_reminder(reminder.id).status,
        )

    def _cancel_cascade(self, conversation_id: str) -> None:
        """复刻 `DELETE /conversations/{id}` 的级联序列（含子会话展开）。

        真路径是 `api/routes/conversations.py:258-272`：先 `list_descendant_ids`，
        再对 `(自己, *子会话)` 逐个跑 `cancel_reminders_for_conversation` /
        `cancel_tasks_for_conversation`。这里不复制**判据**（那两句就是被测对象），
        只复刻调用形状，好让不变量用例走真级联而不是直接点名两句仓储调用。
        """
        for target_id in (
            conversation_id,
            *self.chat_repository.list_descendant_ids(conversation_id),
        ):
            self.reminder_repository.cancel_reminders_for_conversation(target_id)
            self.task_repository.cancel_tasks_for_conversation(target_id)

    # ---- ⑤ 不变量：删除执行会话**不会**取消该任务或提醒（§S3） ------------
    async def test_deleting_the_execution_conversation_does_not_cancel_ownership(
        self,
    ) -> None:
        """两句级联（任务/提醒）判据是 `source_conversation_id`，不是执行会话。

        这是**成立的不变量**（ADR-1 §5：用户可重命名/删除执行会话，任务下次到点会
        新建一份）。锁住它：以后有人把级联改成按 `task_runs.conversation_id` 找，
        就会在这里红——那会让"删掉执行记录"顺手取消掉用户的任务。

        走的是**真级联形状**（`_cancel_cascade` 复刻删除路由的整段循环，含子会话
        展开）。复审 M-8① 指出这条复刻"真路由改了拦不住"——真端点版本另见
        `tests/test_task_create_api.py` 的 `ExecutionConversationDeleteCascadeTest`
        （走 `DELETE /conversations/{id}`）。这里保留仓储级版本，因为它能在**无 HTTP
        夹具**的情况下直接钉住那两句判据。
        """
        source, task = self._seed_source()
        reminder = self._seed_reminder(source.id)
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)
        run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        await worker.drain()

        execution_id = self.run_repository.get_run(run.id).conversation_id
        self.assertNotEqual(source.id, execution_id)

        # 对照面：级联**源会话**照旧取消任务与提醒（判据是它）
        self._cancel_cascade(source.id)
        self.assertEqual("cancelled", self.task_repository.get_task(task.id).status.value)
        self.assertEqual(
            "cancelled",
            self.reminder_repository.get_reminder(reminder.id).status.value,
        )

        # 复位成 active/pending，再把**执行会话**跑一遍同一条级联：
        # 判据不是它，所以一条都不许动。
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status = 'active', cancelled_at = NULL WHERE id = ?",
                (task.id,),
            )
            connection.execute(
                "UPDATE reminders SET status = 'pending', cancelled_at = NULL "
                "WHERE id = ?",
                (reminder.id,),
            )
        self._cancel_cascade(execution_id)
        self.assertEqual(
            "active", self.task_repository.get_task(task.id).status.value
        )
        self.assertEqual(
            "pending", self.reminder_repository.get_reminder(reminder.id).status.value
        )
        # 而且真删掉执行会话本身（这正是产品里的动作）
        self.chat_repository.delete_conversation(execution_id)
        self.assertEqual(
            "active", self.task_repository.get_task(task.id).status.value
        )
        self.assertEqual(
            "pending", self.reminder_repository.get_reminder(reminder.id).status.value
        )

    # ---- 复审 I-3：`chat_repository` 缺失也**不许**静默落回源会话 ----------
    async def test_missing_chat_repository_aborts_instead_of_using_source(
        self,
    ) -> None:
        """ADR-1 §S1 是全称命题：`chat_repository is None` 也是"准备失败"。

        旧形态（`if self._chat_repository is not None:` 包住整段）在仓储缺失时跳过
        建会话与 `link_conversation`，run 的 `conversation_id` 停在
        `source_conversation_id`（claim_run 的初值）→ `【手动执行】…` 被**无日志**地
        写进用户的日常会话。生产只有 `api/wiring.py` 一处构造且传了它，所以实际不可达
        ——但按 §S1 不许留静默旁路。删掉 `if self._chat_repository is None:` 那段即红
        （run 会 COMPLETED 且源会话里出现那条 entry）。
        """
        source, task = self._seed_source()
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        executor = AgentRunExecutor(
            repository=self.runtime_v2_repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )
        worker = TaskWorker(
            task_repository=self.task_repository,
            run_repository=self.run_repository,
            runtime_v2_repository=self.runtime_v2_repository,
            task_executor=executor,
            chat_repository=None,
            reminder_repository=self.reminder_repository,
        )

        run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        self.assertEqual(TaskRunStatus.FAILED, finished.status)
        self.assertIn("未能准备执行会话", finished.error or "")
        self.assertIn("未配置会话仓储", finished.error or "")
        self.assertTrue(finished.retryable)
        # 源会话里一条都没有（旧形态会在这里红），也没真的跑
        self.assertEqual([], self._user_submissions(source.id))
        self.assertEqual([], provider.requests)
        self.assertEqual((), self.runtime_v2_repository.list_runs())

    async def test_missing_chat_repository_aborts_reminder_too(self) -> None:
        """提醒侧同形：仓储缺失也收尾为可见 FAILED（不是静默 mark_fired 后落源会话）。"""
        source, _ = self._seed_source()
        reminder = self._seed_reminder(source.id)
        provider = ScriptedProvider([])
        executor = AgentRunExecutor(
            repository=self.runtime_v2_repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )
        worker = TaskWorker(
            task_repository=self.task_repository,
            run_repository=self.run_repository,
            runtime_v2_repository=self.runtime_v2_repository,
            task_executor=executor,
            chat_repository=None,
            reminder_repository=self.reminder_repository,
        )

        run = await worker.start_reminder(reminder.id)

        self.assertEqual(TaskRunStatus.FAILED, run.status)
        self.assertIn("未能准备执行会话", run.error or "")
        self.assertIn("未配置会话仓储", run.error or "")
        self.assertFalse(run.retryable)
        self.assertEqual([], self._user_submissions(source.id))
        self.assertEqual([], provider.requests)

    # ---- 复用查询只认 active（且环境继承自源会话） -------------------------
    async def test_execution_conversation_inherits_source_environment(self) -> None:
        workspace = SqliteWorkspaceRepository(self.database).create_workspace(
            "执行会话环境继承"
        )
        profile = SqliteProviderProfileRepository(self.database).create_profile(
            ProviderProfileDraft(name="执行会话继承档", default_model="model_seed")
        )
        source = self.chat_repository.create_conversation(workspace.id)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE conversations SET provider_profile_id = ?, "
                "model_override = ? WHERE id = ?",
                (profile.id, "model_seed", source.id),
            )
        task = self.task_repository.create_task(
            title="继承环境",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=source.id,
            source_turn_id="turn_seed",
        )
        provider = ScriptedProvider(
            [[ProviderTextDelta(RUN_ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)
        run = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        landing = self.chat_repository.get_conversation(finished.conversation_id)
        self.assertEqual(workspace.id, landing.workspace_id)
        self.assertEqual(profile.id, landing.provider_profile_id)
        self.assertEqual("model_seed", landing.model_override)
        # 标记列不进产品 payload（`Conversation` dataclass 不动）
        self.assertFalse(hasattr(landing, "internal_owner_kind"))
        payload = conversation_json(landing)
        self.assertNotIn("internal_owner_kind", payload)
        self.assertNotIn("internal_owner_id", payload)

    async def test_find_internal_conversation_is_scoped_by_owner(self) -> None:
        first = self.chat_repository.create_internal_conversation(
            owner_kind="task_execution",
            owner_id="task_a",
            title="《甲》· 执行记录",
        )
        second = self.chat_repository.create_internal_conversation(
            owner_kind="reminder_execution",
            owner_id="task_a",
            title="《乙》· 执行记录",
        )
        self.assertEqual(
            first.id,
            self.chat_repository.find_internal_conversation(
                "task_execution", "task_a"
            ).id,
        )
        self.assertEqual(
            second.id,
            self.chat_repository.find_internal_conversation(
                "reminder_execution", "task_a"
            ).id,
        )
        self.assertIsNone(
            self.chat_repository.find_internal_conversation("task_execution", "task_b")
        )
        with self.assertRaises(NotFoundError):
            self.chat_repository.get_conversation("conv_does_not_exist")


if __name__ == "__main__":
    unittest.main()
