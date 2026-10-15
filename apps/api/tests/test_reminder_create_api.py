"""批次 D Task 1：一次性提醒可**显式创建**（`POST /reminders`，纯后端）。

为什么有这份用例：目标 (a) 要求"按用户设定的时间/周期无人值守地发起任务……"，
而在此之前创建提醒只有一条路——LLM 抽取提案 → `POST /task-proposals/{id}/resolve`
接受；`/reminders` 只有 `GET /reminders` 与 `POST /reminders/{id}/cancel`。
本模块把"显式创建"这条路径钉住：

- ① `CreateReminderApiTest`：201、库里恰好 1 行、`due_at` 是**归一后的 UTC ISO+Z**
  （不是请求里的本地墙钟字符串）；
- ② 五个 `missing_*` 各一条 400 + **零库行**；来源两列相容（`source_turn_mismatch`）
  两种失败各一条 400 + 零库行；非法 `dueAt` 三态 400 + 零库行；
- ③ `reminder_due_in_past`：**R-D3 后半**——`parse_reminder_due` 不判"已过去"，
  所以端点必须在它之后自己挡（否则会**立刻**被调度器选中）；
- ④ `ReminderTimezoneAnchorTest`：本地墙钟 → UTC 的**时区锚定**（在固定
  `TZ=Asia/Shanghai` 下断言落库值与"到点能被选中"的绝对时刻）；
- ⑤ `ReminderShapeIdentityTest`：本端点写出的行与**提案接受**写出的行**逐列同形**；
- ⑥ `SingleReminderWriterTest`：`INSERT INTO reminders` 全仓只有一处。

每条用例怎么"能红"写在各自 docstring 里。
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.api.routes.reminders import reminder_due_has_passed
from endless_task.domain.task_schedule import ReminderDue, reminder_due_to_utc_iso
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import Database
from tests.fixtures.v2_client import send_message, wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation

ANSWER = "好的，我记下了，到点会提醒你；不会提前打扰你。"
TITLE = "给妈妈打电话"
COMMITMENT = "打个电话问问家里情况"

#: 归一后的落库形状：UTC ISO(ms) + `Z`（与 `scheduler.py` 的 `now_iso` 同格式，
#: 否则 `due_at <= now_iso` 的**字符串比较**不成立）。
UTC_ISO_MS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class StaticProvider:
    """每次请求都回同一段文本；本模块不关心 provider 的产出内容。"""

    name = "reminder-create-api"

    def __init__(self, text: str = ANSWER) -> None:
        self.text = text
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        yield ProviderTextDelta(text=self.text)
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
            # 关掉进程内调度循环：本用例自己用既有探针 `scan_once(now=...)` /
            # `list_due_pending` 驱动，免得后台 tick 把"到点"次数搅乱。
            scheduler_enabled=False,
        ),
        provider=provider or StaticProvider(),
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


def count_rows(database: Database, table: str) -> int:
    with database.connect() as connection:
        row = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"])


def reminder_rows(database: Database) -> dict[str, dict]:
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM reminders").fetchall()
    return {row["id"]: dict(row) for row in rows}


def utc_iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def future_local_at(*, minutes: int = 0, hours: int = 3) -> str:
    """本地墙钟：现在 + 偏移，`YYYY-MM-DDTHH:MM`（与 `_ONCE_PATTERN` 同形）。"""
    local = datetime.now(timezone.utc).astimezone() + timedelta(
        hours=hours, minutes=minutes
    )
    return local.strftime("%Y-%m-%dT%H:%M")


def past_local_at(*, minutes: int = 2) -> str:
    local = datetime.now(timezone.utc).astimezone() - timedelta(minutes=minutes)
    return local.strftime("%Y-%m-%dT%H:%M")


def due_body(at: str) -> dict:
    return {"kind": "once", "at": at}


class CreateReminderApiTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "rem.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _create_real_turn(
        self, client, container, conversation_id: str, *, key: str
    ) -> str:
        """在 `conversation_id` 里真跑一轮，返回它的 **v2 run id**。

        `POST /reminders` 与 `POST /tasks` 同一口径：`turnId` 必须指向该会话
        既有的一轮（真实路径里 `turn_id` 就是那一轮的 v2 run id）。
        """
        handle = await send_message(
            client,
            conversation_id,
            "来源轮次：给这个会话留一轮，供建提醒锚定。",
            idempotency_key=key,
        )
        await wait_for_run_terminal(container, handle["runId"])
        return handle["runId"]

    async def _create_reminder(
        self,
        client,
        *,
        conversation_id: str,
        turn_id: str,
        title: str = TITLE,
        commitment: str = COMMITMENT,
        at: str | None = None,
    ) -> dict:
        response = await client.post(
            "/reminders",
            json={
                "title": title,
                "commitment": commitment,
                "dueAt": due_body(at or future_local_at()),
                "conversationId": conversation_id,
                "turnId": turn_id,
            },
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()["reminder"]

    async def _accept_proposal_reminder(
        self,
        client,
        container,
        *,
        conversation_id: str,
        turn_id: str,
        title: str = TITLE,
        commitment: str = COMMITMENT,
        at: str,
    ) -> dict:
        proposal = container.task_proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            title=title,
            commitment=commitment,
            schedule=due_body(at),
            reason="用例造提案（走既有的提案接受路径）",
        )
        resolved = await client.post(
            f"/task-proposals/{proposal.id}/resolve", json={"decision": "accept"}
        )
        self.assertEqual(200, resolved.status_code, resolved.text)
        return resolved.json()["reminder"]


class CreateReminderApiTest(CreateReminderApiTestBase):
    """① 正常创建：201、恰好 1 行、`due_at` 是归一后的 UTC ISO(ms)+Z。

    能红的方式：
    - 路由把请求里的本地墙钟字符串**直接**交给仓储（正是 R-D3 要防的 bug）→
      落库值不再是 `…Z` 形状、回读本地墙钟也不再等于用户填的那一刻，两条断言红；
    - 路由漏调 `create_reminder`、或写了两处 INSERT → 形状/行数断言红。
    """

    async def test_create_returns_201_one_row_and_normalized_utc_due_at(self) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (
                await create_bound_conversation(client)
            )["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-create"
            )
            at = future_local_at(hours=3)

            reminder = await self._create_reminder(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
                at=at,
            )

            # 响应形状照 `reminders.py` 既有口径（`{"reminder": reminder_json(…)}`）。
            self.assertEqual(TITLE, reminder["title"])
            self.assertEqual(COMMITMENT, reminder["commitment"])
            self.assertEqual("pending", reminder["status"])
            self.assertEqual(conversation_id, reminder["sourceConversationId"])
            self.assertEqual(turn_id, reminder["sourceTurnId"])

            rows = reminder_rows(container.database)
            self.assertEqual(1, len(rows))
            row = rows[reminder["id"]]

            # 落库的是**归一后的 UTC 绝对值**，格式与调度器的 `now_iso` 一致
            # （字符串比较才成立）。
            self.assertTrue(
                UTC_ISO_MS_Z.match(row["due_at"]),
                f"due_at 必须是 UTC ISO(ms)+Z，实际 {row['due_at']!r}",
            )
            self.assertTrue(row["due_at"].endswith("Z"))
            # 独立复核（不重跑实现那行代码）：把落库值按**进程本地时区**回读，
            # 必须逐字等于用户填的本地墙钟——"本地墙钟 → UTC → 本地墙钟"闭环。
            round_trip = (
                datetime.fromisoformat(row["due_at"].replace("Z", "+00:00"))
                .astimezone()
                .strftime("%Y-%m-%dT%H:%M")
            )
            self.assertEqual(at, round_trip)
            # 来源两列如实落库；提案锚点为空（这条不是提案来的）。
            self.assertEqual(conversation_id, row["source_conversation_id"])
            self.assertEqual(turn_id, row["source_turn_id"])
            self.assertIsNone(row["source_proposal_id"])
            self.assertIsNone(row["fired_at"])
            self.assertIsNone(row["cancelled_at"])
            # 没顺手写别的东西。
            self.assertEqual(0, count_rows(container.database, "task_proposals"))
            self.assertEqual(0, count_rows(container.database, "tasks"))

    async def test_unknown_body_field_is_rejected_and_leaves_no_rows(self) -> None:
        """请求体是封闭形状（`extra="forbid"`）：多带一个字段就 400。"""
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            response = await client.post(
                "/reminders",
                json={
                    "title": TITLE,
                    "commitment": COMMITMENT,
                    "dueAt": due_body(future_local_at()),
                    "conversationId": "conv_anchor",
                    "turnId": "turn_anchor",
                    "status": "fired",
                },
            )
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual("invalid_request", response.json()["error"]["code"])
            self.assertEqual(0, count_rows(container.database, "reminders"))

    async def test_past_due_at_is_named_400_and_leaves_no_rows(self) -> None:
        """R-D3 后半：解析器**不**判"已过去"，端点必须自己挡下指名的 400。

        能红的方式：删掉路由里 `reminder_due_has_passed` 那道判定 → 本用例拿到
        201（而不是 400 `reminder_due_in_past`），且库里多出一行；而那一行会被
        `TaskScheduler.scan_once` **立刻**选中（`due_at <= now`）——正是用户
        "设一个过去的提醒"最反直觉的后果。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-past"
            )

            response = await client.post(
                "/reminders",
                json={
                    "title": TITLE,
                    "commitment": COMMITMENT,
                    "dueAt": due_body(past_local_at(minutes=2)),
                    "conversationId": conversation_id,
                    "turnId": turn_id,
                },
            )
            self.assertEqual(400, response.status_code, response.text)
            error = response.json()["error"]
            self.assertEqual("reminder_due_in_past", error["code"])
            self.assertIn("过去", error["message"])
            self.assertEqual(0, count_rows(container.database, "reminders"))

            # 对照：同一形状 + 未来时刻 → 201（证明上面的 400 不是"表坏了"）。
            created = await client.post(
                "/reminders",
                json={
                    "title": TITLE,
                    "commitment": COMMITMENT,
                    "dueAt": due_body(future_local_at()),
                    "conversationId": conversation_id,
                    "turnId": turn_id,
                },
            )
            self.assertEqual(201, created.status_code, created.text)
            self.assertEqual(1, count_rows(container.database, "reminders"))

    async def test_missing_fields_are_named_400_with_zero_rows(self) -> None:
        """② 五个必填缺失各一条指名 400，且**零库行**（不许建一半）。

        能红的方式：删掉信封层任一 `is None → ApiRequestError` 分支（pydantic
        的通用报错会说不出是哪个字段缺）、或把校验挪到写库之后（零行断言红）。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-missing"
            )
            base = {
                "title": TITLE,
                "commitment": COMMITMENT,
                "dueAt": due_body(future_local_at()),
                "conversationId": conversation_id,
                "turnId": turn_id,
            }
            cases = (
                ("缺 title", {"title": None}, "missing_title", "title"),
                ("缺 commitment", {"commitment": None},
                 "missing_commitment", "commitment"),
                ("缺 dueAt", {"dueAt": None}, "missing_due_at", "dueAt"),
                ("缺 conversationId", {"conversationId": None},
                 "missing_source_conversation", "conversationId"),
                ("缺 turnId", {"turnId": None},
                 "missing_source_turn", "turnId"),
            )
            for label, override, code, needle in cases:
                payload = {**base, **override}
                with self.subTest(label=label):
                    response = await client.post("/reminders", json=payload)
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual(code, error["code"])
                    self.assertIn(needle, error["message"])
                    self.assertEqual(0, count_rows(container.database, "reminders"))
                    self.assertEqual(
                        0, count_rows(container.database, "task_proposals")
                    )

            # 对照组：表确实可写（上面的"零行"不是因为表坏了）。
            created = await client.post("/reminders", json=base)
            self.assertEqual(201, created.status_code, created.text)
            self.assertEqual(1, count_rows(container.database, "reminders"))

    async def test_foreign_turn_id_is_named_400_and_leaves_no_rows(self) -> None:
        """② 来源轮次必须**属于该会话**（复用 A1 同一个 `source_turn_mismatch`）。

        能红的方式：删掉来源相容校验 → 能建出"来源轮次属于另一个会话"的提醒
        （`reminders.source_turn_id` 语义被污染，worker 又以
        `source_conversation_id` 当执行会话的环境来源），本用例的 400 断言与
        "零库行"断言全红。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_a = (await create_bound_conversation(client))["id"]
            conversation_b = (await create_bound_conversation(client))["id"]
            turn_a = await self._create_real_turn(
                client, container, conversation_a, key="d1-rem-owner-a"
            )
            turn_b = await self._create_real_turn(
                client, container, conversation_b, key="d1-rem-owner-b"
            )
            self.assertNotEqual(conversation_a, conversation_b)

            payload = {
                "title": TITLE,
                "commitment": COMMITMENT,
                "dueAt": due_body(future_local_at()),
                "conversationId": conversation_a,
            }
            for label, turn_id in (
                ("别的会话的轮次", turn_b),
                ("不存在的轮次", "run_does_not_exist"),
            ):
                with self.subTest(label=label):
                    response = await client.post(
                        "/reminders", json={**payload, "turnId": turn_id}
                    )
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual("source_turn_mismatch", error["code"])
                    self.assertIn("turnId", error["message"])
                    self.assertEqual(0, count_rows(container.database, "reminders"))

            # 对照：同一个会话里的真实轮次 → 201，且来源两列如实落库。
            created = await client.post(
                "/reminders", json={**payload, "turnId": turn_a}
            )
            self.assertEqual(201, created.status_code, created.text)
            rows = reminder_rows(container.database)
            self.assertEqual(1, len(rows))
            row = rows[created.json()["reminder"]["id"]]
            self.assertEqual(conversation_a, row["source_conversation_id"])
            self.assertEqual(turn_a, row["source_turn_id"])

    async def test_invalid_due_at_is_400_with_zero_rows(self) -> None:
        """② 非法 `dueAt` 三态 → 400（`validation_error` + 指名 message）+ 零库行。

        词表只在 domain 一处（`parse_reminder_due`）；本路由不重写、不回落。
        能红的方式：路由不调 `parse_reminder_due`（改成直接落库）→ 三态都会
        201，本用例全红。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-invalid"
            )
            base = {
                "title": TITLE,
                "commitment": COMMITMENT,
                "conversationId": conversation_id,
                "turnId": turn_id,
            }
            cases = (
                ("未知 kind", {"kind": "daily", "at": future_local_at()}, "kind"),
                ("缺 at", {"kind": "once"}, "at"),
                ("at 格式错", {"kind": "once", "at": "明天下午三点"}, "at"),
                (
                    "at 日期不存在",
                    {"kind": "once", "at": "2026-02-30T09:00"},
                    "日期",
                ),
                (
                    "多余键",
                    {"kind": "once", "at": future_local_at(), "time": "09:00"},
                    "不支持的字段",
                ),
            )
            for label, due_at, needle in cases:
                with self.subTest(label=label):
                    response = await client.post(
                        "/reminders", json={**base, "dueAt": due_at}
                    )
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual("validation_error", error["code"])
                    self.assertIn(needle, error["message"])
                    self.assertEqual(0, count_rows(container.database, "reminders"))

    async def test_future_due_is_selectable_only_at_that_absolute_moment(self) -> None:
        """④ 时区锚定：本地墙钟 = 现在 + 2 分钟 → 落库的 UTC 只在**到点**被选中。

        "不是现在、也不是过去"就是这条：`list_due_pending(now)` 必须为空，
        `scan_once(now)` 必须 0，而 `list_due_pending(now + 3min)` 必须含它。

        能红的方式：把本地墙钟直接落库（R-D3 的 bug）→ 在 UTC 偏移为正的时区
        下 `due_at` 会**早于**真实绝对时刻，`list_due_pending(now)` 立刻非空、
        `scan_once(now)` 非 0 → 红；在偏移为负的时区下"到点时选中"断言红。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-anchor"
            )
            at = future_local_at(hours=0, minutes=2)

            reminder = await self._create_reminder(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
                at=at,
            )

            now = datetime.now(timezone.utc)
            # 还没到点：一条都不该被选中（提醒**不**提前触发）。
            self.assertEqual(
                [], list(container.reminder_repository.list_due_pending(utc_iso(now)))
            )
            self.assertEqual(0, await container.task_scheduler.scan_once(now=now))
            # 到点（+3 分钟，越过分钟的边界）：必须被选中。
            later = now + timedelta(minutes=3)
            due_ids = [
                item.id
                for item in container.reminder_repository.list_due_pending(
                    utc_iso(later)
                )
            ]
            self.assertEqual([reminder["id"]], due_ids)


