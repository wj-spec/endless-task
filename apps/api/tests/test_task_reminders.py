from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.repositories import ValidationError
from endless_task.domain.task_schedule import (
    ReminderDue,
    TaskSchedule,
    parse_reminder_due,
    parse_schedule_intent,
    parse_task_schedule,
    reminder_due_to_utc_iso,
)
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteReminderRepository,
)
from tests.fixtures.v2_client import run_snapshot, send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation

RUN_ANSWER = (
    "好的，明天下午三点我会提醒你整理这份文档；"
    "到点后会按你确认的承诺自动执行，不会提前打扰你。"
)


class TextProvider:
    name = "reminders"

    def __init__(self, texts=()) -> None:
        self.texts = list(texts)
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


class WaitingProvider:
    """产出半句后**挂住**，直到取消令牌被取消。

    用来把一条提醒 run 停在"在飞"状态，好让用例走**真实**的取消端点
    （`POST /tasks/{task_id}/runs/{run_id}/cancel`）——这是切片 10 残留 A1 的
    "取消提醒 run"路径在今天的唯一入口。
    """

    name = "reminders-waiting"

    def __init__(self) -> None:
        self.requests = []
        self.entered = asyncio.Event()

    async def stream(self, request, cancellation_token):
        self.requests.append(request)
        yield ProviderTextDelta(text="正在整理")
        self.entered.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


