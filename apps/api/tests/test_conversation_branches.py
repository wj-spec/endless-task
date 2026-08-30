"""P4.5 会话分支与临时会话验收（设计 §9 不变量）。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import (
    ConversationKind,
    FinishReason,
    TaskRunTrigger,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime import ProviderCompleted, ProviderError, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteTaskRepository,
)

RUN_ANSWER = (
    "收到执行请求。本周项目进展如下：完成了任务提案与确认链路，"
    "执行台账也已就位，整体进度符合预期。"
)
EMPTY_TASK_EXTRACTION = json.dumps({"task": None})
EMPTY_MEMORY_EXTRACTION = json.dumps({"proposals": []})
MEMORY_EXTRACTION = json.dumps(
    {
        "proposals": [
            {
                "kind": "preference",
                "content": "用户偏好本地优先的方案。",
                "reason": "用户在对话中明确表达。",
            }
        ]
    },
    ensure_ascii=False,
)


class TextProvider:
    name = "branches"

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


class FailingOnRunProvider(TextProvider):
    """前两次请求（源会话轮 + 任务提取）正常，任务执行轮失败一次。"""

    async def stream(self, request, cancellation_token):
        if len(self.requests) == 2:
            self.requests.append(request)
            raise ProviderError("provider_down", "执行服务不可用", retryable=True)
        async for item in super().stream(request, cancellation_token):
            yield item


@asynccontextmanager
async def local_client(database_path: Path, provider, *, memory_proposals=False):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            runtime="v1",
            memory_proposals_enabled=memory_proposals,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
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
        yield client
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


class ConversationBranchGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "branch.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _wait_for_terminal(self, client: httpx.AsyncClient, turn_id: str):
        for _ in range(400):
            payload = (await client.get(f"/turns/{turn_id}")).json()
            if payload["turnStatus"] in {"completed", "failed", "cancelled"}:
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("Turn did not reach a terminal state")

    async def _send_turn(self, client: httpx.AsyncClient, conversation_id: str, content: str):
        response = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": f"req-{uuid.uuid4().hex}"},
            json={"content": content},
        )
        self.assertEqual(202, response.status_code)
        payload = await self._wait_for_terminal(client, response.json()["turnId"])
        self.assertEqual("completed", payload["turnStatus"])
        return payload

    async def test_branch_creation_listing_and_parent_title(self) -> None:
        provider = TextProvider(["第一回合的回答。", "第二回合的回答。"])
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "第一件事实")
            await self._send_turn(client, parent["id"], "第二件事实")

            response = await client.post(
                f"/conversations/{parent['id']}/branches", json={}
            )
            self.assertEqual(201, response.status_code)
            branch = response.json()["conversation"]
            self.assertEqual("ephemeral", branch["kind"])
            self.assertEqual(parent["id"], branch["parentConversationId"])
            snapshot = (await client.get(f"/conversations/{parent['id']}")).json()
            self.assertEqual(
                snapshot["turns"][-1]["turn"]["id"], branch["forkTurnId"]
            )

            listing = (
                await client.get(f"/conversations/{parent['id']}/branches")
            ).json()
            self.assertEqual([branch["id"]], [item["id"] for item in listing["items"]])

            detail = (await client.get(f"/conversations/{branch['id']}")).json()
            refreshed = (await client.get(f"/conversations/{parent['id']}")).json()
            self.assertEqual(
                refreshed["conversation"]["title"], detail["parentTitle"]
            )

    async def test_branch_snapshot_includes_lineage_turns(self) -> None:
        """分支快照合并谱系历史：继承轮在前、自有轮在后，conversationId 可区分。"""
        provider = TextProvider(["第一轮的回答。", "第二轮的回答。"])
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            first = await self._send_turn(client, parent["id"], "第一轮事实")
            await self._send_turn(client, parent["id"], "第二轮事实")

            response = await client.post(
                f"/conversations/{parent['id']}/branches",
                json={"forkTurnId": first["turnId"]},
            )
            self.assertEqual(201, response.status_code)
            branch = response.json()["conversation"]

            detail = (await client.get(f"/conversations/{branch['id']}")).json()
            self.assertEqual(1, len(detail["turns"]))
            self.assertEqual(first["turnId"], detail["turns"][0]["turn"]["id"])
            self.assertEqual(
                parent["id"], detail["turns"][0]["turn"]["conversationId"]
            )

            await self._send_turn(client, branch["id"], "分支里的新问题")
            detail = (await client.get(f"/conversations/{branch['id']}")).json()
            self.assertEqual(2, len(detail["turns"]))
            self.assertEqual(
                parent["id"], detail["turns"][0]["turn"]["conversationId"]
            )
            self.assertEqual(
                branch["id"], detail["turns"][1]["turn"]["conversationId"]
            )

            # 父会话快照不受影响，仍只有自有两轮。
            parent_detail = (await client.get(f"/conversations/{parent['id']}")).json()
            self.assertEqual(2, len(parent_detail["turns"]))

    async def test_fork_turn_validation(self) -> None:
        provider = TextProvider(["回答甲。", "回答乙。"])
        async with local_client(self.database_path, provider) as client:
            first = (await client.post("/conversations")).json()
            await self._send_turn(client, first["id"], "甲会话的内容")
            second = (await client.post("/conversations")).json()
            self.assertNotEqual(first["id"], second["id"])

            empty = await client.post(f"/conversations/{second['id']}/branches", json={})
            self.assertEqual(409, empty.status_code)

            foreign = await client.post(
                f"/conversations/{second['id']}/branches",
                json={"forkTurnId": "turn_missing"},
            )
            self.assertEqual(404, foreign.status_code)

    async def test_promote_matrix(self) -> None:
        provider = TextProvider(["源会话的回答。"])
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "源会话的一轮")

            normal = await client.post(f"/conversations/{parent['id']}/promote")
            self.assertEqual(409, normal.status_code)

            branch = (
                await client.post(f"/conversations/{parent['id']}/branches", json={})
            ).json()["conversation"]
            promoted = await client.post(f"/conversations/{branch['id']}/promote")
            self.assertEqual(200, promoted.status_code)
            body = promoted.json()["conversation"]
            self.assertEqual("normal", body["kind"])
            self.assertTrue(body["promotedAt"])

            again = await client.post(f"/conversations/{branch['id']}/promote")
            self.assertEqual(409, again.status_code)

    async def test_lineage_context_inherited_and_messages_isolated(self) -> None:
        provider = TextProvider(
            ["第一轮回答：青瓷。", "第二轮回答：建盏。", "分支里的回答。"]
        )
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            first = await self._send_turn(client, parent["id"], "第一件事实：青瓷")
            await self._send_turn(client, parent["id"], "第二件事实：建盏")
            first_turn_id = None
            snapshot = (await client.get(f"/conversations/{parent['id']}")).json()
            first_turn_id = snapshot["turns"][0]["turn"]["id"]

            branch = (
                await client.post(
                    f"/conversations/{parent['id']}/branches",
                    json={"forkTurnId": first_turn_id},
                )
            ).json()["conversation"]
            await self._send_turn(client, branch["id"], "接着深究青瓷")

            branch_request = provider.requests[-1]
            joined = "\n".join(
                message.content for message in branch_request.messages
            )
            self.assertIn("青瓷", joined)
            self.assertNotIn("建盏", joined)

            parent_snapshot = (await client.get(f"/conversations/{parent['id']}")).json()
            self.assertEqual(2, len(parent_snapshot["turns"]))
            branch_snapshot = (await client.get(f"/conversations/{branch['id']}")).json()
            self.assertEqual(2, len(branch_snapshot["turns"]))
            self.assertEqual(
                parent["id"], branch_snapshot["turns"][0]["turn"]["conversationId"]
            )
            self.assertEqual(
                branch["id"], branch_snapshot["turns"][1]["turn"]["conversationId"]
            )

    async def test_ephemeral_skips_memory_proposals_until_promoted(self) -> None:
        provider = TextProvider(
            [
                "源会话的回答。",
                EMPTY_MEMORY_EXTRACTION,
                "升级前的分支回答。",
                "升级后的分支回答。",
                MEMORY_EXTRACTION,
            ]
        )
        async with local_client(
            self.database_path, provider, memory_proposals=True
        ) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "源会话：记住我喜欢本地优先。")
            branch = (
                await client.post(f"/conversations/{parent['id']}/branches", json={})
            ).json()["conversation"]

            await self._send_turn(client, branch["id"], "升级前：记住我喜欢本地优先")
            await asyncio.sleep(0.05)
            pending = (
                await client.get(f"/conversations/{branch['id']}/memory-proposals")
            ).json()["items"]
            self.assertEqual([], pending)

            promoted = await client.post(f"/conversations/{branch['id']}/promote")
            self.assertEqual(200, promoted.status_code)
            await self._send_turn(client, branch["id"], "升级后：记住我喜欢本地优先")
            items = await self._wait_for_proposals(client, branch["id"], 1)
            self.assertEqual(1, len(items))

    async def _wait_for_proposals(self, client, conversation_id: str, count: int):
        for _ in range(400):
            items = (
                await client.get(f"/conversations/{conversation_id}/memory-proposals")
            ).json()["items"]
            if len(items) >= count:
                return items
            await asyncio.sleep(0.005)
        raise AssertionError("Proposals did not appear in time")

    async def test_task_run_lands_in_branch_and_source_untouched(self) -> None:
        provider = TextProvider(
            ["源会话的回答：已经收到你的请求，我会按照约定的节奏持续跟进这件事，有进展会第一时间同步给你。", EMPTY_TASK_EXTRACTION, RUN_ANSWER, EMPTY_TASK_EXTRACTION]
        )
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "请每周总结项目进展")
            while len(provider.requests) < 2:
                await asyncio.sleep(0.005)

            tasks = SqliteTaskRepository(Database(self.database_path))
            task = tasks.create_task(
                title="每周项目进展总结",
                commitment="每周一 09:00 总结上周的项目进展",
                schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
                source_conversation_id=parent["id"],
                source_turn_id="turn_seed",
            )

            response = await client.post(f"/tasks/{task.id}/run")
            self.assertEqual(202, response.status_code)
            run = response.json()["run"]
            self.assertNotEqual(parent["id"], run["conversationId"])

            for _ in range(400):
                items = (await client.get(f"/tasks/{task.id}/runs")).json()["items"]
                if items and items[-1]["status"] == "completed":
                    break
                await asyncio.sleep(0.005)
            else:
                raise AssertionError("Run did not complete")

            parent_snapshot = (await client.get(f"/conversations/{parent['id']}")).json()
            self.assertEqual(1, len(parent_snapshot["turns"]))

            branch_snapshot = (
                await client.get(f"/conversations/{run['conversationId']}")
            ).json()
            branch = branch_snapshot["conversation"]
            self.assertEqual("ephemeral", branch["kind"])
            self.assertEqual(parent["id"], branch["parentConversationId"])
            self.assertIn("《每周项目进展总结》· 执行 @", branch["title"])
            self.assertEqual(2, len(branch_snapshot["turns"]))
            self.assertEqual(
                parent["id"], branch_snapshot["turns"][0]["turn"]["conversationId"]
            )
            user_content = branch_snapshot["turns"][1]["userMessage"]["content"]
            self.assertTrue(user_content.startswith("【手动执行】"))

    async def test_retry_reuses_existing_branch(self) -> None:
        provider = FailingOnRunProvider(
            [
                "源会话的回答：已经收到你的请求，我会按照约定的节奏持续跟进这件事，有进展会第一时间同步给你。",
                EMPTY_TASK_EXTRACTION,
                RUN_ANSWER,
                RUN_ANSWER,
                EMPTY_TASK_EXTRACTION,
            ]
        )
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "请每周总结项目进展")
            while len(provider.requests) < 2:
                await asyncio.sleep(0.005)

            tasks = SqliteTaskRepository(Database(self.database_path))
            task = tasks.create_task(
                title="每周项目进展总结",
                commitment="每周一 09:00 总结上周的项目进展",
                schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
                source_conversation_id=parent["id"],
                source_turn_id="turn_seed",
            )

            first = await client.post(f"/tasks/{task.id}/run")
            self.assertEqual(202, first.status_code)
            first_run_id = first.json()["run"]["id"]
            failed = await self._wait_for_run(client, task.id, "failed")
            self.assertEqual(first_run_id, failed["id"])
            branch_id = failed["conversationId"]
            self.assertNotEqual(parent["id"], branch_id)

            second = await client.post(f"/tasks/{task.id}/run")
            self.assertEqual(202, second.status_code)
            second_run_id = second.json()["run"]["id"]
            completed = await self._wait_for_run(client, task.id, "completed")
            self.assertEqual(second_run_id, completed["id"])
            self.assertEqual(branch_id, completed["conversationId"])

            branches = (
                await client.get(f"/conversations/{parent['id']}/branches")
            ).json()["items"]
            self.assertEqual(1, len(branches))

    async def _wait_for_run(self, client, task_id: str, status: str):
        for _ in range(400):
            items = (await client.get(f"/tasks/{task_id}/runs")).json()["items"]
            for item in reversed(items):
                if item["status"] == status:
                    return item
            await asyncio.sleep(0.005)
        raise AssertionError(f"Run did not reach {status}")

    async def test_parent_delete_cascades_descendants(self) -> None:
        provider = TextProvider(["源会话的回答。", "分支的回答。"])
        async with local_client(self.database_path, provider) as client:
            parent = (await client.post("/conversations")).json()
            await self._send_turn(client, parent["id"], "源会话的一轮")
            branch = (
                await client.post(f"/conversations/{parent['id']}/branches", json={})
            ).json()["conversation"]
            await self._send_turn(client, branch["id"], "分支的一轮")

            tasks = SqliteTaskRepository(Database(self.database_path))
            task = tasks.create_task(
                title="分支里的安排",
                commitment="每周一 09:00 做点什么",
                schedule={"kind": "weekly", "weekday": 1, "time": "09:00"},
                source_conversation_id=branch["id"],
                source_turn_id="turn_seed",
            )

            deleted = await client.delete(f"/conversations/{parent['id']}")
            self.assertEqual(204, deleted.status_code)

            missing = await client.get(f"/conversations/{branch['id']}")
            self.assertEqual(404, missing.status_code)
            listing = (await client.get("/conversations")).json()["items"]
            self.assertEqual([], listing)
            remaining = (await client.get(f"/tasks/{task.id}")).json()["task"]
            self.assertEqual("cancelled", remaining["status"])

    def test_repository_lineage_order_and_depth_limit(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        repository = SqliteChatRepository(database)

        root = repository.create_conversation()

        def finish(snapshot, content: str):
            variant_id = snapshot.response_variants[0].variant.id
            repository.mark_response_running(
                turn_id=snapshot.turn.id, variant_id=variant_id
            )
            return repository.complete_response(
                turn_id=snapshot.turn.id,
                variant_id=variant_id,
                content=content,
                finish_reason=FinishReason.STOP,
            )

        first = finish(
            repository.create_turn(
                conversation_id=root.id, client_request_id="r1", content="第一轮"
            ),
            "第一轮的回答",
        )
        second = finish(
            repository.create_turn(
                conversation_id=root.id, client_request_id="r2", content="第二轮"
            ),
            "第二轮的回答",
        )

        branch = repository.create_branch(parent_conversation_id=root.id)
        self.assertEqual(ConversationKind.EPHEMERAL, branch.kind)
        self.assertEqual(second.turn.id, branch.fork_turn_id)

        lineage = repository.list_lineage_turns(branch.id)
        self.assertEqual(
            (first.turn.id, second.turn.id),
            tuple(item.turn.id for item in lineage),
        )

        mid_branch = repository.create_branch(
            parent_conversation_id=root.id, fork_turn_id=first.turn.id
        )
        mid_lineage = repository.list_lineage_turns(mid_branch.id)
        self.assertEqual((first.turn.id,), tuple(item.turn.id for item in mid_lineage))

        branch_turn = finish(
            repository.create_turn(
                conversation_id=branch.id,
                client_request_id="b1",
                content="分支里的一轮",
            ),
            "分支里的回答",
        )
        grandchild = repository.create_branch(parent_conversation_id=branch.id)
        grand_lineage = repository.list_lineage_turns(grandchild.id)
        self.assertEqual(
            (first.turn.id, second.turn.id, branch_turn.turn.id),
            tuple(item.turn.id for item in grand_lineage),
        )

        self.assertEqual((), tuple(repository.list_lineage_turns(root.id)))

        cursor = grandchild
        for index in range(6):
            finish(
                repository.create_turn(
                    conversation_id=cursor.id,
                    client_request_id=f"depth-{index}",
                    content="再深一层",
                ),
                "再深一层的回答",
            )
            cursor = repository.create_branch(parent_conversation_id=cursor.id)
        with self.assertRaises(InvalidStateError):
            finish(
                repository.create_turn(
                    conversation_id=cursor.id,
                    client_request_id="depth-final",
                    content="最深一层",
                ),
                "最深一层的回答",
            )
            repository.create_branch(parent_conversation_id=cursor.id)


if __name__ == "__main__":
    unittest.main()
