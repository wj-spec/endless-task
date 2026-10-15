"""切片 10 T3 Step 0 + Step 2 洞 1：启动清扫必须能**复合**，提醒被中断不许静默。

库里同一个"被中断的定时任务 run"同时有 **v2 run 行**（由 T2 的
`runtime_v2/run_sweep.py:sweep_interrupted_v2_runs` 管）与 **task_runs 行**（由既有
`SqliteTaskRunRepository.sweep_interrupted_runs` 管），随后 `api/app.py` 的通知循环按
task 仓储的 `swept_runs` 发通知。本模块把三件要求做成用例：

- (a) 两条清扫同时跑：不 double-finalize、不互相打架（状态与错误码各自正确、可重入）；
- (b) 用户**恰好收到一条**如实通知（不是两条、也不是零条）——提醒 run 的 `task_id`
  是 **reminder id**，以前无条件 `get_task()` → `NotFoundError` → `continue` → **零通知**；
- (c) 顺序无关：两条清扫在 lifespan 里对调先后，可观测结果不变；
- (d) T2 的 v2 清扫先落 FAILED **不会**改变既有 task 清扫/通知路径的行为
  （它只碰 `v2_runs`，不碰 `task_runs`，通知循环也不依赖 v2 run 是否活动）。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.api.app import _notify_swept_runs
from endless_task.domain.models import TaskRun, TaskRunStatus, TaskRunTrigger
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.run_sweep import (
    SWEEP_ERROR_CODE,
    sweep_interrupted_v2_runs,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteNotificationRepository,
    SqliteReminderRepository,
    SqliteRuntimeV2Repository,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks import REMINDER_NO_RETRY_NOTE
from endless_task.tasks.notification_service import TaskNotificationService

TASK_ERROR = "服务重启中断了这次执行。"


class NeverCalledProvider:
    name = "sweep-composition"

    def __init__(self) -> None:
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token: CancellationToken):
        self.requests.append(request)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("unexpected")
        yield ProviderCompleted()


def seed_interrupted_runs(database_path: Path) -> dict[str, str]:
    """造出"上一个进程被杀"的精确状态：task_runs RUNNING + 对应 v2 run RUNNING。"""
    database = Database(database_path)
    database.initialize()
    chat = SqliteChatRepository(database)
    tasks = SqliteTaskRepository(database)
    runs = SqliteTaskRunRepository(database)
    runtime = SqliteRuntimeV2Repository(database)
    reminders = SqliteReminderRepository(database)

    # 任务 run：提交进任务自己的执行分支（与 `TaskWorker.start` 同形）
    task_conversation = chat.create_conversation()
    task = tasks.create_task(
        title="每周进展",
        commitment="每周一 09:00 总结上周的项目进展",
        schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
        source_conversation_id=task_conversation.id,
        source_turn_id="turn_seed",
    )
    task_run = runs.claim_run(
        task_id=task.id,
        trigger=TaskRunTrigger.SCHEDULED,
        conversation_id=task_conversation.id,
    )
    task_submission = runtime.create_message_submission(
        conversation_id=task_conversation.id,
        lane_id=None,
        content="【到点执行】每周一 09:00 总结上周的项目进展",
        client_request_id=f"taskrun:{task_run.id}",
    )
    runtime.start_run(task_submission.run.id)
    runs.link_turn(task_run.id, task_submission.run.id)

    # 提醒 run：`task_id` 存的是 **reminder id**，turn 提交进**源会话**
    reminder_conversation = chat.create_conversation()
    reminder = reminders.create_reminder(
        title="整理文档",
        commitment="整理这份文档",
        due_at="2020-01-01T00:00:00.000Z",
        source_conversation_id=reminder_conversation.id,
        source_turn_id="turn_seed",
        source_proposal_id=None,
    )
    reminder_run = runs.claim_run(
        task_id=reminder.id,
        trigger=TaskRunTrigger.SCHEDULED,
        conversation_id=reminder_conversation.id,
    )
    reminder_submission = runtime.create_message_submission(
        conversation_id=reminder_conversation.id,
        lane_id=None,
        content="【提醒】你设置的提醒到时了，请按承诺执行：整理这份文档",
        client_request_id=f"taskrun:{reminder_run.id}",
    )
    runtime.start_run(reminder_submission.run.id)
    runs.link_turn(reminder_run.id, reminder_submission.run.id)
    # `start_reminder` 是**先 mark_fired 再 spawn**：崩溃窗口里提醒已经是 fired
    reminders.mark_fired(reminder.id)

    return {
        "task_id": task.id,
        "task_run_id": task_run.id,
        "task_v2_run_id": task_submission.run.id,
        "task_conversation_id": task_conversation.id,
        "reminder_id": reminder.id,
        "reminder_run_id": reminder_run.id,
        "reminder_v2_run_id": reminder_submission.run.id,
        "reminder_conversation_id": reminder_conversation.id,
    }


def repositories(database_path: Path):
    database = Database(database_path)
    database.initialize()
    return SimpleNamespace(
        database=database,
        chat=SqliteChatRepository(database),
        tasks=SqliteTaskRepository(database),
        runs=SqliteTaskRunRepository(database),
        runtime=SqliteRuntimeV2Repository(database),
        reminders=SqliteReminderRepository(database),
        notifications=SqliteNotificationRepository(database),
    )


@asynccontextmanager
async def app_client(database_path: Path, provider):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            # 关掉调度器：本模块只测启动清扫/通知，不要被"到点补跑"干扰
            scheduler_enabled=False,
        ),
        provider=provider,
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client, app
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class _RecordingNotifications:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify_run(self, task, run, excerpt=None, **kwargs) -> None:
        self.calls.append(("task", task.id, None))

    def notify_reminder(self, reminder, run, excerpt=None, *, note=None) -> None:
        self.calls.append(("reminder", reminder.id, note))


class SweepDispatchUnitTest(unittest.TestCase):
    """`_notify_swept_runs` 按 subject 分派（洞 1 的最小可红用例）。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "dispatch.db"
        self.ids = seed_interrupted_runs(self.database_path)
        self.repos = repositories(self.database_path)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _container(self, service) -> SimpleNamespace:
        return SimpleNamespace(
            task_notification_service=service,
            task_repository=self.repos.tasks,
            reminder_repository=self.repos.reminders,
        )

    def _swept(self):
        return self.repos.runs.sweep_interrupted_runs()

    def test_reminder_run_gets_a_reminder_notification(self) -> None:
        service = _RecordingNotifications()
        swept = [run for run in self._swept() if run.task_id == self.ids["reminder_id"]]
        self.assertEqual(1, len(swept))
        _notify_swept_runs(self._container(service), swept)
        self.assertEqual(
            [("reminder", self.ids["reminder_id"], REMINDER_NO_RETRY_NOTE)],
            service.calls,
        )
        # 切片 10 终审修复 W-F：钉**字面**口径，而不是"等于自己引用的常量"
        # （后者改文案不会红）。被中断的提醒 run 可能已经 spawn 过、有工具副作用，
        # "本次未执行"是过度断言；如实口径是"本次没有跑完"。
        note = service.calls[0][2]
        self.assertIsNotNone(note)
        self.assertIn("本次没有跑完", note)
        self.assertNotIn("本次未执行", note)
        self.assertIn("不会自动重跑", note)

    def test_task_run_still_gets_a_task_notification(self) -> None:
        service = _RecordingNotifications()
        swept = [run for run in self._swept() if run.task_id == self.ids["task_id"]]
        self.assertEqual(1, len(swept))
        _notify_swept_runs(self._container(service), swept)
        self.assertEqual([("task", self.ids["task_id"], None)], service.calls)

    def test_unknown_subject_is_skipped_without_raising(self) -> None:
        service = _RecordingNotifications()
        original = self.repos.runs.get_run(self.ids["task_run_id"])
        orphan = TaskRun(
            **{
                **{
                    field: getattr(original, field)
                    for field in (
                        "id",
                        "trigger",
                        "status",
                        "conversation_id",
                        "turn_id",
                        "error",
                        "started_at",
                        "finished_at",
                        "awaiting_user",
                        "awaiting_note",
                        "attempt",
                        "retryable",
                    )
                },
                "task_id": "task_does_not_exist",
            }
        )
        _notify_swept_runs(self._container(service), [orphan])
        self.assertEqual([], service.calls)


class SweepCompositionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "composition.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _notifications(self, client) -> list[dict]:
        response = await client.get("/notifications")
        self.assertEqual(200, response.status_code)
        return response.json()["items"]

    # ---- (a)+(b)：两条清扫复合，恰好各一条通知 -----------------------------
    async def test_startup_sweeps_compose_and_notify_once_per_subject(self) -> None:
        ids = seed_interrupted_runs(self.database_path)
        provider = NeverCalledProvider()
        async with app_client(self.database_path, provider) as (client, app):
            repos = repositories(self.database_path)
            # (a) task_runs 侧：既有清扫的语义逐字不变
            for run_id in (ids["task_run_id"], ids["reminder_run_id"]):
                record = repos.runs.get_run(run_id)
                self.assertEqual(TaskRunStatus.FAILED, record.status)
                self.assertEqual(TASK_ERROR, record.error)
                self.assertTrue(record.retryable)
                self.assertIsNotNone(record.finished_at)
            # (a) v2_runs 侧：T2 的清扫把同一批 run 落 FAILED 且留下可检索的错误码
            for run_id in (ids["task_v2_run_id"], ids["reminder_v2_run_id"]):
                record = repos.runtime.get_run(run_id)
                self.assertEqual("failed", record.status.value)
                self.assertEqual(SWEEP_ERROR_CODE, record.error_code)
            # (a) 可重入：终态行不在活动集合里，再来一次是空操作（不 double-finalize）
            self.assertEqual((), tuple(sweep_interrupted_v2_runs(repos.runtime)))
            self.assertEqual((), tuple(repos.runs.sweep_interrupted_runs()))

            # (b) 恰好一条：任务是任务通知，提醒是提醒通知（今天是零条 → 能红）
            notes = await self._notifications(client)
            self.assertEqual(2, len(notes), notes)
            titles = sorted(note["title"] for note in notes)
            self.assertEqual(
                sorted(["《每周进展》· 到点执行", "《提醒·整理文档》"]), titles
            )
            task_note = next(n for n in notes if n["title"].startswith("《每周进展》"))
            reminder_note = next(
                n for n in notes if n["title"].startswith("《提醒·")
            )
            self.assertIn(TASK_ERROR, task_note["body"])
            self.assertIn(TASK_ERROR, reminder_note["body"])
            # 提醒必须如实说清"不会自动重跑"
            self.assertIn("不会自动重跑", reminder_note["body"])
            self.assertEqual(ids["reminder_id"], reminder_note["taskId"])
            self.assertEqual(ids["task_id"], task_note["taskId"])
            # 提醒的深链仍是 run 的会话（run.conversation_id，既有语义不动）
            self.assertEqual(
                ids["reminder_conversation_id"], reminder_note["conversationId"]
            )
            # 清扫不产生任何 run（不是"又补跑一次"）
            self.assertEqual([], provider.requests)
            self.assertEqual(1, len(repos.runs.list_runs(task_id=ids["task_id"])))
            self.assertEqual(
                1, len(repos.runs.list_runs(task_id=ids["reminder_id"]))
            )

    # ---- (c) 顺序无关 -------------------------------------------------------
    def test_sweep_order_does_not_matter(self) -> None:
        forward_path = Path(self._temporary_directory.name) / "forward.db"
        reverse_path = Path(self._temporary_directory.name) / "reverse.db"
        forward_ids = seed_interrupted_runs(forward_path)
        reverse_ids = seed_interrupted_runs(reverse_path)

        forward = repositories(forward_path)
        reverse = repositories(reverse_path)

        # app.py 今天的顺序：task 清扫 → v2 清扫
        task_swept = forward.runs.sweep_interrupted_runs()
        v2_swept = sweep_interrupted_v2_runs(forward.runtime)
        # 对调：v2 清扫 → task 清扫
        v2_swept_reverse = sweep_interrupted_v2_runs(reverse.runtime)
        task_swept_reverse = reverse.runs.sweep_interrupted_runs()

        self.assertEqual(2, len(task_swept))
        self.assertEqual(len(task_swept), len(task_swept_reverse))
        self.assertEqual(len(v2_swept), len(v2_swept_reverse))

        def shape(repos, ids):
            return {
                "task_run": (
                    repos.runs.get_run(ids["task_run_id"]).status.value,
                    repos.runs.get_run(ids["task_run_id"]).error,
                    repos.runs.get_run(ids["task_run_id"]).retryable,
                ),
                "reminder_run": (
                    repos.runs.get_run(ids["reminder_run_id"]).status.value,
                    repos.runs.get_run(ids["reminder_run_id"]).error,
                    repos.runs.get_run(ids["reminder_run_id"]).retryable,
                ),
                "task_v2": (
                    repos.runtime.get_run(ids["task_v2_run_id"]).status.value,
                    repos.runtime.get_run(ids["task_v2_run_id"]).error_code,
                ),
                "reminder_v2": (
                    repos.runtime.get_run(ids["reminder_v2_run_id"]).status.value,
                    repos.runtime.get_run(ids["reminder_v2_run_id"]).error_code,
                ),
            }

        self.assertEqual(
            shape(forward, forward_ids), shape(reverse, reverse_ids)
        )

    # ---- (d) T2 先落 v2 FAILED 不改变 task 清扫/通知路径 --------------------
    def test_v2_sweep_first_does_not_change_task_sweep_or_notifications(self) -> None:
        v2_first_path = Path(self._temporary_directory.name) / "v2first.db"
        task_first_path = Path(self._temporary_directory.name) / "taskfirst.db"
        v2_first_ids = seed_interrupted_runs(v2_first_path)
        task_first_ids = seed_interrupted_runs(task_first_path)

        v2_first = repositories(v2_first_path)
        task_first = repositories(task_first_path)

        v2_swept = sweep_interrupted_v2_runs(v2_first.runtime)
        self.assertEqual(2, len(v2_swept))
        task_swept_after = v2_first.runs.sweep_interrupted_runs()

        task_swept_first = task_first.runs.sweep_interrupted_runs()
        sweep_interrupted_v2_runs(task_first.runtime)

        # 既有 task 清扫看到的行、文案、可重试标记**逐字不变**
        self.assertEqual(
            [
                (
                    run.status.value,
                    run.error,
                    run.retryable,
                )
                for run in task_swept_after
            ],
            [
                (run.status.value, run.error, run.retryable)
                for run in task_swept_first
            ],
        )
        self.assertEqual(
            [(TaskRunStatus.FAILED.value, TASK_ERROR, True)] * 2,
            [
                (run.status.value, run.error, run.retryable)
                for run in task_swept_after
            ],
        )

        # 通知路径独立于 v2 run 是否活动：按 swept 逐条分派，仍是各一条
        notification_repository = SqliteNotificationRepository(v2_first.database)
        service = TaskNotificationService(
            notification_repository=notification_repository
        )
        container = SimpleNamespace(
            task_notification_service=service,
            task_repository=v2_first.tasks,
            reminder_repository=v2_first.reminders,
        )
        _notify_swept_runs(container, task_swept_after)
        notes = notification_repository.list_notifications()
        self.assertEqual(2, len(notes))
        self.assertEqual(
            {v2_first_ids["task_id"], v2_first_ids["reminder_id"]},
            {note.task_id for note in notes},
        )
        # 顺序对调不会让 v2 清扫多发通知
        notification_repository_reverse = SqliteNotificationRepository(
            task_first.database
        )
        service_reverse = TaskNotificationService(
            notification_repository=notification_repository_reverse
        )
        _notify_swept_runs(
            SimpleNamespace(
                task_notification_service=service_reverse,
                task_repository=task_first.tasks,
                reminder_repository=task_first.reminders,
            ),
            task_swept_first,
        )
        self.assertEqual(2, len(notification_repository_reverse.list_notifications()))
        self.assertEqual(
            {task_first_ids["task_id"], task_first_ids["reminder_id"]},
            {note.task_id for note in notification_repository_reverse.list_notifications()},
        )


if __name__ == "__main__":
    unittest.main()