@asynccontextmanager
async def local_client(database_path: Path, provider):
    app = create_app(
            settings=AppSettings(
                database_path=database_path,
                memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            scheduler_tick_seconds=0.05,
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


class ReminderProtocolTest(unittest.TestCase):
    def test_parse_matrix(self) -> None:
        due = parse_reminder_due({"kind": "once", "at": "2026-08-26T15:00"})
        self.assertEqual("2026-08-26T15:00", due.at)
        for bad in (
            {"kind": "once", "at": "2026-13-01T10:00"},
            {"kind": "once", "at": "2026-02-30T10:00"},
            {"kind": "once", "at": "明天 10:00"},
            {"kind": "once", "at": "2026-08-26T24:00"},
            {"kind": "once"},
        ):
            with self.assertRaises(ValidationError):
                parse_reminder_due(bad)

    def test_schedule_intent_dispatch(self) -> None:
        once = parse_schedule_intent({"kind": "once", "at": "2026-08-26T15:00"})
        self.assertIsInstance(once, ReminderDue)
        daily = parse_schedule_intent({"kind": "daily", "time": "09:00"})
        self.assertIsInstance(daily, TaskSchedule)

    def test_schedule_identity_spans_both_schedule_types(self) -> None:
        """`schedule_identity` 必须能同时吃 TaskSchedule 与 ReminderDue。

        它是"同一个安排"的判据，被提案去重与 supersede 依赖；两个类型若只有
        其中一个被支持，调用方就会在另一个类型上崩掉。
        """
        from endless_task.domain.task_schedule import schedule_identity

        self.assertEqual(
            schedule_identity(ReminderDue(at="2026-08-26T15:00")),
            schedule_identity(ReminderDue(at="2026-08-26T15:00")),
        )
        self.assertNotEqual(
            schedule_identity(ReminderDue(at="2026-08-26T15:00")),
            schedule_identity(ReminderDue(at="2026-08-26T16:00")),
        )
        self.assertEqual(
            schedule_identity(parse_task_schedule({"kind": "daily", "time": "09:00"})),
            schedule_identity(parse_task_schedule({"kind": "daily", "time": "09:00"})),
        )
        self.assertNotEqual(
            schedule_identity(parse_task_schedule({"kind": "daily", "time": "09:00"})),
            schedule_identity(parse_task_schedule({"kind": "daily", "time": "10:00"})),
        )
        # 一次性与周期即便字段巧合相同也不得视为同一安排。
        self.assertNotEqual(
            schedule_identity(ReminderDue(at="2026-08-26T09:00")),
            schedule_identity(parse_task_schedule({"kind": "daily", "time": "09:00"})),
        )


class ReminderFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "rem.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _wait_for_runs(self, client, run_key: str, count: int):
        for _ in range(600):
            items = (await client.get(f"/tasks/{run_key}/runs")).json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Expected run did not appear")

    async def test_extraction_to_proposal_to_reminder(self) -> None:
        at = (datetime.now().astimezone() + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M"
        )
        extraction = json.dumps(
            {
                "task": {
                    "title": "整理文档",
                    "commitment": "明天下午三点整理这份文档",
                    "schedule": {"kind": "once", "at": at},
                    "reason": "用户要求一次性定时提醒",
                }
            },
            ensure_ascii=False,
        )
        provider = TextProvider([[RUN_ANSWER], [extraction]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            handle = await send_message(
                client,
                conversation_id,
                "明天下午三点提醒我整理这份文档",
                idempotency_key="req-rem-1",
            )
            await wait_for_run_terminal(app.state.container, handle["runId"])
            for _ in range(400):
                proposals = (
                    await client.get(
                        f"/conversations/{conversation_id}/task-proposals"
                    )
                ).json()["items"]
                if proposals:
                    break
                await asyncio.sleep(0.005)
            self.assertEqual(1, len(proposals))
            proposal = proposals[0]
            self.assertEqual("once", proposal["schedule"]["kind"])
            self.assertIn("一次性", proposal["scheduleDescription"])

            resolved = await client.post(
                f"/task-proposals/{proposal['id']}/resolve",
                json={"decision": "accept"},
            )
            self.assertEqual(200, resolved.status_code)
            self.assertIn("reminder", resolved.json())

            reminders = (await client.get("/reminders")).json()["items"]
            self.assertEqual(1, len(reminders))
            self.assertEqual("pending", reminders[0]["status"])
            tasks = (await client.get("/tasks")).json()["items"]
            self.assertEqual([], tasks)

    async def test_due_reminder_fires_once_and_notifies(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        past_local = datetime.now().astimezone() - timedelta(hours=1)
        due = ReminderDue(at=past_local.strftime("%Y-%m-%dT%H:%M"))
        reminders = SqliteReminderRepository(database)
        reminder = reminders.create_reminder(
            title="整理文档",
            commitment="整理这份文档",
            due_at=reminder_due_to_utc_iso(due),
            source_conversation_id="conv_seed",
            source_turn_id="turn_seed",
        )
        provider = TextProvider(
            [[RUN_ANSWER], ['{"awaiting_user": false, "note": null}']]
        )
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            # 源会话需存在：用 API 重建同 id 不可行，改为新建提醒指向真实会话。
            reminders.cancel_reminder(reminder.id)
            due2 = ReminderDue(at=past_local.strftime("%Y-%m-%dT%H:%M"))
            reminder = reminders.create_reminder(
                title="整理文档",
                commitment="整理这份文档",
                due_at=reminder_due_to_utc_iso(due2),
                source_conversation_id=conversation_id,
                source_turn_id="turn_seed",
            )

            items = await self._wait_for_runs(client, reminder.id, 1)
            for _ in range(400):
                items = (await client.get(f"/tasks/{reminder.id}/runs")).json()["items"]
                if items and items[0]["status"] != "running":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual("completed", items[0]["status"])

            await asyncio.sleep(0.3)
            items = (await client.get(f"/tasks/{reminder.id}/runs")).json()["items"]
            self.assertEqual(1, len(items))

            # 批次 B Task 1（ADR-1）：提醒的到点执行落进**该提醒自己的执行会话**，
            # `task_runs.conversation_id` 就是它（旧形态硬编码回源会话）。
            landing_id = items[0]["conversationId"]
            self.assertNotEqual(conversation_id, landing_id)
            snapshot = await run_snapshot(client, landing_id)
            user_messages = [
                entry["data"]["content"]
                for entry in snapshot["entries"]
                if entry.get("actor") == "user"
                and isinstance(entry.get("data", {}).get("content"), str)
            ]
            self.assertTrue(
                any(msg.startswith("【提醒】") for msg in user_messages)
            )
            # 源会话里**没有**这条提醒
            source_snapshot = await run_snapshot(client, conversation_id)
            source_messages = [
                entry["data"]["content"]
                for entry in source_snapshot["entries"]
                if entry.get("actor") == "user"
                and isinstance(entry.get("data", {}).get("content"), str)
            ]
            self.assertFalse(
                any(msg.startswith("【提醒】") for msg in source_messages),
                source_messages,
            )

            listing = (await client.get("/reminders")).json()["items"]
            self.assertEqual("fired", listing[0]["status"])

            notes = (await client.get("/notifications")).json()["items"]
            self.assertEqual(1, len(notes))
            self.assertIn("提醒", notes[0]["title"])

    async def test_cancelled_in_flight_reminder_run_leaves_honest_notification(
        self,
    ) -> None:
        """切片 10 残留 A1：在飞提醒 run 落 `CANCELLED` → **不许**零通知。

        可达性（本用例同时就是证据）：今天取消一条在飞提醒 run 的唯一入口是
        `POST /tasks/{task_id}/runs/{run_id}/cancel`（`task_runs.task_id` 对提醒 run
        存 reminder id；`TaskWorker.cancel` 是全仓唯一的 `token.cancel()` 调用点，
        见 `tasks/worker.py`）。T3 的取消端点**未接进 UI**，所以这条路径今天主要由
        直接 API 调用触发。取消后 `_execute_reminder` 落 `CANCELLED` → 旧的
        `notify_reminder` 对非 COMPLETED/FAILED 的终态**提前 return** → 提醒已
        `fired`、消息没送到、用户什么都不知道。

        红检查：把 `notify_reminder` 改回"非 COMPLETED/FAILED 提前 return"
        → 通知数断言红（0 而非 1）。
        """
        database = Database(self.database_path)
        database.initialize()
        past_local = datetime.now().astimezone() - timedelta(hours=1)
        reminders = SqliteReminderRepository(database)
        provider = WaitingProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            reminder = reminders.create_reminder(
                title="整理文档",
                commitment="整理这份文档",
                due_at=reminder_due_to_utc_iso(
                    ReminderDue(at=past_local.strftime("%Y-%m-%dT%H:%M"))
                ),
                source_conversation_id=conversation_id,
                source_turn_id="turn_seed",
            )

            # 等调度器点着提醒、run 停在"在飞"（provider 挂住不返回）。
            items = await self._wait_for_runs(client, reminder.id, 1)
            for _ in range(600):
                items = (await client.get(f"/tasks/{reminder.id}/runs")).json()["items"]
                if items and items[0]["status"] == "running":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual("running", items[0]["status"])
            await asyncio.wait_for(provider.entered.wait(), timeout=4.0)
            run_id = items[0]["id"]

            # 真实取消端点：本进程持有该 run 的在飞句柄 → accepted: true。
            accepted = await client.post(
                f"/tasks/{reminder.id}/runs/{run_id}/cancel"
            )
            self.assertEqual(200, accepted.status_code, accepted.text)
            self.assertTrue(accepted.json()["accepted"], accepted.text)

            for _ in range(600):
                items = (await client.get(f"/tasks/{reminder.id}/runs")).json()["items"]
                if items and items[0]["status"] != "running":
                    break
                await asyncio.sleep(0.005)
            self.assertEqual("cancelled", items[0]["status"])

            # ① 如实可见：不是"零通知"。第一句必须是"未执行"，不许被
            #    "不会自动重跑"掩盖。
            notes = (await client.get("/notifications")).json()["items"]
            self.assertEqual(1, len(notes), notes)
            self.assertEqual(run_id, notes[0]["runId"])
            self.assertIn("提醒", notes[0]["title"])
            self.assertTrue(notes[0]["body"].startswith("本次提醒未执行"))
            self.assertIn("不会自动重跑", notes[0]["body"])

            # ② 提醒自己仍是一次性的 `fired`：取消不改写提醒状态（"没有重跑入口"
            #    这句话与库里的状态一致）。
            listing = (await client.get("/reminders")).json()["items"]
            self.assertEqual("fired", listing[0]["status"])

    async def test_cancel_matrix_and_cascade(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        future_local = datetime.now().astimezone() + timedelta(hours=5)
        due = ReminderDue(at=future_local.strftime("%Y-%m-%dT%H:%M"))
        reminders = SqliteReminderRepository(database)
        provider = TextProvider([[RUN_ANSWER]])
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await create_bound_conversation(client))["id"]
            reminder = reminders.create_reminder(
                title="发结果",
                commitment="把结果发给我",
                due_at=reminder_due_to_utc_iso(due),
                source_conversation_id=conversation_id,
                source_turn_id="turn_seed",
            )
            cancelled = await client.post(f"/reminders/{reminder.id}/cancel")
            self.assertEqual(200, cancelled.status_code)
            self.assertEqual(
                409, (await client.post(f"/reminders/{reminder.id}/cancel")).status_code
            )

            second = reminders.create_reminder(
                title="发结果2",
                commitment="把结果发给我",
                due_at=reminder_due_to_utc_iso(due),
                source_conversation_id=conversation_id,
                source_turn_id="turn_seed",
            )
            deleted = await client.delete(f"/conversations/{conversation_id}")
            self.assertEqual(204, deleted.status_code)
            listing = (
                await client.get("/reminders?include_cancelled=true")
            ).json()["items"]
            by_id = {item["id"]: item for item in listing}
            self.assertEqual("cancelled", by_id[second.id]["status"])


if __name__ == "__main__":
    unittest.main()