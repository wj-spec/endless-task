"""切片 10 T5：定时任务可**显式创建**（`POST /tasks`，纯后端）。

为什么有这份用例：目标 (a) 要求"按用户设定的时间/周期无人值守地发起任务……
（定时任务的**创建**/存储/触发/可观测/失败处置）"，而在本任务之前创建只有一条
路——LLM 抽取提案 → `POST /task-proposals/{id}/resolve` 接受。本模块把"显式创建"
这条路径钉住：

- ① `TaskShapeIdentityTest`：手工任务与"提案接受"写出来的 `tasks` 行**逐字段相同**
  （除必然不同的 4 列），且到点后走**同一条**触发/执行路径；
- ② `ExplicitValidationTest`：非法频度/时间/空内容/超长/越界 → 4xx + 指名错误，
  且**不留任何库行**（不许建一半）；
- ③ `SingleTaskWriterTest`：`INSERT INTO tasks` 全仓**只有一处**（路由里没有
  第二份建任务实现）。

每条用例怎么"能红"写在各自 docstring 里。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import Database
from tests.fixtures.v2_client import (
    run_snapshot,
    send_message,
    wait_for_run_terminal,
)
from tests.fixtures.workspace_client import create_bound_conversation

ANSWER = "好的，我按这个周期安排执行；到点后会自动跑，不会提前打扰你。"
TITLE = "每周项目进展总结"
COMMITMENT = "每周一 09:00 总结上周的项目进展"
WEEKLY = {"kind": "weekly", "weekday": 1, "time": "09:00"}
#: 落库字节（不是"语义相同"）：kind/time 在前，weekday 在后。
WEEKLY_JSON = '{"kind":"weekly","time":"09:00","weekday":1}'

#: 逐字段对照用的稳定列（两边必须相同）。
IDENTICAL_COLUMNS = (
    "title",
    "commitment",
    "schedule",
    "status",
    "source_conversation_id",
    "source_turn_id",
    "cancelled_at",
    "resumed_at",
)
#: **必然**不同、且各自正确的列。
EXPECTED_DIFFERING_COLUMNS = frozenset(
    {"id", "created_at", "updated_at", "source_proposal_id"}
)


class StaticProvider:
    """每次请求都回同一段文本：任务 turn 拿到 ANSWER，执行审查拿到非 JSON
    （`TaskRunReviewService._parse` 对非 JSON 如实回落 false），因此本用例对
    provider 调用顺序不敏感。"""

    name = "task-create-api"

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
            # 关掉进程内调度循环：本用例自己用既有探针 `scan_once(now=...)` 驱动，
            # 免得后台 tick 把"到点"次数搅乱。
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


def task_rows(database: Database) -> dict[str, dict]:
    with database.connect() as connection:
        rows = connection.execute("SELECT * FROM tasks").fetchall()
    return {row["id"]: dict(row) for row in rows}


class CreateTaskApiTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self._temporary_directory.name) / "task-create.db"
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _create_real_turn(
        self, client, container, conversation_id: str, *, key: str
    ) -> str:
        """在 `conversation_id` 里真跑一轮，返回它的 **v2 run id**。

        切片 10 残留 A1 起 `POST /tasks` 校验"`turnId` 必须指向该会话既有的一轮"，
        所以本模块不能再编 `turn_anchor` 这类假 id——`tasks.source_turn_id` 在真实
        路径（提案抽取 / 前端 T6）里存的就是该轮的 v2 run id。
        """
        handle = await send_message(
            client,
            conversation_id,
            "来源轮次：给这个会话留一轮，供建任务锚定。",
            idempotency_key=key,
        )
        await wait_for_run_terminal(container, handle["runId"])
        return handle["runId"]

    async def _create_manual_task(
        self, client, *, conversation_id: str, turn_id: str, title: str = TITLE,
        commitment: str = COMMITMENT, schedule: dict = WEEKLY,
    ) -> dict:
        response = await client.post(
            "/tasks",
            json={
                "title": title,
                "commitment": commitment,
                "schedule": schedule,
                "conversationId": conversation_id,
                "turnId": turn_id,
            },
        )
        self.assertEqual(201, response.status_code, response.text)
        return response.json()["task"]

    async def _accept_proposal_task(
        self, client, container, *, conversation_id: str, turn_id: str,
        title: str = TITLE, commitment: str = COMMITMENT, schedule: dict = WEEKLY,
    ) -> dict:
        proposal = container.task_proposal_repository.create_proposal(
            conversation_id=conversation_id,
            turn_id=turn_id,
            title=title,
            commitment=commitment,
            schedule=schedule,
            reason="用例造提案（走既有的提案接受路径）",
        )
        resolved = await client.post(
            f"/task-proposals/{proposal.id}/resolve", json={"decision": "accept"}
        )
        self.assertEqual(200, resolved.status_code, resolved.text)
        return resolved.json()["task"]


class TaskShapeIdentityTest(CreateTaskApiTestBase):
    """① 手工任务与提案任务在库行上逐字段同形，且同一条触发/执行路径。

    能红的方式：
    - 路由自己写一份 INSERT（例如换了列值/列集合）→ 逐字段断言或"差异列集合"
      断言立刻红；
    - `create_task` 与 `accept_proposal` 不再共用 `insert_task_row`（任一边被
      改掉 schedule 序列化/来源列）→ 同形断言红；
    - 路由绕开 `create_task` 直接落库（例如 `source_conversation_id` 写空）→
      到点触发用例的 `childIsExecutionConversation` / 整条 run 指纹红。
    """

    async def test_manual_row_is_field_identical_to_accepted_proposal_row(self) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            conversation_id = conversation["id"]
            # 来源锚点必须是真的：会话里先跑一轮，取它的 v2 run id 当 `turnId`
            # （两边共用同一个锚点，"逐字段相同"才有意义）。
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="a1-anchor-identical"
            )

            manual = await self._create_manual_task(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            accepted_task = await self._accept_proposal_task(
                client,
                container,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )

            rows = task_rows(container.database)
            self.assertEqual(2, len(rows))
            manual_row = rows[manual["id"]]
            accepted_row = rows[accepted_task["id"]]

            # 同一张表 → 列集合必须一致（防止"少写一列"这类形状漂移）。
            self.assertEqual(sorted(manual_row), sorted(accepted_row))

            for column in IDENTICAL_COLUMNS:
                with self.subTest(column=column):
                    self.assertEqual(accepted_row[column], manual_row[column])

            # schedule 比的是**落库字节**，两边都由各自的序列化路径产出：
            # 手工走 `serialize_task_schedule`，提案走建提案时写入的同一形状。
            self.assertEqual(WEEKLY_JSON, manual_row["schedule"])
            self.assertEqual(WEEKLY_JSON, accepted_row["schedule"])
            self.assertEqual("active", manual_row["status"])

            # 必然不同的 4 列，各自都要正确（不是"随便不同"）。
            self.assertIsNone(manual_row["source_proposal_id"])
            self.assertEqual(
                accepted_row["source_proposal_id"], accepted_task["sourceProposalId"]
            )
            self.assertTrue(accepted_row["source_proposal_id"].startswith("taskp_"))
            self.assertNotEqual(manual_row["id"], accepted_row["id"])
            self.assertTrue(manual_row["id"].startswith("task_"))
            self.assertTrue(accepted_row["id"].startswith("task_"))
            for column in ("created_at", "updated_at"):
                with self.subTest(column=column):
                    self.assertTrue(manual_row[column])
                    self.assertTrue(accepted_row[column])

            # "除这 4 列外逐字段相同"的单一断言（用子集，避免同一毫秒建两行时
            # 时间戳恰好相等造成的假红）。
            differing = {
                column
                for column in manual_row
                if manual_row[column] != accepted_row[column]
            }
            self.assertLessEqual(differing, set(EXPECTED_DIFFERING_COLUMNS), differing)

    async def test_manual_task_runs_the_same_trigger_path_as_proposal_task(self) -> None:
        manual_title = "手工建的周报"
        proposal_title = "提案建的周报"
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            conversation_id = conversation["id"]
            # 批次 B Task 1：到点执行落进**该任务自己的执行会话**，不再需要给源会话
            # 补 v1 turn（旧形态走 `create_branch`，parent 必须已有 v1 turn；新形态
            # 按 `(internal_owner_kind, internal_owner_id)` 查/建，与 v1 `turns` 无关）。
            # 两条路各锚一个**真实**轮次（同会话里的两轮；本轮校验只要求"属于该会话"）。
            manual_turn = await self._create_real_turn(
                client, container, conversation_id, key="a1-anchor-manual"
            )
            proposal_turn = await self._create_real_turn(
                client, container, conversation_id, key="a1-anchor-proposal"
            )

            manual = await self._create_manual_task(
                client,
                conversation_id=conversation_id,
                turn_id=manual_turn,
                title=manual_title,
                commitment="每周一 09:00 出一份手工建的周报",
            )
            accepted_task = await self._accept_proposal_task(
                client,
                container,
                conversation_id=conversation_id,
                turn_id=proposal_turn,
                title=proposal_title,
                commitment="每周一 09:00 出一份提案建的周报",
            )

            # 用既有探针把"现在"推到 40 天后：daily/weekly/monthly 一律已到点
            # （最长周期 monthly 的 day ∈ 1–28，40 天必然跨过下一次）。
            started = await container.task_scheduler.scan_once(
                now=datetime.now(timezone.utc) + timedelta(days=40)
            )
            self.assertEqual(2, started)

            manual_fingerprint = await self._trigger_fingerprint(
                client, container, manual["id"], conversation_id, manual_title
            )
            proposal_fingerprint = await self._trigger_fingerprint(
                client, container, accepted_task["id"], conversation_id, proposal_title
            )
            self.assertEqual(proposal_fingerprint, manual_fingerprint)
            self.assertEqual("scheduled", manual_fingerprint["trigger"])
            self.assertEqual("completed", manual_fingerprint["status"])
            # 批次 B Task 1（ADR-1 C 形状）：执行会话是该任务自己的**正式**会话——
            # `kind='normal'`、无父会话（不是源会话下的 EPHEMERAL 子会话）。
            # 指纹里**不含** owner id / 标题（两条路必然一个任务一份，按上面"逐字段
            # 同形"的口径它们本就该不同），逐条各自点名，避免把"必然不同"混进
            # "必须相同"的整条断言里。
            self.assertEqual("normal", manual_fingerprint["childKind"])
            self.assertFalse(manual_fingerprint["childParentIsSource"])
            self.assertIsNone(manual_fingerprint["childParent"])
            self.assertEqual("task_execution", manual_fingerprint["childOwnerKind"])
            self.assertEqual("task_execution", proposal_fingerprint["childOwnerKind"])
            self.assertTrue(manual_fingerprint["childIsExecutionConversation"])
            self.assertTrue(manual_fingerprint["sourceHasNoScheduledEntry"])
            self.assertTrue(manual_fingerprint["submissionIsScheduledPrompt"])
            self.assertTrue(manual_fingerprint["notificationNamesTask"])
            # 各自的执行会话归各自的任务、标题点名各自己的任务。
            self.assertEqual(
                manual["id"],
                self._execution_owner_id(container, manual),
            )
            self.assertEqual(
                accepted_task["id"],
                self._execution_owner_id(container, accepted_task),
            )
            self.assertEqual(
                "《手工建的周报》· 执行记录",
                self._execution_title(container, manual),
            )
            self.assertEqual(
                "《提案建的周报》· 执行记录",
                self._execution_title(container, accepted_task),
            )

    @staticmethod
    def _landing_conversation(container, task: dict):
        """该任务最近一次 run 的落点会话（`link_conversation` 之后的真相）。"""
        runs = container.task_run_repository.list_runs(task_id=task["id"])
        stored = container.task_run_repository.get_run(runs[-1].id)
        return container.chat_repository.get_conversation(stored.conversation_id)

    @staticmethod
    def _execution_owner_id(container, task: dict) -> str:
        landing = TaskShapeIdentityTest._landing_conversation(container, task)
        with container.database.connect() as connection:
            row = connection.execute(
                "SELECT internal_owner_id FROM conversations WHERE id = ?",
                (landing.id,),
            ).fetchone()
        return row["internal_owner_id"]

    @staticmethod
    def _execution_title(container, task: dict) -> str:
        return TaskShapeIdentityTest._landing_conversation(container, task).title

    async def _trigger_fingerprint(
        self, client, container, task_id: str, source_conversation_id: str, title: str
    ) -> dict:
        runs = await self._wait_task_runs(client, task_id)
        self.assertEqual(1, len(runs))
        run = runs[0]
        # `task_runs` 行给出真正执行的那条会话（`started_at` 之后被
        # `link_conversation` 改成该任务**自己的执行会话**，见 ADR-1）。
        stored = container.task_run_repository.get_run(run["id"])
        child = container.chat_repository.get_conversation(stored.conversation_id)
        # 标记列不进产品 payload（`Conversation` dataclass 不动），所以这里直接读库。
        with container.database.connect() as connection:
            marker = connection.execute(
                "SELECT internal_owner_kind FROM conversations WHERE id = ?",
                (stored.conversation_id,),
            ).fetchone()
        snapshot = await run_snapshot(client, stored.conversation_id)
        source_snapshot = await run_snapshot(client, source_conversation_id)
        submissions = [
            entry["data"]["content"]
            for entry in snapshot["entries"]
            if entry.get("actor") == "user"
            and isinstance(entry.get("data", {}).get("content"), str)
        ]
        source_submissions = [
            entry["data"]["content"]
            for entry in source_snapshot["entries"]
            if entry.get("actor") == "user"
            and isinstance(entry.get("data", {}).get("content"), str)
        ]
        notes = (await client.get("/notifications")).json()["items"]
        return {
            "trigger": run["trigger"],
            "status": run["status"],
            "childKind": child.kind.value,
            "childParentIsSource": (
                child.parent_conversation_id == source_conversation_id
            ),
            "childParent": child.parent_conversation_id,
            "childOwnerKind": marker["internal_owner_kind"],
            "childIsNotSource": stored.conversation_id != source_conversation_id,
            "childIsExecutionConversation": (
                stored.conversation_id != source_conversation_id
                and child.parent_conversation_id is None
            ),
            "sourceHasNoScheduledEntry": not any(
                message.startswith("【到点执行】") for message in source_submissions
            ),
            "submissionIsScheduledPrompt": any(
                message.startswith("【到点执行】") for message in submissions
            ),
            "notificationNamesTask": any(
                title in note["title"] and "到点执行" in note["title"]
                for note in notes
            ),
        }

    async def _wait_task_runs(self, client, task_id: str, *, expected: int = 1):
        for _ in range(800):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            if len(items) >= expected and items[0]["status"] != "running":
                return items
            await asyncio.sleep(0.005)
        raise AssertionError(f"Task {task_id} did not finish a run in time")


class ExecutionConversationDeleteCascadeTest(CreateTaskApiTestBase):
    """批次 B Task 1 复审 M-8①：走**真删除端点**的不变量用例。

    仓储级的同类用例（`test_task_worker.py` 的
    `test_deleting_the_execution_conversation_does_not_cancel_ownership`）手工复刻了
    `DELETE /conversations/{id}` 的级联循环——真路由改了它拦不住。这条改成真调端点：
    删除**执行会话**不许取消该任务；而删除**源会话**照旧取消（对照面）。

    红检查：把 `cancel_tasks_for_conversation` 的判据从 `source_conversation_id` 改成
    `task_runs.conversation_id`（或让删除路由对 `list_descendant_ids` 之外多收一次），
    下面"任务仍是 active"就会红。
    """

    async def _wait_for_run(self, client, task_id: str):
        for _ in range(800):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            if items and items[0]["status"] != "running":
                return items
            await asyncio.sleep(0.005)
        raise AssertionError(f"Task {task_id} did not finish a run in time")

    async def test_real_delete_endpoint_of_execution_conversation_keeps_task(
        self,
    ) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation = await create_bound_conversation(client)
            conversation_id = conversation["id"]
            turn_id = await self._create_real_turn(
                client, container, conversation_id, key="m8-anchor"
            )
            # 到点执行一次，拿到该任务自己的执行会话
            task = await self._create_manual_task(
                client,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            started = await container.task_scheduler.scan_once(
                (datetime.now(timezone.utc) + timedelta(days=40))
            )
            self.assertEqual(1, started)
            runs = await self._wait_for_run(client, task["id"])
            execution_id = runs[0]["conversationId"]
            self.assertNotEqual(conversation_id, execution_id)

            # 真调删除端点删掉**执行会话**：任务不许被取消
            deleted = await client.delete(f"/conversations/{execution_id}")
            self.assertEqual(204, deleted.status_code, deleted.text)
            listing = (await client.get("/tasks")).json()["items"]
            self.assertEqual(1, len(listing))
            self.assertEqual("active", listing[0]["status"])
            self.assertEqual(
                "active",
                container.task_repository.get_task(task["id"]).status.value,
            )

            # 对照面：删**源会话**照旧取消该任务（真端点、真级联）
            deleted_source = await client.delete(f"/conversations/{conversation_id}")
            self.assertEqual(204, deleted_source.status_code, deleted_source.text)
            self.assertEqual(
                "cancelled",
                container.task_repository.get_task(task["id"]).status.value,
            )


class ExplicitValidationTest(CreateTaskApiTestBase):
    """② 非法输入必须**显式** 4xx + 指名错误，且不产生任何库行。

    能红的方式：
    - 路由吞掉校验（返回 2xx）或静默回落 → 状态码断言红；
    - 校验发生在写库之后 / 写成多事务（先建行再校验）→ "零行"断言红；
    - 错误信息不再指名（变成通用 400/空 message）→ code/message 断言红。
    """

    async def test_invalid_inputs_are_named_4xx_and_leave_no_rows(self) -> None:
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            # 来源锚点用**真实**会话/真实轮次：这样"排期非法"之类由领域解析器裁决
            # 的用例不会先撞上本轮的来源校验（校验顺序：缺字段 → 来源相容 → 写库）。
            conversation = await create_bound_conversation(client)
            anchor_conversation = conversation["id"]
            anchor_turn = await self._create_real_turn(
                client, container, anchor_conversation, key="a1-invalid-base"
            )
            base = {
                "title": TITLE,
                "commitment": COMMITMENT,
                "schedule": WEEKLY,
                "conversationId": anchor_conversation,
                "turnId": anchor_turn,
            }
            cases = (
                ("非法频度", {"schedule": {"kind": "yearly", "time": "09:00"}},
                 "validation_error", "kind"),
                ("非法时间", {"schedule": {"kind": "daily", "time": "25:00"}},
                 "validation_error", "time"),
                ("空内容", {"commitment": "   \n\t "},
                 "validation_error", "commitment"),
                ("超长标题", {"title": "长" * 201},
                 "validation_error", "200"),
                ("越界 weekday", {"schedule": {"kind": "weekly", "weekday": 9, "time": "09:00"}},
                 "validation_error", "weekday"),
                ("越界 day", {"schedule": {"kind": "monthly", "day": 40, "time": "09:00"}},
                 "validation_error", "day"),
                ("once 不是任务（一次性事项）",
                 {"schedule": {"kind": "once", "at": "2026-08-26T15:00"}},
                 "validation_error", "一次性"),
                ("带时区字段（不许引入 tz 口径）",
                 {"schedule": {"kind": "daily", "time": "09:00", "timezone": "UTC"}},
                 "validation_error", "timezone"),
                ("缺 title", {"title": None}, "missing_title", "title"),
                ("缺 commitment", {"commitment": None},
                 "missing_commitment", "commitment"),
                ("缺 schedule", {"schedule": None}, "missing_schedule", "schedule"),
                ("缺 conversationId", {"conversationId": None},
                 "missing_source_conversation", "conversationId"),
                ("缺 turnId", {"turnId": None}, "missing_source_turn", "turnId"),
            )
            for label, override, code, needle in cases:
                payload = {**base, **override}
                with self.subTest(label=label):
                    response = await client.post("/tasks", json=payload)
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual(code, error["code"])
                    self.assertIn(needle, error["message"])
                    # 不许建一半：任务表与提案表都必须是干净的。
                    self.assertEqual(0, count_rows(container.database, "tasks"))
                    self.assertEqual(
                        0, count_rows(container.database, "task_proposals")
                    )

            # 对照组：表确实可写（上面的"零行"不是因为表坏了）。
            created = await client.post("/tasks", json=base)
            self.assertEqual(201, created.status_code, created.text)
            self.assertEqual(1, count_rows(container.database, "tasks"))
            self.assertEqual(0, count_rows(container.database, "task_proposals"))

    async def test_foreign_turn_id_is_named_400_and_leaves_no_rows(self) -> None:
        """① 来源轮次必须**属于该会话**：指向别的会话 / 不存在的 run → 指名 400。

        起点（切片 10 残留）：`POST /tasks` 只校验两列非空（迁移 016 的 NOT NULL），
        从不校验二者相容 → 直接调 API 能建出"来源轮次属于另一个会话"的任务
        （`tasks.source_turn_id` 语义被污染，worker 又以 `source_conversation_id`
        当作执行子会话的父会话）。校验缺失时本用例的 400 断言（以及"零库行"）全红。

        能红的另一种方式：把校验挪到 `create_task` **之后** → "零库行"断言红。
        """
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            conversation_a = (await create_bound_conversation(client))["id"]
            conversation_b = (await create_bound_conversation(client))["id"]
            turn_a = await self._create_real_turn(
                client, container, conversation_a, key="a1-owner-a"
            )
            turn_b = await self._create_real_turn(
                client, container, conversation_b, key="a1-owner-b"
            )
            self.assertNotEqual(conversation_a, conversation_b)

            payload = {
                "title": TITLE,
                "commitment": COMMITMENT,
                "schedule": WEEKLY,
                "conversationId": conversation_a,
            }
            cases = (
                # 越权：B 会话里真实存在的一轮，被拿去当 A 会话任务的来源。
                ("别的会话的轮次", {**payload, "turnId": turn_b}),
                # 不存在：没有任何行对应。
                ("不存在的轮次", {**payload, "turnId": "run_does_not_exist"}),
            )
            for label, body in cases:
                with self.subTest(label=label):
                    response = await client.post("/tasks", json=body)
                    self.assertEqual(400, response.status_code, response.text)
                    error = response.json()["error"]
                    self.assertEqual("source_turn_mismatch", error["code"])
                    self.assertIn("turnId", error["message"])
                    # 不许建一半：任务表与提案表都必须是干净的。
                    self.assertEqual(0, count_rows(container.database, "tasks"))
                    self.assertEqual(
                        0, count_rows(container.database, "task_proposals")
                    )

            # 对照：同一个会话里的真实轮次 → 201，且来源两列如实落库。
            created = await client.post(
                "/tasks", json={**payload, "turnId": turn_a}
            )
            self.assertEqual(201, created.status_code, created.text)
            rows = task_rows(container.database)
            self.assertEqual(1, len(rows))
            row = rows[created.json()["task"]["id"]]
            self.assertEqual(conversation_a, row["source_conversation_id"])
            self.assertEqual(turn_a, row["source_turn_id"])

    async def test_unknown_body_field_is_rejected(self) -> None:
        """请求体是封闭形状：多带一个字段就 400，不许悄悄忽略。"""
        async with local_client(self.database_path) as (client, app):
            container = app.state.container
            response = await client.post(
                "/tasks",
                json={
                    "title": TITLE,
                    "commitment": COMMITMENT,
                    "schedule": WEEKLY,
                    "conversationId": "conv_anchor",
                    "turnId": "turn_anchor",
                    "nextRunAt": "2026-01-01T00:00:00Z",
                },
            )
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual("invalid_request", response.json()["error"]["code"])
            self.assertEqual(0, count_rows(container.database, "tasks"))


class SingleTaskWriterTest(unittest.TestCase):
    """③ `tasks` 表全仓只有一个 INSERT 写入器 → 路由里没有第二份建任务实现。

    能红的方式：任何人在 `src/endless_task` 里再写一处 `INSERT INTO tasks`
    （例如图省事在路由/服务里另开一条落库路径）→ 清单立刻多一项。
    """

    def test_tasks_table_has_exactly_one_insert_site(self) -> None:
        source_root = Path(__file__).resolve().parents[1] / "src" / "endless_task"
        hits = sorted(
            str(path.relative_to(source_root))
            for path in source_root.rglob("*.py")
            if "INSERT INTO tasks" in path.read_text(encoding="utf-8")
        )
        self.assertEqual(["storage/sqlite_task_repository.py"], hits)