class ReminderDuePastBoundaryTest(unittest.TestCase):
    """R-D3 的边界是**纯函数**：当前分钟不算过去，严格早于当前分钟才算。

    能红的方式：把 `<` 改成 `<=` → "当前分钟"那条立刻红（用户填"现在这一分钟"
    会被拒），或者把截断去掉 → "同分钟秒数更大"那条红。
    """

    def test_current_minute_is_allowed_but_strictly_earlier_is_not(self) -> None:
        now = datetime(2026, 9, 14, 12, 30, 45, tzinfo=timezone.utc)
        # 当前分钟（12:30:00.000）——允许。
        self.assertFalse(
            reminder_due_has_passed("2026-09-14T12:30:00.000Z", now=now)
        )
        # 下一分钟——允许。
        self.assertFalse(
            reminder_due_has_passed("2026-09-14T12:31:00.000Z", now=now)
        )
        # 前一分钟——过去。
        self.assertTrue(
            reminder_due_has_passed("2026-09-14T12:29:00.000Z", now=now)
        )
        # 同一分钟但秒数更小——仍然允许（不比秒）。
        self.assertFalse(
            reminder_due_has_passed("2026-09-14T12:30:00.000Z", now=now)
        )

    def test_non_utc_now_is_normalized_before_comparison(self) -> None:
        shanghai = timezone(timedelta(hours=8))
        now = datetime(2026, 9, 14, 20, 30, 5, tzinfo=shanghai)  # UTC 12:30:05
        self.assertFalse(
            reminder_due_has_passed("2026-09-14T12:30:00.000Z", now=now)
        )
        self.assertTrue(
            reminder_due_has_passed("2026-09-14T12:29:00.000Z", now=now)
        )


