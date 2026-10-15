"""切片 10 T3 Step 2 洞 2 / 验收 ③：提醒撞"会话已有活跃运行"必须如实、不再被吞。

今天不成立的原因（起点 d2a5376）：`tasks/worker.py:_execute_task_turn` 用宽
`except Exception` 把 `ConflictError("Conversation already has an active v2 run")`
吞成 `FAILED` + 泛化文案 `"Turn submission failed."`；提醒侧没有任何东西会重试它
（重试循环只遍历 tasks），提醒又已经 `fired` → 用户看到的是一句不明所以的失败，
实际效果是"这条提醒没发生"。

本模块钉住：
- 提交冲突**按异常类型分派**：文案如实（未执行 + 原因 + 会不会重跑），不再是泛化句；
- 提醒侧：真的没有执行（provider 没被调用）、提醒保持 `fired`、通知里能读到完整解释；
- 任务侧：仍是 `retryable`，**既有**重试语义（`TaskScheduler._retry_attempt`）照旧生效。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.domain.models import (
    ReminderStatus,
    TaskRunStatus,
    TaskRunTrigger,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import AgentRunExecutor, StaticToolApprovalGate, ToolApprovalDecision
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteNotificationRepository,
    SqliteReminderRepository,
    SqliteRuntimeV2Repository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks import TaskNotificationService
from endless_task.tasks.scheduler import TaskScheduler
from endless_task.tasks.worker import (
    REMINDER_CONFLICT_ERROR,
    TURN_CONFLICT_ERROR,
    TaskWorker,
)
from endless_task.tooling import ToolRegistry

COMMITMENT = "每周一 09:00 总结上周的项目进展"
ANSWER = "收到执行请求。本周进展如下：提案与确认链路已完成，执行台账已就位。"
GENERIC_FAILURE = "Turn submission failed."


class ScriptedProvider:
    name = "task-conflict"

    def __init__(
        self, responses: Sequence[Sequence[ProviderStreamEvent]] = ()
    ) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("冲突场景不该真的调到 provider")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class TaskConflictVisibilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "conflict.db"
        )
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.task_repository = SqliteTaskRepository(self.database)
        self.run_repository = SqliteTaskRunRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.reminder_repository = SqliteReminderRepository(self.database)
        self.notification_repository = SqliteNotificationRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

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
            notification_service=TaskNotificationService(
                notification_repository=self.notification_repository
            ),
        )

    def _occupy(self, conversation_id: str) -> str:
        """在该会话里放一条**活跃** v2 run（模拟用户正在这个会话里跑一轮）。"""
        submission = self.runtime_v2_repository.create_message_submission(
            conversation_id=conversation_id,
            lane_id=None,
            content="我正在进行的一轮",
            client_request_id="busy-submission",
        )
        self.runtime_v2_repository.start_run(submission.run.id)
        return submission.run.id

    def _notifications(self):
        return self.notification_repository.list_notifications()

    # ---- 提醒侧：未执行 + 原因 + 不会重跑，且真的没执行 ---------------------
    async def test_reminder_blocked_by_active_run_is_visible_and_honest(self) -> None:
        conversation = self.chat_repository.create_conversation()
        # 批次 B Task 1：提醒的到点执行**不再硬编码回源会话**，它落进该提醒自己的
        # 执行会话（`_execute_reminder` 改用 `run.conversation_id`）。所以"占住源会话"
        # 再也造不出冲突——被占住的必须是**落点**会话。这里按新的查/建口径预建那条
        # 执行会话（与 `worker._ensure_branch(owner_kind='reminder_execution',
        # owner_id=reminder.id)` 同一个键），再占住它。
        reminder = self.reminder_repository.create_reminder(
            title="整理文档",
            commitment="整理这份文档",
            due_at="2099-01-01T09:00:00",
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
            source_proposal_id=None,
        )
        landing = self.chat_repository.create_internal_conversation(
            owner_kind="reminder_execution",
            owner_id=reminder.id,
            title="《整理文档》· 执行记录",
        )
        busy_run_id = self._occupy(landing.id)
        provider = ScriptedProvider()
        worker = self._make_worker(provider)

        run = await worker.start_reminder(reminder.id)
        await worker.drain()

        finished = self.run_repository.get_run(run.id)
        # 落点是那条执行会话（不是源会话）——这正是本批的修复
        self.assertEqual(landing.id, finished.conversation_id)
        self.assertEqual(TaskRunStatus.FAILED, finished.status)
        # 不再落泛化文案：说清"未执行 + 原因 + 会不会重跑"
        self.assertNotIn(GENERIC_FAILURE, finished.error or "")
        self.assertEqual(REMINDER_CONFLICT_ERROR, finished.error)
        self.assertIn("未执行", finished.error)
        self.assertIn("不会自动重跑", finished.error)
        # 切片 10 终审修复 W-C：`retryable` 必须跟同一条通知的口径一致——提醒没有重跑
        # 入口（R6），落 True 就是自相矛盾。任务侧仍照既有重试语义（见下一条用例）。
        self.assertFalse(
            finished.retryable,
            "提醒不会自动重跑，run 行就不许说 retryable=True",
        )
        # 真的没有执行：provider 一次都没被调用，源会话里也没有任何一轮
        self.assertEqual([], provider.requests)
        self.assertEqual(
            (),
            self.runtime_v2_repository.list_runs(conversation_id=conversation.id),
        )
        # 落点会话里只有"用户占住"的那一条在飞 run，提醒自己没有提交任何新轮次
        self.assertEqual(
            [busy_run_id],
            [
                item.id
                for item in self.runtime_v2_repository.list_runs(
                    conversation_id=landing.id
                )
            ],
        )
        # 提醒到时即 mark_fired（既有语义）——正是"没人会重试它"的根因
        self.assertEqual(
            ReminderStatus.FIRED,
            self.reminder_repository.get_reminder(reminder.id).status,
        )
        # 如实留下可见痕迹（收件箱/通知）
        notes = self._notifications()
        self.assertEqual(1, len(notes))
        self.assertEqual("《提醒·整理文档》", notes[0].title)
        self.assertIn("已有正在进行的运行", notes[0].body)
        self.assertIn("不会自动重跑", notes[0].body)
        self.assertEqual(reminder.id, notes[0].task_id)
        self.assertEqual(run.id, notes[0].run_id)

    # ---- 任务侧：同样如实，但照既有重试语义仍是 retryable -------------------
    async def test_task_blocked_by_active_run_stays_honest_and_retryable(self) -> None:
        conversation = self.chat_repository.create_conversation()
        task = self.task_repository.create_task(
            title="每周项目进展总结",
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        )
        provider = ScriptedProvider(
            [[ProviderTextDelta(ANSWER), ProviderCompleted()]]
        )
        worker = self._make_worker(provider)

        # 第一次跑成功 → 得到这一任务的执行落点会话
        # （工作区绑定的真会话会开出 EPHEMERAL 执行分支；仓储级会话没有 v1 turns，
        #  `create_branch` 抛 InvalidStateError → `_ensure_branch` 返回 None → 落点
        #  就是源会话。两种形状下"占住落点会话"都能造出同一个冲突。）
        first = await worker.start(task.id, trigger=TaskRunTrigger.MANUAL)
        await worker.drain()
        first_finished = self.run_repository.get_run(first.id)
        self.assertEqual(TaskRunStatus.COMPLETED, first_finished.status)
        landing_id = first_finished.conversation_id
        self.assertTrue(landing_id)

        # 用户正好在这个执行落点里跑着一轮（面板「去处理」就是打开它）
        self._occupy(landing_id)

        second = await worker.start(task.id, trigger=TaskRunTrigger.SCHEDULED)
        await worker.drain()
        second_finished = self.run_repository.get_run(second.id)
        self.assertEqual(TaskRunStatus.FAILED, second_finished.status)
        self.assertNotIn(GENERIC_FAILURE, second_finished.error or "")
        self.assertEqual(TURN_CONFLICT_ERROR, second_finished.error)
        self.assertTrue(second_finished.retryable)

        # 既有重试语义照旧：调度器会把这次失败当成"值得重试的第 2 次尝试"
        scheduler = TaskScheduler(
            task_repository=self.task_repository,
            run_repository=self.run_repository,
            worker=worker,
            max_attempts=3,
            reminder_repository=self.reminder_repository,
        )
        finished_at = datetime.fromisoformat(
            second_finished.finished_at.replace("Z", "+00:00")
        )
        self.assertEqual(
            2,
            scheduler._retry_attempt(
                task, finished_at + timedelta(seconds=61)
            ),
        )
        self.assertIsNone(
            scheduler._retry_attempt(task, finished_at + timedelta(seconds=1))
        )
        # 失败同样如实通知（任务侧标题带 trigger）
        notes = self._notifications()
        self.assertEqual(2, len(notes))
        failed_note = next(note for note in notes if note.run_id == second.id)
        self.assertEqual("《每周项目进展总结》· 到点执行", failed_note.title)
        self.assertIn("已有正在进行的运行", failed_note.body)


if __name__ == "__main__":
    unittest.main()
