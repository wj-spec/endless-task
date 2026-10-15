"""切片 10 T3 Step 1 / 验收 ①：在飞的定时任务/提醒 run 可取消。

今天不成立的原因（起点 d2a5376）：`tasks/worker.py` 执行每一轮时用的是**匿名**
`CancellationToken()`，外部拿不到句柄；`POST /tasks/{id}/cancel` 只改任务状态，
碰不到正在跑的 run（`recon-longrun.md` §4 第三条路）。

本模块钉住：
- 在飞的定时 run 能经 `POST /tasks/{task_id}/runs/{run_id}/cancel` 取消**到终态**；
- 没有在飞句柄时**如实**返回 `accepted:false`（不存在的 run → 404，别人的 run → 404）；
- 提醒 run 走同一条通道（`task_runs.task_id` 对提醒就是 reminder id）；
- 取消会释放 `max_concurrent_task_runs=1` 的信号量名额（顺带的可观测行为，
  上限语义本身不动）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import TaskRunStatus, TaskRunTrigger
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2 import AgentRunExecutor, RunStatus
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
from tests.fixtures.workspace_client import create_bound_conversation

ANSWER = "本周项目进展如下：提案与确认链路已完成，执行台账已就位。"
COMMITMENT = "每周一 09:00 总结上周的项目进展"
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
#: v2 run 的终态（`RunStatus` 里没有 `is_terminal` 帮助函数，写死以防语义漂移）。
TERMINAL_V2_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


class GatedProvider:
    """第一个请求会被闸住；闸门关闭期间每 10ms 检查一次取消令牌。

    用轮询而不是 `Event.wait()`：取消必须在**闸门还关着**时就能被观察到，
    否则这条用例测不到"取消在飞 run"。
    """

    name = "task-cancel"

    def __init__(self, *, gate_first: bool = True) -> None:
        self.gate = asyncio.Event()
        self.gate_first = gate_first
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token: CancellationToken):
        index = len(self.requests)
        self.requests.append(request)
        if self.gate_first and index == 0:
            while not self.gate.is_set():
                cancellation_token.raise_if_cancelled()
                await asyncio.sleep(0.01)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta(ANSWER)
        cancellation_token.raise_if_cancelled()
        yield ProviderCompleted()


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.005):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached in time")


@asynccontextmanager
async def local_client(database_path: Path, provider, **settings_overrides):
    settings = {
        "database_path": database_path,
        "memory_proposals_enabled": False,
        "artifact_proposals_enabled": False,
        "scheduler_tick_seconds": 0.05,
    }
    settings.update(settings_overrides)
    app = create_app(settings=AppSettings(**settings), provider=provider)
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
        # 断言失败时也别把闸门关着退出：`task_worker.drain()` 会等在飞 run，
        # 一个永远停在被闸住的 provider 里的 run 会把整个测试进程挂死。
        provider.gate.set()
        await lifespan.__aexit__(None, None, None)


class TaskRunCancelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "cancel.db"
        self.database = Database(self.database_path)
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _task_repository(self) -> SqliteTaskRepository:
        database = Database(self.database_path)
        database.initialize()
        return SqliteTaskRepository(database)

    def _reminder_repository(self) -> SqliteReminderRepository:
        database = Database(self.database_path)
        database.initialize()
        return SqliteReminderRepository(database)

    async def _runs(self, client, subject_id: str) -> list[dict]:
        response = await client.get(f"/tasks/{subject_id}/runs")
        self.assertEqual(200, response.status_code)
        return response.json()["items"]

    async def _wait_for_terminal(
        self, client, subject_id: str, run_id: str
    ) -> Optional[dict]:
        for _ in range(600):
            items = await self._runs(client, subject_id)
            finished = next(
                (item for item in items if item["id"] == run_id),
                None,
            )
            if finished is not None and finished["status"] in TERMINAL_RUN_STATUSES:
                return finished
            await asyncio.sleep(0.01)
        return None

    async def _wait_for_running(self, client, subject_id: str) -> list[dict]:
        for _ in range(600):
            items = await self._runs(client, subject_id)
            if items and items[0]["status"] == "running":
                return items
            await asyncio.sleep(0.01)
        raise AssertionError("no running run appeared in time")

    def _create_task(self, conversation_id: str, title: str = "每周进展"):
        return self._task_repository().create_task(
            title=title,
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        )

    # ---- 验收 ①：在飞 → 终态 ------------------------------------------------
    async def test_in_flight_task_run_can_be_cancelled_to_terminal(self) -> None:
        provider = GatedProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation = await create_bound_conversation(client)
            task = self._create_task(conversation["id"])
            accepted = await client.post(f"/tasks/{task.id}/run")
            self.assertEqual(202, accepted.status_code)
            run_id = accepted.json()["run"]["id"]

            # 真的在飞：执行器已经把这一轮交给 provider，而闸门还关着。
            await _wait_until(lambda: len(provider.requests) == 1)
            items = await self._runs(client, task.id)
            self.assertEqual("running", items[0]["status"])
            self.assertIsNone(items[0]["finishedAt"])
            self.assertTrue(app.state.container.task_worker.in_flight(run_id))

            cancelled = await client.post(
                f"/tasks/{task.id}/runs/{run_id}/cancel"
            )
            self.assertEqual(200, cancelled.status_code)
            self.assertEqual(
                {"runId": run_id, "accepted": True, "status": "running"},
                cancelled.json(),
            )

            finished = await self._wait_for_terminal(client, task.id, run_id)
            self.assertIsNotNone(finished, "取消后 run 必须落到终态")
            self.assertEqual("cancelled", finished["status"])
            # 取消不是"改任务状态"的副作用：任务仍是 active
            task_json = (await client.get(f"/tasks/{task.id}")).json()["task"]
            self.assertEqual("active", task_json["status"])
            # 句柄已释放：再取消必须如实说不
            again = await client.post(f"/tasks/{task.id}/runs/{run_id}/cancel")
            self.assertEqual(
                {"runId": run_id, "accepted": False, "status": "cancelled"},
                again.json(),
            )
            provider.gate.set()

    # ---- 验收 ①：没有在飞 run 时如实 accepted:false --------------------------
    async def test_cancel_reports_false_when_nothing_is_in_flight(self) -> None:
        provider = GatedProvider(gate_first=False)
        async with local_client(self.database_path, provider) as (client, app):
            conversation = await create_bound_conversation(client)
            task = self._create_task(conversation["id"])
            accepted = await client.post(f"/tasks/{task.id}/run")
            run_id = accepted.json()["run"]["id"]
            finished = await self._wait_for_terminal(client, task.id, run_id)
            self.assertEqual("completed", finished["status"])

            response = await client.post(f"/tasks/{task.id}/runs/{run_id}/cancel")
            self.assertEqual(200, response.status_code)
            self.assertEqual(
                {"runId": run_id, "accepted": False, "status": "completed"},
                response.json(),
            )

    async def test_cancel_unknown_or_foreign_run_is_404(self) -> None:
        provider = GatedProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation = await create_bound_conversation(client)
            first = self._create_task(conversation["id"], title="甲")
            second = self._create_task(conversation["id"], title="乙")
            accepted = await client.post(f"/tasks/{first.id}/run")
            run_id = accepted.json()["run"]["id"]
            await _wait_until(lambda: len(provider.requests) == 1)

            missing = await client.post(
                f"/tasks/{first.id}/runs/run_does_not_exist/cancel"
            )
            self.assertEqual(404, missing.status_code)
            # 别的 subject 的 run id：不许借道取消
            foreign = await client.post(
                f"/tasks/{second.id}/runs/{run_id}/cancel"
            )
            self.assertEqual(404, foreign.status_code)
            still = await client.post(f"/tasks/{first.id}/runs/{run_id}/cancel")
            self.assertTrue(still.json()["accepted"])
            provider.gate.set()

    # ---- 提醒 run 走同一条通道 ----------------------------------------------
    async def test_in_flight_reminder_run_can_be_cancelled(self) -> None:
        provider = GatedProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation = await create_bound_conversation(client)
            reminders = self._reminder_repository()
            reminder = reminders.create_reminder(
                title="整理文档",
                commitment="整理这份文档",
                due_at="2020-01-01T00:00:00.000Z",
                source_conversation_id=conversation["id"],
                source_turn_id="turn_seed",
                source_proposal_id=None,
            )
            # 调度器（tick 50ms）会把它当成到期 pending 提醒起一轮
            await _wait_until(lambda: len(provider.requests) == 1)
            runs = await self._wait_for_running(client, reminder.id)
            run_id = runs[0]["id"]

            cancelled = await client.post(
                f"/tasks/{reminder.id}/runs/{run_id}/cancel"
            )
            self.assertEqual(200, cancelled.status_code)
            self.assertTrue(cancelled.json()["accepted"])

            finished = await self._wait_for_terminal(client, reminder.id, run_id)
            self.assertIsNotNone(finished, "提醒 run 必须落到终态")
            self.assertEqual("cancelled", finished["status"])
            # 提醒到时即 mark_fired（既有语义，本任务不改）——用户主动取消它仍是"已触发"
            listing = (await client.get("/reminders")).json()["items"]
            self.assertEqual("fired", listing[0]["status"])
            provider.gate.set()

    # ---- 顺带：取消后立刻放行被堵在信号量上的下一个到期任务 ------------------
    async def test_cancelling_a_run_releases_the_concurrency_slot(self) -> None:
        provider = GatedProvider()
        async with local_client(self.database_path, provider) as (client, app):
            self.assertEqual(
                1, app.state.container.settings.max_concurrent_task_runs
            )
            conversation = await create_bound_conversation(client)
            first = self._create_task(conversation["id"], title="甲")
            second = self._create_task(conversation["id"], title="乙")

            first_run = (await client.post(f"/tasks/{first.id}/run")).json()["run"]
            await _wait_until(lambda: len(provider.requests) == 1)
            second_run = (await client.post(f"/tasks/{second.id}/run")).json()["run"]
            self.assertEqual("running", second_run["status"])
            # 乙还没被交给 provider：它堵在 max_concurrent_task_runs=1 的信号量上
            await asyncio.sleep(0.2)
            self.assertEqual(1, len(provider.requests))

            cancelled = await client.post(
                f"/tasks/{first.id}/runs/{first_run['id']}/cancel"
            )
            self.assertTrue(cancelled.json()["accepted"])

            # 名额一释放，乙立刻跑起来并完成
            # （用 >= 而不是 ==：任务跑完后的复盘 `TaskRunReviewService` 也会调 provider）
            await _wait_until(lambda: len(provider.requests) >= 2)
            finished = await self._wait_for_terminal(
                client, second.id, second_run["id"]
            )
            self.assertEqual("completed", finished["status"])
            first_finished = await self._wait_for_terminal(
                client, first.id, first_run["id"]
            )
            self.assertEqual("cancelled", first_finished["status"])
            provider.gate.set()

    # ---- 切片 10 终审修复 W-A：取消**排队中**的 run 不许漏一条永久 running 的 v2 行
    def _repository_worker(self, provider, *, max_concurrent_task_runs: int = 1):
        """仓储级的 worker（同 `tests/test_task_run_cancel.py` 的 HTTP 面同源）。

        为什么这里不走 HTTP：这条用例要直接观察 **v2 run 行**与"落点会话还能不能
        提交新 turn"——前者没有 HTTP 面，后者用 `create_message_submission` 就是
        `POST /messages` 的那道活跃闸（409）本身。
        """
        chat_repository = SqliteChatRepository(self.database)
        task_repository = SqliteTaskRepository(self.database)
        run_repository = SqliteTaskRunRepository(self.database)
        runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        executor = AgentRunExecutor(
            repository=runtime_v2_repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )
        worker = TaskWorker(
            task_repository=task_repository,
            run_repository=run_repository,
            runtime_v2_repository=runtime_v2_repository,
            task_executor=executor,
            chat_repository=chat_repository,
            max_concurrent_task_runs=max_concurrent_task_runs,
        )
        return worker, chat_repository, task_repository, run_repository, runtime_v2_repository

    def _seed_task(self, task_repository: SqliteTaskRepository, conversation_id: str, title: str):
        return task_repository.create_task(
            title=title,
            commitment=COMMITMENT,
            schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        )

    async def test_cancelling_a_queued_run_does_not_leak_a_running_v2_row(self) -> None:
        """排队中被取消：task_run **与** v2 run 都到终态，落点会话不再 409，还能重跑。

        起点 `ec997c5` 的真实缺口：`AgentRunExecutor.execute` 把
        `cancellation_token.raise_if_cancelled()` 放在唯一处理 `RuntimeCancelled` 的
        `try` **之外**。取消一个还堵在 `max_concurrent_task_runs` 后面的 run 时，
        它拿到名额 → `start_run` 建好 v2 行（`running`）→ 立刻在 try 外抛出 →
        调用方只把 task_run 落 `cancelled`，v2 行**永远停在 running**：落点会话被
        `storage/v2/transcript.py` 的活跃闸永久占住（新 turn 恒 `ConflictError`，
        同一 task 之后每次 run 都失败），只能重启自愈。
        """
        provider = GatedProvider()
        (
            worker,
            chat_repository,
            task_repository,
            run_repository,
            runtime_v2_repository,
        ) = self._repository_worker(provider)
        conversation = chat_repository.create_conversation()
        # 源会话必须已经有 v1 turn，`_ensure_branch` 才会真的开出执行分支
        # （否则 `create_branch` 抛 InvalidStateError → branch=None → 落点即源会话）。
        chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="seed-turn",
            content="你好",
        )
        first_task = self._seed_task(task_repository, conversation.id, "甲")
        second_task = self._seed_task(task_repository, conversation.id, "乙")

        # 甲占住唯一的名额（provider 第一轮被闸住）
        first = await worker.start(first_task.id, trigger=TaskRunTrigger.MANUAL)
        await _wait_until(lambda: len(provider.requests) == 1)
        # 乙在信号量后面排队：它的 task_run 已经 running，但 `execute` 还没被唤醒
        second = await worker.start(second_task.id, trigger=TaskRunTrigger.MANUAL)
        self.assertIsNone(run_repository.get_run(second.id).turn_id)
        self.assertTrue(worker.in_flight(second.id))
        # 取消发生在"排队中"：v2 行为此还没建出来，所以还看不出问题
        self.assertTrue(worker.cancel(second.id))

        # 放行甲 → 乙拿到名额 → 建 v2 行 → 命中取消检查
        provider.gate.set()
        finished = None
        for _ in range(600):
            candidate = run_repository.get_run(second.id)
            if candidate.status is TaskRunStatus.CANCELLED:
                finished = candidate
                break
            await asyncio.sleep(0.01)
        self.assertIsNotNone(finished, "排队中被取消的 run 必须落到 cancelled")
        self.assertIsNotNone(finished.turn_id, "它已经建出了 v2 行（正是漏终态的那一条）")

        # ① 断言：**v2 行也到终态**（起点：永久 running）
        leaked = runtime_v2_repository.get_run(finished.turn_id)
        self.assertIn(
            leaked.status,
            TERMINAL_V2_RUN_STATUSES,
            f"v2 run 必须落到终态，实际 {leaked.status.value}（永久 running 会占死会话）",
        )
        self.assertEqual(RunStatus.CANCELLED, leaked.status)

        # ② 断言：落点会话不再被活跃闸挡住（= `POST /messages` 不再 409）
        landing_id = finished.conversation_id
        self.assertIsNotNone(landing_id)
        submission = runtime_v2_repository.create_message_submission(
            conversation_id=landing_id,
            lane_id=None,
            content="之后的一轮",
            client_request_id="after-queued-cancel",
        )
        self.assertTrue(submission.created)
        self.assertIs(RunStatus.CREATED, submission.run.status)
        # 这条探针 turn 必须跑完，否则它会**自己**成为落点会话的下一条活跃 run，
        # ③ 里的重跑就会撞上它（那是本用例自己造的，不是被测缺口）。
        probe_result = await worker._task_executor.execute(
            submission.run.id, cancellation_token=CancellationToken()
        )
        self.assertIs(RunStatus.COMPLETED, probe_result.status)
        self.assertEqual(
            (),
            tuple(
                run
                for run in runtime_v2_repository.list_runs(
                    conversation_id=landing_id
                )
                if run.status not in TERMINAL_V2_RUN_STATUSES
            ),
            "落点会话里不许再留任何活动态 v2 行",
        )

        # ③ 断言：同一 task 之后还能重跑成功（起点：每次 run 都撞 409 而失败）
        await worker.drain()
        rerun = await worker.start(second_task.id, trigger=TaskRunTrigger.SCHEDULED)
        await worker.drain()
        rerun_finished = run_repository.get_run(rerun.id)
        self.assertEqual(TaskRunStatus.COMPLETED, rerun_finished.status)
        self.assertIsNotNone(rerun_finished.turn_id)
        self.assertIn(
            runtime_v2_repository.get_run(rerun_finished.turn_id).status,
            TERMINAL_V2_RUN_STATUSES,
        )
        provider.gate.set()


if __name__ == "__main__":
    unittest.main()