class ReminderTimezoneAnchorTest(CreateReminderApiTestBase):
    """④ `TZ=Asia/Shanghai`（+08:00）下的时区锚定——**独立**算出期望的 UTC。

    不重跑实现那行代码：期望值由"本地墙钟 + 固定偏移 +08:00 → UTC"独立算出。
    能红的方式：路由把本地墙钟直接落库 → 落库值比真实绝对时刻**早 8 小时**，
    本用例的逐字相等断言与"到点前不该选中 / 到点后必须选中"断言全红。
    """

    def setUp(self) -> None:
        super().setUp()
        if not hasattr(time, "tzset"):  # pragma: no cover - 非 POSIX 平台
            self.skipTest("本用例需要 POSIX 的 time.tzset()")
        self._original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()
        self.addCleanup(self._restore_tz)

    def _restore_tz(self) -> None:
        if self._original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._original_tz
        time.tzset()

    async def test_local_wall_clock_is_stored_as_utc_and_fires_at_that_instant(
        self,
    ) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-tz"
            )
            # 本地（+08:00）墙钟：现在 +2 分钟。
            local = datetime.now(timezone.utc).astimezone() + timedelta(minutes=2)
            at = local.strftime("%Y-%m-%dT%H:%M")
            # 前提检查：这个进程现在确实把本地时区当成 +08:00（否则本用例
            # 测的就不是"非 UTC 偏移"下的锚定了）。
            self.assertEqual("+0800", local.strftime("%z"))

            reminder = await self._create_reminder(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
                at=at,
            )

            # 独立期望：把墙钟按**已知的 +08:00** 解释，再转 UTC。
            expected_utc = (
                datetime.strptime(at, "%Y-%m-%dT%H:%M")
                .replace(tzinfo=timezone(timedelta(hours=8)))
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            row = reminder_rows(container.database)[reminder["id"]]
            self.assertEqual(expected_utc, row["due_at"])
            # 本地墙钟字符串与落库值**不同**（+08:00 一定差 8 小时）——证明没有
            # 把请求字符串直接交给仓储。
            self.assertNotEqual(at, row["due_at"])
            # 落库值代表的**绝对时刻**就是本地那一分钟（aware datetime 比较跨时区
            # 比的是瞬间，与格式化无关）。
            self.assertEqual(
                local.replace(second=0, microsecond=0),
                datetime.fromisoformat(row["due_at"].replace("Z", "+00:00")),
            )

            # 到点前 1 分钟：不该被选中（过去的错误实现会在这里就非空）。
            just_before = datetime.fromisoformat(
                expected_utc.replace("Z", "+00:00")
            ) - timedelta(minutes=1)
            self.assertEqual(
                [],
                list(
                    container.reminder_repository.list_due_pending(
                        utc_iso(just_before)
                    )
                ),
            )
            # 到点：必须被选中。
            at_moment = datetime.fromisoformat(expected_utc.replace("Z", "+00:00"))
            self.assertEqual(
                [reminder["id"]],
                [
                    item.id
                    for item in container.reminder_repository.list_due_pending(
                        utc_iso(at_moment)
                    )
                ],
            )


class ReminderShapeIdentityTest(CreateReminderApiTestBase):
    """⑤ 本端点写出的行与**提案接受**写出的行**逐列同形**（照 T5 的指纹做法）。

    能红的方式：任一条路自己内联一份 INSERT（列序/列值漂移，例如 `status`
    写成别的字面量、`due_at` 少一次归一）→ 同形断言立刻红；或者本端点绕开
    `insert_reminder_row` 直接落库 → 同样红。
    """

    #: 逐列对照用的稳定列（两条路必须相同）。
    IDENTICAL_COLUMNS = (
        "title",
        "commitment",
        "due_at",
        "status",
        "source_conversation_id",
        "source_turn_id",
        "fired_at",
        "cancelled_at",
    )
    #: **必然**不同、且各自正确的列。
    EXPECTED_DIFFERING_COLUMNS = frozenset(
        {"id", "created_at", "updated_at", "source_proposal_id"}
    )
    #: `insert_reminder_row` 在 INSERT 里**显式**写的 10 列
    #: （`storage/sqlite_reminder_repository.py`，`reminders` 表的唯一写入器）。
    #: 手抄的字面清单——不从被测代码、也不从 `SELECT *` 推导，这正是它能红的原因。
    INSERTED_COLUMNS = frozenset(
        {
            "id",
            "title",
            "commitment",
            "due_at",
            "status",
            "source_conversation_id",
            "source_turn_id",
            "source_proposal_id",
            "created_at",
            "updated_at",
        }
    )
    #: 不在 INSERT 列表里、只在状态迁移（fired / cancelled）时由 UPDATE 写的两个
    #: 可空列（迁移 `023_task_reminders.sql`）。
    STATE_ONLY_COLUMNS = frozenset({"fired_at", "cancelled_at"})
    #: 于是 `SELECT * FROM reminders` 的键集合**恰好**是这两者之和（12 列）。
    TABLE_COLUMNS = INSERTED_COLUMNS | STATE_ONLY_COLUMNS

    async def test_manual_row_is_column_identical_to_accepted_proposal_row(
        self,
    ) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_id = (await create_bound_conversation(client))["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="d1-rem-identical"
            )
            at = future_local_at(hours=5)

            manual = await self._create_reminder(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
                at=at,
            )
            accepted = await self._accept_proposal_reminder(
                client,
                container,
                conversation_id=conversation_id,
                turn_id=turn_id,
                at=at,
            )

            rows = reminder_rows(container.database)
            self.assertEqual(2, len(rows))
            manual_row = rows[manual["id"]]
            accepted_row = rows[accepted["id"]]

            # 显式列清单比对（替代原先的空转断言）。
            # 原先这里写的是 `sorted(manual_row) == sorted(accepted_row)`：两行都来自
            # **同一张表**的 `SELECT *`，键集合按定义相同 ⇒ 那条断言**结构上不可能
            # 红**（检出力为零）。现在两行的键集合各自去对一份**手抄的字面清单**
            # （写入器列面 + 两个状态列），表结构 / 写入器列面一旦漂移就红。
            for label, row in (("manual", manual_row), ("accepted", accepted_row)):
                with self.subTest(row=label):
                    self.assertEqual(self.TABLE_COLUMNS, frozenset(row))
            for column in self.IDENTICAL_COLUMNS:
                with self.subTest(column=column):
                    self.assertEqual(accepted_row[column], manual_row[column])

            # 落库的 due_at 逐字相同：两条路都走 `reminder_due_to_utc_iso`。
            self.assertEqual(
                reminder_due_to_utc_iso(ReminderDue(at=at)), manual_row["due_at"]
            )
            self.assertEqual("pending", manual_row["status"])

            # 必然不同的 4 列，各自都要正确（不是"随便不同"）。
            self.assertIsNone(manual_row["source_proposal_id"])
            self.assertTrue(accepted_row["source_proposal_id"].startswith("taskp_"))
            self.assertNotEqual(manual_row["id"], accepted_row["id"])
            self.assertTrue(manual_row["id"].startswith("rem_"))
            self.assertTrue(accepted_row["id"].startswith("rem_"))
            for column in ("created_at", "updated_at"):
                with self.subTest(column=column):
                    self.assertTrue(manual_row[column])
                    self.assertTrue(accepted_row[column])

            # "除这 4 列外逐字段相同"的单一断言。
            differing = {
                column
                for column in manual_row
                if manual_row[column] != accepted_row[column]
            }
            self.assertLessEqual(
                differing, set(self.EXPECTED_DIFFERING_COLUMNS), differing
            )


class SingleReminderWriterTest(unittest.TestCase):
    """⑥ `reminders` 表全仓只有一个 INSERT 写入器（R-D4 的单点缝）。

    能红的方式：任何人在 `src/endless_task` 里再写一处 `INSERT INTO reminders`
    （例如图省事在路由/提案路径里另开一条落库路径）→ 清单立刻多一项。
    """

    def test_reminders_table_has_exactly_one_insert_site(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "endless_task"
        hits = sorted(
            str(path.relative_to(source_root))
            for path in source_root.rglob("*.py")
            if "INSERT INTO reminders" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(["storage/sqlite_reminder_repository.py"], hits)


if __name__ == "__main__":
    unittest.main()
