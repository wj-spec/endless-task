"""批次 D Task 1：定时任务可**编辑**（`PATCH /tasks/{id}`，纯后端）。

为什么有这份用例：用户建了一条安排之后，此前**没有任何办法改**——只有暂停 /
恢复 / 取消 / 立即执行这些动作型端点；`title` / `commitment` / `schedule` 一旦
写入就只能删了重建（而"删了重建"会丢掉 `created_at` 这个触发 anchor 与来源锚点）。
本模块把编辑路径钉住，重点是 R-D6（允许面）与 R-D7（改期必须重置 anchor）：

- ① `TaskUpdateShapeTest`：只改 title → 其它列**逐字不变**、`updated_at` 变、
  `resumed_at` **不变**，且到点通知用新 title；把当前排期**原样读回写回**也不动 anchor；
- ② `TaskUpdateScheduleAnchorTest`：改 `schedule` → `resumed_at` 被写、**不会立刻
  补跑**（用"改成相对旧 anchor 已过去的时刻"做对照：改前 `is_due` 为真、改后为假，
  而且"若不重置 anchor 就会补跑"这条反事实也被断言）；
- ③ `TaskUpdateValidationTest`：空 body 400 / 非法 schedule（含 `once`）400 且
  库里**零变化** / `cancelled` 409 / 不存在 404 / `status` 不是允许面（双入口）。

每条用例怎么"能红"写在各自 docstring 里。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    ProviderCompleted,
    ProviderTextDelta,
)
from endless_task.storage import (
    Database,
    SqliteTaskRepository,
    SqliteTaskRunRepository,
)
from endless_task.tasks import TaskScheduler
from tests.fixtures.workspace_client import create_bound_conversation

RUN_ANSWER = "到点执行完成：本周项目进展如下，任务调度与执行链路均正常工作。"
REVIEW_ANSWER = '{"awaiting_user": false, "note": null}'
TITLE = "每日进展总结"
COMMITMENT = "每天总结项目进展"
NEW_TITLE = "每日项目进展总结"
NEW_COMMITMENT = "每天总结一次项目进展并列出阻塞项"


class ReviewProvider:
    """第一条文本给任务 run，第二条给执行审查（非 JSON 会如实回落成 false）。"""

    name = "task-update-api"

    def __init__(self, texts=(None,)) -> None:
        self.texts = list(texts)
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        text = self.texts[min(index, len(self.texts) - 1)]
        if text:
            for chunk in text:
                yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


@asynccontextmanager
async def local_client(database_path: Path, provider=None):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            task_proposals_enabled=False,
            # 关掉进程内调度循环：本用例自己用既有探针 `scan_once(now=…)` /
            # `is_due` 驱动，免得后台 tick 把"到点"次数搅乱。
            scheduler_enabled=False,
        ),
        provider=provider or ReviewProvider([[RUN_ANSWER], [REVIEW_ANSWER]]),
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


def utc_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_time_of_day(*, hours_ago: int) -> str:
    return (
        datetime.now(timezone.utc).astimezone() - timedelta(hours=hours_ago)
    ).strftime("%H:%M")


class TaskUpdateApiTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "update.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_task(
        self,
        conversation_id: str,
        time: str,
        *,
        clock=None,
        title: str = TITLE,
        commitment: str = COMMITMENT,
    ) -> str:
        kwargs = {}
        if clock is not None:
            kwargs["clock"] = clock
        tasks = SqliteTaskRepository(Database(self.database_path), **kwargs)
        return tasks.create_task(
            title=title,
            commitment=commitment,
            schedule={"kind": "daily", "time": time},
            source_conversation_id=conversation_id,
            source_turn_id="turn_seed",
        ).id

    def _row(self, task_id: str) -> dict:
        with Database(self.database_path).connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def _scheduler(self) -> TaskScheduler:
        database = Database(self.database_path)
        return TaskScheduler(
            task_repository=SqliteTaskRepository(database),
            run_repository=SqliteTaskRunRepository(database),
            worker=None,
        )

    async def _wait_for_run(self, client, task_id: str, status: str):
        for _ in range(400):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            if items and items[-1]["status"] == status:
                return items[-1]
            await asyncio.sleep(0.005)
        raise AssertionError(f"Run did not reach {status}")


class TaskUpdateShapeTest(TaskUpdateApiTestBase):
    """① 只改 title：其它列逐字不变、`updated_at` 变、`resumed_at` **不变**。

    能红的方式：
    - 仓储顺手 `UPDATE` 了没被传的列（例如无脑把 `resumed_at` 也写成 now，
      或者重置 `created_at`）→ "逐字不变"断言红；
    - 路由漏了 `ValidationError("Nothing to update.")` 的判定，或把 title 写丢
      → title/通知断言红。
    """

    async def test_title_only_update_leaves_other_columns_byte_identical(
        self,
    ) -> None:
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            before = self._row(task_id)

            response = await client.patch(f"/tasks/{task_id}", json={"title": NEW_TITLE})

            self.assertEqual(200, response.status_code, response.text)
            # 响应形状与列表/详情一致（同一处 `task_json`）。
            self.assertEqual(NEW_TITLE, response.json()["task"]["title"])
            self.assertIn("schedule", response.json()["task"])
            self.assertIn("scheduleDescription", response.json()["task"])

            after = self._row(task_id)
            # 除 title 与 updated_at 外**逐字**不变（含 schedule 落库字节、来源
            # 三列、created_at、resumed_at、cancelled_at）。
            for column in sorted(before):
                if column in ("title", "updated_at"):
                    continue
                with self.subTest(column=column):
                    self.assertEqual(before[column], after[column])
            self.assertEqual(NEW_TITLE, after["title"])
            self.assertNotEqual(before["updated_at"], after["updated_at"])
            # 只改标题**不**动触发 anchor（R-D7）。
            self.assertIsNone(before["resumed_at"])
            self.assertIsNone(after["resumed_at"])

    async def test_edited_title_reaches_the_notification_text(self) -> None:
        """改 title 的涟漪：通知文案在**通知时刻**取当前 title（R-D8）。

        能红的方式：路由把 title 写进别的地方（例如只写 `source` 表）、或者
        仓储没真的落库新 title → 通知标题里还是旧名，本用例红。
        """
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))

            patched = await client.patch(
                f"/tasks/{task_id}", json={"title": NEW_TITLE}
            )
            self.assertEqual(200, patched.status_code, patched.text)

            started = await client.post(f"/tasks/{task_id}/run")
            self.assertEqual(202, started.status_code, started.text)
            await self._wait_for_run(client, task_id, "completed")

            items = (await client.get("/notifications")).json()["items"]
            self.assertEqual(1, len(items))
            item = items[0]
            self.assertIn(NEW_TITLE, item["title"])
            self.assertNotIn(TITLE, item["title"])

    async def test_echoing_the_current_schedule_back_does_not_move_the_anchor(
        self,
    ) -> None:
        """编辑表单"原样读回再写回"排期**不算改期**，anchor 必须不动。

        能红的方式：把"变了"判成"请求里带了 `schedule`"（而不是比较解析后的
        语义）→ 本用例的 `resumed_at` 断言红。这正是前端把当前排期整份回填的
        正常用法：只改个标题不该把下一次触发推到"明天"。
        """
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            time = local_time_of_day(hours_ago=-3)
            task_id = self._seed_task(conversation_id, time)
            before = self._row(task_id)
            self.assertIsNone(before["resumed_at"])

            response = await client.patch(
                f"/tasks/{task_id}",
                json={
                    "title": NEW_TITLE,
                    "schedule": {"kind": "daily", "time": time},
                },
            )

            self.assertEqual(200, response.status_code, response.text)
            after = self._row(task_id)
            self.assertEqual(before["schedule"], after["schedule"])
            self.assertIsNone(after["resumed_at"])
            self.assertEqual(NEW_TITLE, after["title"])


class TaskUpdateScheduleAnchorTest(TaskUpdateApiTestBase):
    """② R-D7：改期重置触发 anchor，**不会**立刻补跑。

    能红的方式：仓储改 `schedule` 却不写 `resumed_at` → 改完 `is_due` 仍然为真
    （而且下面的反事实断言也会红：那条断言说的就是"不重置就会补跑"）；
    `scan_once` 那一刻随即非 0。
    """

    async def test_rescheduling_resets_the_anchor_and_does_not_catch_up(self) -> None:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        Database(self.database_path).initialize()
        # 3 小时前创建、时间设成 1 小时前（相对旧 anchor 已经过去）→ 今天就该跑。
        task_id = self._seed_task(
            "conv_seed",
            local_time_of_day(hours_ago=1),
            clock=lambda: utc_iso(yesterday),
        )
        scheduler = self._scheduler()
        now = datetime.now(timezone.utc)
        before = SqliteTaskRepository(Database(self.database_path)).get_task(task_id)
        self.assertTrue(scheduler.is_due(before, now), "前置条件：改期前它已经到点了")

        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            # 改成"相对**旧** anchor 同样已过去"的另一个时刻（2 小时前）——
            # 这里不是改成未来，而是特意保留"若 anchor 不动就会立刻补跑"的条件。
            response = await client.patch(
                f"/tasks/{task_id}",
                json={
                    "schedule": {
                        "kind": "daily",
                        "time": local_time_of_day(hours_ago=2),
                    }
                },
            )

            self.assertEqual(200, response.status_code, response.text)
            after = SqliteTaskRepository(
                Database(self.database_path)
            ).get_task(task_id)

            # ① 锚点被写：`resumed_at` 从 None 变成 now（语义扩张为"排期变过"）。
            self.assertIsNone(before.resumed_at)
            self.assertIsNotNone(after.resumed_at)
            # ② 改之后**不再**到点。
            self.assertFalse(
                scheduler.is_due(after, now), "改期后不该立刻补跑"
            )
            # ③ 反事实：若这次改期**没有**重置 anchor，它立刻就是到点的
            #    （这就是"改期即触发"那个反直觉 bug 的形状）。
            self.assertTrue(
                scheduler.is_due(replace(after, resumed_at=None), now),
                "锚点若不动，这个安排立刻就会被补跑一次",
            )
            # ④ 真实的调度探针也确认：这一刻没有东西可跑，也没有多出 run 行。
            self.assertEqual(0, await container.task_scheduler.scan_once(now=now))
            runs = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            self.assertEqual([], runs)

            # ⑤ 对照：**再**改到未来时刻 → 到点时照常选中（改期不是死路）。
            tomorrow_time = (
                datetime.now(timezone.utc).astimezone() + timedelta(days=1)
            ).strftime("%H:%M")
            future_patch = await client.patch(
                f"/tasks/{task_id}",
                json={"schedule": {"kind": "daily", "time": tomorrow_time}},
            )
            self.assertEqual(200, future_patch.status_code, future_patch.text)
            self.assertEqual(
                0, await container.task_scheduler.scan_once(now=now)
            )
            self.assertEqual(
                1,
                await container.task_scheduler.scan_once(
                    now=now + timedelta(days=2)
                ),
            )


class TaskUpdateValidationTest(TaskUpdateApiTestBase):
    """③ 空 body / 非法 schedule / `once` / `cancelled` / 不存在 / `status`。

    能红的方式：删掉 `Nothing to update.` 判定（空 body 会 200 而不是 400）、
    去掉 `extra="forbid"`（`status`/`sourceConversationId` 会被静默吃掉）、
    或把 `parse_task_schedule` 的校验挪到写库之后（"零变化"断言红）。
    """

    async def test_empty_body_is_400_nothing_to_update(self) -> None:
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            before = self._row(task_id)

            response = await client.patch(f"/tasks/{task_id}", json={})

            self.assertEqual(400, response.status_code, response.text)
            error = response.json()["error"]
            self.assertEqual("validation_error", error["code"])
            self.assertIn("Nothing to update.", error["message"])
            self.assertEqual(before, self._row(task_id))

    async def test_invalid_schedule_and_once_are_400_with_zero_change(self) -> None:
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            before = self._row(task_id)

            cases = (
                ("once 不是任务", {"kind": "once", "at": "2030-01-01T09:00"}, "任务"),
                ("未知频度", {"kind": "hourly", "time": "09:00"}, "kind"),
                ("时间格式错", {"kind": "daily", "time": "9:00"}, "time"),
                ("weekly 越界", {"kind": "weekly", "time": "09:00", "weekday": 9},
                 "weekday"),
                ("多余键", {"kind": "daily", "time": "09:00", "day": 3},
                 "不支持的字段"),
            )
            for label, schedule, needle in cases:
                with self.subTest(label=label):
                    response = await client.patch(
                        f"/tasks/{task_id}",
                        json={"title": "不该被写进去", "schedule": schedule},
                    )
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual("validation_error", error["code"])
                    self.assertIn(needle, error["message"])
                    # 非法输入**零变化**：连 title 与 updated_at 都没动。
                    self.assertEqual(before, self._row(task_id))

    async def test_title_too_long_is_400_with_zero_change(self) -> None:
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            before = self._row(task_id)

            response = await client.patch(
                f"/tasks/{task_id}", json={"title": "长" * 201}
            )
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual("validation_error", response.json()["error"]["code"])
            self.assertEqual(before, self._row(task_id))

    async def test_status_is_not_an_accepted_field(self) -> None:
        """R-D6：`status` 不是允许面（否则与 pause/resume/cancel 双入口）。"""
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            before = self._row(task_id)

            response = await client.patch(
                f"/tasks/{task_id}", json={"status": "paused"}
            )
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(
                "invalid_request", response.json()["error"]["code"]
            )
            self.assertEqual(before, self._row(task_id))

            # 来源锚点与 created_at 同样不接受改写（`extra="forbid"`）。
            for field, value in (
                ("sourceConversationId", "conv_other"),
                ("sourceTurnId", "turn_other"),
                ("sourceProposalId", "taskp_other"),
                ("createdAt", "2020-01-01T00:00:00.000Z"),
            ):
                with self.subTest(field=field):
                    rejected = await client.patch(
                        f"/tasks/{task_id}", json={field: value}
                    )
                    self.assertEqual(400, rejected.status_code, rejected.text)
                    self.assertEqual(before, self._row(task_id))

    async def test_cancelled_task_is_409(self) -> None:
        """`cancelled` 是终态（与 `_transition` 的允许集对齐）。"""
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))
            # 走**真实**的取消端点（`tasks` 表的 CHECK 要求 cancelled_at 同步写）。
            cancelled = await client.post(f"/tasks/{task_id}/cancel")
            self.assertEqual(200, cancelled.status_code, cancelled.text)
            before = self._row(task_id)

            response = await client.patch(
                f"/tasks/{task_id}", json={"title": NEW_TITLE}
            )
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual("invalid_state", response.json()["error"]["code"])
            self.assertEqual(before, self._row(task_id))

    async def test_missing_task_is_404(self) -> None:
        async with local_client(self.database_path) as (client, app):
            response = await client.patch(
                "/tasks/task_missing", json={"title": NEW_TITLE}
            )
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual("not_found", response.json()["error"]["code"])

    async def test_commitment_and_schedule_can_be_updated_together(self) -> None:
        """允许面里的三个字段可以一起改，且都如实落库。"""
        async with local_client(self.database_path) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            task_id = self._seed_task(conversation_id, local_time_of_day(hours_ago=-3))

            response = await client.patch(
                f"/tasks/{task_id}",
                json={
                    "title": NEW_TITLE,
                    "commitment": NEW_COMMITMENT,
                    "schedule": {"kind": "weekly", "weekday": 3, "time": "07:30"},
                },
            )

            self.assertEqual(200, response.status_code, response.text)
            task = response.json()["task"]
            self.assertEqual(NEW_TITLE, task["title"])
            self.assertEqual(NEW_COMMITMENT, task["commitment"])
            self.assertEqual(
                {"kind": "weekly", "time": "07:30", "weekday": 3}, task["schedule"]
            )
            row = self._row(task_id)
            self.assertEqual(
                '{"kind":"weekly","time":"07:30","weekday":3}', row["schedule"]
            )
            self.assertIsNotNone(row["resumed_at"])
            # 来源三列与 created_at 一如既往。
            self.assertEqual(conversation_id, row["source_conversation_id"])
            self.assertEqual("turn_seed", row["source_turn_id"])


if __name__ == "__main__":
    unittest.main()
