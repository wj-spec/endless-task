"""@ 引用注入的**端到端**用例（批次 C Task 3a 交付物 B）。

钉住三件最容易做错的事：

1. **注入必须发生在提交之前** ⇒ 引用 entry 是 ``run.trigger_entry_id`` 的**祖先**
   （``execution_impl/runner.py:385`` 只读祖先链；提交之后再 append 是静默失效）。
   断言用**与 runner 同一个读面**（``list_entry_context_entries``）**加**真正发给
   provider 的消息序列——不是"entry 存在"的间接推断。
2. **两写不原子**（``Database.transaction()`` 不可嵌套）⇒ 提交失败**不许留孤儿**
   引用 entry（``include_in_llm=True``，会污染之后每一轮的模型上下文）。
3. **不带 references 时行为逐字不变**（惰性）——这是后端半场能单独合入的前提。
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import ConversationStatus
from endless_task.domain.repositories import ConflictError, NotFoundError
from endless_task.mentions.projection import (
    MODE_RECENT,
    MODE_SUMMARY,
    MODE_TOKEN_LIMITS,
)
from endless_task.mentions.service import _artifact_names
from endless_task.runtime.fake_provider import FakeProvider
from endless_task.runtime_v2 import (
    Actor,
    RunStatus,
    TranscriptEntryStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.compaction import (
    RuntimeV2ContextCompactionService,
    history_turns,
)
from tests.fixtures.v2_client import wait_for_run_terminal
from tests.fixtures.workspace_client import create_bound_conversation


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _reference(conversation_id: str, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "conversation",
        "conversationId": conversation_id,
    }
    payload.update(overrides)
    return payload


class MentionInjectionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self, *, full_mode: bool = False, chunks=("回答",)):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "mentions.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                scheduler_enabled=False,
                notifications_enabled=False,
                task_run_review_enabled=False,
                mention_full_mode_enabled=full_mode,
            ),
            provider=FakeProvider(chunks=chunks),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        return client, app.state.container

    # -- helpers ---------------------------------------------------------

    async def _prepare(self, client, container, texts) -> list[str]:
        """在**同一个工作区**里建 N 个会话并**立刻各 seed 一条**。

        立刻 seed 是必须的：``POST /conversations`` 走
        ``create_or_reuse_empty_conversation``，同工作区里还空着的会话会被**复用**
        （不 seed 的话三个"新会话"其实是同一个）。
        """

        first = await create_bound_conversation(client)
        workspace_id = first["workspaceId"]
        ids: list[str] = []
        for index, text in enumerate(texts):
            if index == 0:
                conversation_id = first["id"]
            else:
                created = await client.post(
                    "/conversations", json={"workspaceId": workspace_id}
                )
                self.assertEqual(201, created.status_code, created.text)
                conversation_id = created.json()["id"]
            await self._seed_lane(client, container, conversation_id, text)
            ids.append(conversation_id)
        return ids

    async def _seed_lane(self, client, container, conversation_id: str, text: str) -> str:
        """发一条普通消息并等到终态，返回 lane id（= 会话有了 lane + 一轮历史）。"""

        response = await client.post(
            f"/api/v2/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": _key("seed")},
            json={"content": text},
        )
        self.assertEqual(202, response.status_code, response.text)
        await wait_for_run_terminal(container, response.json()["runId"])
        return self._lane(container, conversation_id).id

    def _lane(self, container, conversation_id: str):
        pointer = container.runtime_v2_repository.get_conversation_pointer(
            conversation_id
        )
        return container.runtime_v2_repository.get_lane(pointer.active_lane_id)

    def _entries(self, container, conversation_id: str):
        return container.runtime_v2_repository.list_lane_context_entries(
            self._lane(container, conversation_id).id
        )

    def _notices(self, container, conversation_id: str):
        return tuple(
            entry
            for entry in self._entries(container, conversation_id)
            if entry.type is TranscriptEntryType.SYSTEM_NOTICE
        )

    # -- ① 机制自证：引用 entry 是 trigger 的祖先 -------------------------

    async def test_reference_notice_is_ancestor_of_run_trigger_entry(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client,
            container,
            ["被引用会话的第一轮问题", "本会话的第一轮问题"],
        )

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("ref")},
            json={
                "content": "把那个会话的结论带过来",
                "references": [_reference(target_id, mode="summary")],
            },
        )
        self.assertEqual(202, response.status_code, response.text)
        payload = response.json()
        await wait_for_run_terminal(container, payload["runId"])

        run = container.runtime_v2_repository.get_run(payload["runId"])
        trigger = run.trigger_entry_id
        # 与 runner 取上下文**同一个**读面（runner.py:385）。
        chain = container.runtime_v2_repository.list_entry_context_entries(trigger)
        chain_ids = [entry.id for entry in chain]

        self.assertEqual(trigger, payload["userMessageId"])
        notices = [
            entry
            for entry in chain
            if entry.type is TranscriptEntryType.SYSTEM_NOTICE
        ]
        self.assertEqual(1, len(notices), "引用块应当是一条 system_notice")
        notice = notices[0]
        # **祖先**而不是"只是存在"：notice 在链上，且在 trigger **之前**（链根在前）。
        self.assertIn(notice.id, chain_ids)
        self.assertLess(chain_ids.index(notice.id), chain_ids.index(trigger))
        # notice 的父节点确实是链上的前一个 entry（成链，不是旁挂）。
        index = chain_ids.index(notice.id)
        self.assertEqual(notice.parent_id, chain_ids[index - 1] if index else None)
        self.assertIsNotNone(notice.parent_id)
        # 投影正文确实来自被引用会话，策略默认会进模型上下文（role="system"）。
        self.assertIn("被引用会话的第一轮问题", notice.payload["content"])
        self.assertTrue(notice.context_policy["include_in_llm"])
        self.assertEqual("full", notice.context_policy["transform"])
        self.assertEqual("trusted", notice.context_policy["trust_level"])
        provenance = notice.context_policy["reference_provenance"]
        self.assertEqual(target_id, provenance["conversationId"])
        self.assertEqual("summary", provenance["mode"])
        self.assertEqual(2, provenance["recentTurns"])
        self.assertFalse(provenance["truncated"])
        self.assertGreater(provenance["projectedCharacters"], 0)
        self.assertEqual("normal", provenance["kind"])
        # 端到端：真的进了发给 provider 的消息序列（system role）。
        provider_messages = container.provider.requests[-1].messages
        self.assertTrue(
            any(
                getattr(message, "role", "") == "system"
                and "<referenced_conversation" in message.content
                for message in provider_messages
            ),
            "注入块没有出现在发给模型的消息里",
        )
        # 响应里带回可审计的 token 预估（前端 chip / picker 用）。
        self.assertEqual(1, len(payload["references"]))
        self.assertGreater(payload["references"][0]["tokenEstimate"], 0)
        self.assertEqual(
            payload["referenceTokenEstimate"],
            payload["references"][0]["tokenEstimate"],
        )

    async def test_two_references_chain_under_the_trigger_entry(self) -> None:
        client, container = await self._client()
        first_id, second_id, sender_id = await self._prepare(
            client, container, ["会话一号", "会话二号", "本会话"]
        )

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("multi")},
            json={
                "content": "两条引用",
                "references": [_reference(first_id), _reference(second_id)],
            },
        )
        self.assertEqual(202, response.status_code, response.text)
        payload = response.json()
        await wait_for_run_terminal(container, payload["runId"])
        notices = self._notices(container, sender_id)
        self.assertEqual(2, len(notices))
        # 顺序成链：第二条的 parent 就是第一条（append_entry 自动推进 lane leaf）。
        self.assertEqual(notices[1].parent_id, notices[0].id)
        run = container.runtime_v2_repository.get_run(payload["runId"])
        chain_ids = [
            entry.id
            for entry in container.runtime_v2_repository.list_entry_context_entries(
                run.trigger_entry_id
            )
        ]
        self.assertLess(chain_ids.index(notices[0].id), chain_ids.index(notices[1].id))
        self.assertLess(
            chain_ids.index(notices[1].id), chain_ids.index(run.trigger_entry_id)
        )

    async def test_reference_notice_does_not_enter_the_system_prefix(self) -> None:
        """注入走 entry（可压缩、可规划），**不进**不可压缩的 system 前缀。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("prefix")},
            json={
                "content": "带引用的一轮",
                "references": [_reference(target_id)],
            },
        )
        self.assertEqual(202, response.status_code, response.text)
        await wait_for_run_terminal(container, response.json()["runId"])
        projected = container.runtime_v2_repository.list_lane_context_entries(
            self._lane(container, sender_id).id
        )
        self.assertTrue(
            any(
                entry.type is TranscriptEntryType.SYSTEM_NOTICE
                for entry in projected
            ),
            "引用块必须是 lane 上的 entry（这样才同时进压缩范围与优先级规划器）",
        )

    # -- ② 两写不原子：提交失败不许留孤儿 ---------------------------------

    async def test_submit_failure_leaves_no_orphan_reference_notice(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )

        lane = self._lane(container, sender_id)
        before_ids = [entry.id for entry in self._entries(container, sender_id)]
        before_leaf = lane.leaf_entry_id
        repository = container.runtime_v2_repository

        gateway = container.runtime_v2_gateway
        original_send = gateway.send

        async def racing_send(cid, content, *, lane_id=None, client_request_id=None):
            # 预检之后、提交之前把会话变忙 ⇒ 提交闸必然拒绝（**真实**失败，不是假异常）。
            repository.create_run(
                conversation_id=sender_id,
                lane_id=lane.id,
                trigger_entry_id=before_leaf,
            )
            return await original_send(
                cid,
                content,
                lane_id=lane_id,
                client_request_id=client_request_id,
            )

        gateway.send = racing_send
        try:
            response = await client.post(
                f"/api/v2/conversations/{sender_id}/messages",
                headers={"Idempotency-Key": _key("busy")},
                json={
                    "content": "这条会失败",
                    "references": [_reference(target_id)],
                },
            )
        finally:
            gateway.send = original_send

        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("active v2 run", response.json()["error"]["message"])
        # 孤儿：补偿是**灭活**（C-1：删除会撞 FK）——notice 下没有并发子节点时
        # lane leaf 会被还原，notice 变成链外的死枝 ⇒ 链上应当**一条都不多**。
        self.assertEqual(
            before_ids, [entry.id for entry in self._entries(container, sender_id)]
        )
        self.assertEqual([], list(self._notices(container, sender_id)))
        # 但"删掉"不是要求，"再也进不了上下文"才是：整条 lane 上任何残留 notice
        # （含链外的）都必须 include_in_llm=False。
        self.assertEqual(
            [],
            [
                entry
                for entry in container.runtime_v2_repository.list_entries(
                    lane.id, include_variants=True
                )
                if entry.type is TranscriptEntryType.SYSTEM_NOTICE
                and entry.context_policy.get("include_in_llm", True)
            ],
        )
        self.assertEqual(
            before_leaf,
            container.runtime_v2_repository.get_lane(lane.id).leaf_entry_id,
        )

    async def test_active_run_precheck_rejects_before_any_write(self) -> None:
        """在飞闸预检：命中即按提交闸同一语义拒绝，**早于 append**。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )

        lane = self._lane(container, sender_id)
        before = [entry.id for entry in self._entries(container, sender_id)]
        container.runtime_v2_repository.create_run(
            conversation_id=sender_id,
            lane_id=lane.id,
            trigger_entry_id=lane.leaf_entry_id,
        )

        import endless_task.api.mentions_support as support

        original_append = support.append_reference_notice
        appended: list[str] = []

        def counting_append(repository, **kwargs):
            appended.append(kwargs["text"])
            return original_append(repository, **kwargs)

        support.append_reference_notice = counting_append
        try:
            response = await client.post(
                f"/api/v2/conversations/{sender_id}/messages",
                headers={"Idempotency-Key": _key("precheck")},
                json={"content": "被预检挡住", "references": [_reference(target_id)]},
            )
        finally:
            support.append_reference_notice = original_append

        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("active v2 run", response.json()["error"]["message"])
        # **早于 append**：一次写入都没发生（不是"写了再补偿"）。
        self.assertEqual([], appended)
        self.assertEqual(
            before, [entry.id for entry in self._entries(container, sender_id)]
        )

    # -- ③ 幂等重放（B4 要求实测） ---------------------------------------

    async def test_idempotency_replay_adds_no_second_reference_notice(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )

        key = _key("replay")
        body = {"content": "重放这条", "references": [_reference(target_id)]}
        url = f"/api/v2/conversations/{sender_id}/messages"
        first = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        self.assertEqual(202, first.status_code, first.text)
        await wait_for_run_terminal(container, first.json()["runId"])
        entries_after_first = [e.id for e in self._entries(container, sender_id)]
        self.assertEqual(1, len(self._notices(container, sender_id)))

        second = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        self.assertEqual(202, second.status_code, second.text)
        # 重放返回**同一个** run / 用户消息（既有幂等语义不变）。
        self.assertEqual(first.json()["runId"], second.json()["runId"])
        self.assertEqual(
            first.json()["userMessageId"], second.json()["userMessageId"]
        )
        # **不多出**引用 entry —— 这正是"没有预检就会留下孤儿"的那条路径。
        self.assertEqual(
            entries_after_first,
            [e.id for e in self._entries(container, sender_id)],
        )
        # 而且是**预检**挡住的（干净路径），不是"写了再补偿"：没有任何告警。
        # 这一条把"预检存在"本身钉住，而不只是钉住最终可观测结果。
        self.assertEqual([], second.json()["referenceWarnings"])

    async def test_partial_append_failure_compensates_what_was_written(self) -> None:
        """append 到一半失败：已写下的那种也必须被补偿掉（不许留半条）。"""

        client, container = await self._client()
        first_id, second_id, sender_id = await self._prepare(
            client, container, ["一号", "二号", "本会话"]
        )
        lane = self._lane(container, sender_id)
        before = [entry.id for entry in self._entries(container, sender_id)]
        before_leaf = lane.leaf_entry_id

        import endless_task.api.mentions_support as support

        original = support.append_reference_notice
        calls = {"count": 0}

        def flaky_append(repository, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                # 用仓储层的领域错误（app 会翻成 409），避免走 500 的
                # ServerErrorMiddleware 再抛出路径。
                raise ConflictError("模拟第二条引用写入失败")
            return original(repository, **kwargs)

        support.append_reference_notice = flaky_append
        try:
            response = await client.post(
                f"/api/v2/conversations/{sender_id}/messages",
                headers={"Idempotency-Key": _key("partial")},
                json={
                    "content": "半途失败",
                    "references": [_reference(first_id), _reference(second_id)],
                },
            )
        finally:
            support.append_reference_notice = original

        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("模拟第二条引用写入失败", response.text)
        # 半条也不许留：链上回到原样（leaf 被还原），且整条 lane 上任何残留 notice
        # 都已被**灭活**（C-1：删除会撞 FK，所以是灭活而不是删行）。
        self.assertEqual(
            before, [entry.id for entry in self._entries(container, sender_id)]
        )
        self.assertEqual(
            [],
            [
                entry
                for entry in container.runtime_v2_repository.list_entries(
                    lane.id, include_variants=True
                )
                if entry.type is TranscriptEntryType.SYSTEM_NOTICE
                and entry.context_policy.get("include_in_llm", True)
            ],
        )
        self.assertEqual(
            before_leaf,
            container.runtime_v2_repository.get_lane(lane.id).leaf_entry_id,
        )

    async def test_replay_race_is_compensated_by_the_ancestor_check(self) -> None:
        """预检被绕过（模拟竞态窗口）时，后置祖先核验必须把孤儿收回去。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )

        key = _key("race")
        body = {"content": "竞态重放", "references": [_reference(target_id)]}
        url = f"/api/v2/conversations/{sender_id}/messages"
        first = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        self.assertEqual(202, first.status_code, first.text)
        await wait_for_run_terminal(container, first.json()["runId"])
        entries_before = [e.id for e in self._entries(container, sender_id)]
        lane = self._lane(container, sender_id)

        import endless_task.api.mentions_support as support

        original = support.message_request_exists
        support.message_request_exists = lambda *args, **kwargs: False
        try:
            second = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        finally:
            support.message_request_exists = original

        self.assertEqual(202, second.status_code, second.text)
        self.assertEqual(first.json()["runId"], second.json()["runId"])
        # 孤儿被后置核验**灭活**（C-1：删除会撞 FK）：notice 下没有并发子节点 ⇒
        # leaf 被还原、链回到原样；本次**新增**的那条 notice 必须再也进不了上下文
        # （首次那条合法 notice 仍是 active，那是应该的）。
        self.assertEqual(
            entries_before, [e.id for e in self._entries(container, sender_id)]
        )
        rows = container.runtime_v2_repository.list_entries(
            lane.id, include_variants=True
        )
        by_id = {entry.id: entry for entry in rows}
        new_ids = [entry_id for entry_id in by_id if entry_id not in entries_before]
        self.assertEqual(1, len(new_ids), "只应多出那条被灭活的孤儿 notice")
        orphan = by_id[new_ids[0]]
        self.assertIs(TranscriptEntryType.SYSTEM_NOTICE, orphan.type)
        self.assertFalse(orphan.context_policy["include_in_llm"])
        self.assertEqual("none", orphan.context_policy["transform"])
        self.assertIn("reference_compensated", orphan.context_policy)
        warnings = second.json()["referenceWarnings"]
        self.assertTrue(warnings)
        self.assertIn("已灭活", warnings[0])

    # -- C-1：notice 下**已有并发子节点**时的两条失败路径 -----------------

    async def test_submit_failure_with_a_concurrent_child_deactivates_the_notice(self) -> None:
        """C-1 路径①（提交失败补偿）：notice 下有并发子节点时，补偿不许抛、
        不许顶替 409，且孤儿**之后任何一轮都不许再进上下文**。

        复审 C-1 的形态：在实现者自己的用例里只加一行 ``append_entry(parent=notice)``
        就把补偿打成 ``sqlite3.IntegrityError``（裸 DELETE 撞 FK）⇒ notice 一条没处理，
        include_in_llm 仍是 True。
        """

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用会话正文标记", "本会话第一轮"]
        )
        lane = self._lane(container, sender_id)
        repository = container.runtime_v2_repository
        gateway = container.runtime_v2_gateway
        original_send = gateway.send
        state: dict[str, str] = {}

        async def racing_send(cid, content, *, lane_id=None, client_request_id=None):
            # 真实并发写入者：挂在我们刚写的 notice **下面**（C-1 的 FK 触发形态）。
            notice_id = repository.get_lane(lane.id).leaf_entry_id
            self.assertIsNotNone(notice_id)
            state["notice"] = notice_id
            repository.append_entry(
                conversation_id=sender_id,
                lane_id=lane.id,
                parent_id=notice_id,
                type=TranscriptEntryType.USER_MESSAGE,
                actor=Actor.USER,
                payload={"content": "并发写入者的消息"},
                context_policy={"include_in_llm": True, "transform": "full"},
            )
            # 预检之后、提交之前把会话变忙 ⇒ 提交闸必然拒绝（真实失败）。
            run = repository.create_run(
                conversation_id=sender_id,
                lane_id=lane.id,
                trigger_entry_id=notice_id,
            )
            state["run"] = run.id
            return await original_send(
                cid,
                content,
                lane_id=lane_id,
                client_request_id=client_request_id,
            )

        gateway.send = racing_send
        try:
            response = await client.post(
                f"/api/v2/conversations/{sender_id}/messages",
                headers={"Idempotency-Key": _key("busy-child")},
                json={
                    "content": "这条会失败",
                    "references": [_reference(target_id)],
                },
            )
        finally:
            gateway.send = original_send

        # ① 返回码没有被补偿顶替：原本要返回的 409 必须还是 409（不是 500）。
        self.assertEqual(409, response.status_code, response.text)
        self.assertIn("active v2 run", response.json()["error"]["message"])

        # ② 孤儿被**灭活**（不是删除：删不掉且会撞 FK），并发写入者的链不许被破坏。
        notices = self._notices(container, sender_id)
        self.assertEqual(1, len(notices), "引用 notice 应该还在（灭活而非删除）")
        orphan = notices[0]
        self.assertEqual(state["notice"], orphan.id)
        self.assertFalse(orphan.context_policy["include_in_llm"])
        self.assertEqual("none", orphan.context_policy["transform"])
        self.assertIn("reference_compensated", orphan.context_policy)
        self.assertNotEqual(
            orphan.id, repository.get_lane(lane.id).leaf_entry_id
        )
        # 该断言不是空断言：这条 notice 的正文里**确实**有被引用会话的正文。
        self.assertIn("被引用会话正文标记", orphan.payload["content"])

        # ③ 让手动 run 终态，再**连发两轮**：真正发给 provider 的消息里都不许有被引用
        #    正文（与 runner 同一个读面：model messages）。两轮是为了钉住"不是只对
        #    紧接着的一轮有效"——灭活是写在行上的策略，每轮投影都吃它。
        repository.update_run_status(state["run"], RunStatus.COMPLETED)
        for round_index in (1, 2):
            follow = await client.post(
                f"/api/v2/conversations/{sender_id}/messages",
                headers={"Idempotency-Key": _key(f"busy-child-after-{round_index}")},
                json={"content": f"之后第 {round_index} 轮"},
            )
            self.assertEqual(202, follow.status_code, follow.text)
            await wait_for_run_terminal(container, follow.json()["runId"])
            messages = container.provider.requests[-1].messages
            self.assertFalse(
                any(
                    "被引用会话正文标记" in message.content
                    for message in messages
                ),
                f"被灭活的孤儿 notice 在第 {round_index} 轮之后仍进了模型上下文",
            )

    async def test_replay_race_with_a_concurrent_child_does_not_replace_the_202(self) -> None:
        """C-1 路径②（后置祖先核验）：notice 下有并发子节点时，核验+补偿不许抛，
        成功的 202（幂等重放）不许变成 500，孤儿之后也不许再进上下文。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用会话正文标记", "本会话第一轮"]
        )
        lane = self._lane(container, sender_id)
        repository = container.runtime_v2_repository

        key = _key("race-child")
        body = {"content": "竞态重放", "references": [_reference(target_id)]}
        url = f"/api/v2/conversations/{sender_id}/messages"
        first = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        self.assertEqual(202, first.status_code, first.text)
        await wait_for_run_terminal(container, first.json()["runId"])
        entries_before = [e.id for e in self._entries(container, sender_id)]
        # 首次（合法注入）之后给被引用会话加一轮，并带一个**只可能来自孤儿 notice** 的
        # 标记：这样"之后一轮的模型消息里有它 = 孤儿真的进了上下文"才不可能是误报
        # （首次那条 notice 里没有这个标记）。
        marker = "只应来自孤儿 notice 的被引用正文标记"
        await self._seed_lane(client, container, target_id, marker)

        import endless_task.api.mentions_support as support

        original_exists = support.message_request_exists
        original_append = support.append_reference_notice

        def append_with_concurrent_child(reference_repository, **kwargs):
            record = original_append(reference_repository, **kwargs)
            # 并发写入者挂在这条 notice 下面（裸 DELETE 会被 FK 拒绝）。
            reference_repository.append_entry(
                conversation_id=kwargs["conversation_id"],
                lane_id=kwargs["lane_id"],
                parent_id=record.id,
                type=TranscriptEntryType.USER_MESSAGE,
                actor=Actor.USER,
                payload={"content": "并发写入者的消息"},
                context_policy={"include_in_llm": True, "transform": "full"},
            )
            return record

        support.message_request_exists = lambda *args, **kwargs: False
        support.append_reference_notice = append_with_concurrent_child
        try:
            second = await client.post(url, headers={"Idempotency-Key": key}, json=body)
        finally:
            support.message_request_exists = original_exists
            support.append_reference_notice = original_append

        # ① 返回码没有被顶替：幂等重放的 202 必须还是 202（不是 500）。
        self.assertEqual(202, second.status_code, second.text)
        self.assertEqual(first.json()["runId"], second.json()["runId"])
        # ② 孤儿被灭活（行还在，但 include_in_llm=False）；并发子节点的链**没被破坏**。
        rows = repository.list_entries(lane.id, include_variants=True)
        by_id = {entry.id: entry for entry in rows}
        new_ids = [entry_id for entry_id in by_id if entry_id not in entries_before]
        self.assertEqual(2, len(new_ids), "并发子节点 + 被灭活的孤儿 notice 各一条")
        new_entries = [by_id[entry_id] for entry_id in new_ids]
        notices = [
            entry
            for entry in new_entries
            if entry.type is TranscriptEntryType.SYSTEM_NOTICE
        ]
        self.assertEqual(1, len(notices))
        orphan = notices[0]
        self.assertFalse(orphan.context_policy["include_in_llm"])
        self.assertEqual("none", orphan.context_policy["transform"])
        self.assertIn("reference_compensated", orphan.context_policy)
        children = [
            entry
            for entry in new_entries
            if entry.type is TranscriptEntryType.USER_MESSAGE
        ]
        self.assertEqual(1, len(children), "并发写入者的 entry 必须还在")
        self.assertEqual(orphan.id, children[0].parent_id)
        self.assertEqual(
            children[0].id, repository.get_lane(lane.id).leaf_entry_id
        )
        warnings = second.json()["referenceWarnings"]
        self.assertTrue(warnings)
        self.assertIn("已灭活", warnings[0])
        self.assertIn(marker, orphan.payload["content"])

        # ③ 之后一轮：**只可能来自孤儿 notice** 的标记不许出现在发给 provider 的
        #    消息里（首次那条合法 notice 里没有它 ⇒ 这条断言是真的在钉孤儿）。
        follow = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("race-child-after")},
            json={"content": "之后一轮"},
        )
        self.assertEqual(202, follow.status_code, follow.text)
        await wait_for_run_terminal(container, follow.json()["runId"])
        messages = container.provider.requests[-1].messages
        self.assertFalse(
            any(marker in message.content for message in messages),
            "被灭活的孤儿 notice 仍然进了模型上下文",
        )

    # -- I-1：注入块的 turns= 必须是**轮次数**，不是 lane entry 计数 ------

    async def test_reference_block_reports_turns_not_lane_entry_count(self) -> None:
        """I-1：3 轮（6 条 entry）的会话必须渲染成 ``turns="3"``。

        复审实测：修复前渲染 ``turns="6"``（喂的是 ``entry_count``）。
        """

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用会话的第 1 轮", "本会话第一轮"]
        )
        for turn in (2, 3):
            await self._seed_lane(client, container, target_id, f"被引用会话的第 {turn} 轮")

        target_lane = self._lane(container, target_id)
        target_entry_count = len(
            container.runtime_v2_repository.list_lane_context_entries(target_lane.id)
        )
        self.assertEqual(6, target_entry_count, "3 轮 = 6 条 entry（每轮 user+assistant）")

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("turns")},
            json={"content": "带引用的一轮", "references": [_reference(target_id)]},
        )
        self.assertEqual(202, response.status_code, response.text)
        await wait_for_run_terminal(container, response.json()["runId"])

        notice = self._notices(container, sender_id)[0]
        first_line = notice.payload["content"].splitlines()[0]
        self.assertIn('turns="3"', first_line)
        self.assertNotIn('turns="6"', first_line)
        # 两个数字在同一条 provenance 里同时给出 ⇒ 上面钉的是"喂的是哪个"。
        provenance = notice.context_policy["reference_provenance"]
        self.assertEqual(6, provenance["referencedLaneEntryCount"])

    # -- B1/B2 校验：指名 4xx 且不写任何 entry ---------------------------

    async def test_illegal_references_are_named_and_write_nothing(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )
        before = [entry.id for entry in self._entries(container, sender_id)]
        # 另一个工作区里的会话（跨区用例）。
        foreign = await create_bound_conversation(client)
        foreign_id = foreign["id"]

        cases = [
            (
                "未知 mode",
                [_reference(target_id, mode="everything")],
                400,
                ("references.0", "mode"),
            ),
            (
                "未知 kind",
                [{"kind": "workspace", "conversationId": target_id}],
                400,
                "references.0",
            ),
            (
                "recentTurns 越界",
                [_reference(target_id, mode="recent", recentTurns=3)],
                400,
                "recentTurns",
            ),
            ("自引用", [_reference(sender_id)], 400, "mention_self_reference"),
            (
                "被引用会话不存在",
                [_reference("conv_missing")],
                404,
                "mention_conversation_not_found",
            ),
            (
                "跨工作区",
                [_reference(foreign_id)],
                400,
                "mention_cross_workspace",
            ),
            (
                "entryIds 非空（一期预留）",
                [_reference(target_id, entryIds=["entry_x"])],
                400,
                "mention_entry_ids_unsupported",
            ),
            (
                "full 档默认禁用",
                [_reference(target_id, mode="full")],
                400,
                "mention_full_mode_disabled",
            ),
        ]
        for label, references, expected_status, expected_token in cases:
            with self.subTest(case=label):
                response = await client.post(
                    f"/api/v2/conversations/{sender_id}/messages",
                    headers={"Idempotency-Key": _key("bad")},
                    json={"content": "非法引用", "references": references},
                )
                self.assertEqual(expected_status, response.status_code, response.text)
                tokens = (
                    expected_token
                    if isinstance(expected_token, tuple)
                    else (expected_token,)
                )
                for token in tokens:
                    self.assertIn(token, response.text)
                self.assertEqual(
                    before,
                    [entry.id for entry in self._entries(container, sender_id)],
                    "非法引用写了 entry",
                )

    async def test_illegal_reference_does_not_change_title_or_write_entries(self) -> None:
        """M-1：校验必须**前置在自动命名之前**——非法引用连侧信道写入都不许发生。

        复审实测：把 ``plan_injection`` 挪到命名之后（变异 X3），请求照样 400，
        但**标题被改了**（真正的侧信道写入）。所以这里断言"没变"，不是只断言状态码。
        """

        client, container = await self._client()
        target_id, _ = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )
        workspace_id = (
            await client.get(f"/conversations/{target_id}")
        ).json()["conversation"]["workspaceId"]
        fresh = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        self.assertEqual(201, fresh.status_code, fresh.text)
        sender_id = fresh.json()["id"]
        repository = container.runtime_v2_repository
        lane = repository.create_lane(conversation_id=sender_id)
        repository.set_conversation_pointer(
            conversation_id=sender_id, active_lane_id=lane.id
        )
        # 这个会话还没有用户消息 ⇒ 命名钩子**会**动它（前提成立，断言才有意义）。
        self.assertFalse(repository.has_user_message(sender_id))
        self.assertEqual(
            "新对话", container.chat_repository.get_conversation(sender_id).title
        )

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("illegal-nowrite")},
            json={
                "content": "这条会因非法引用被拒",
                "references": [_reference("conv_missing")],
            },
        )
        self.assertEqual(404, response.status_code, response.text)
        self.assertIn("mention_conversation_not_found", response.text)
        # 零字段变更：标题没被侧信道改掉（校验确实在前）。
        self.assertEqual(
            "新对话", container.chat_repository.get_conversation(sender_id).title
        )
        # 零新增 entry：lane 仍是空的、leaf 未动。
        self.assertEqual([], list(repository.list_lane_context_entries(lane.id)))
        self.assertIsNone(repository.get_lane(lane.id).leaf_entry_id)

    async def test_default_reference_mode_is_summary_with_its_budget(self) -> None:
        """M-2：协议的**默认档**是 ``summary``，且预算就是 summary 档的上限。

        复审变异 X6b（默认改 "recent"）**全绿** ⇒ 每条引用的上下文预算悄悄翻约 3 倍。
        """

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )
        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={"references": [_reference(target_id)]},  # 故意不带 mode
        )
        self.assertEqual(200, response.status_code, response.text)
        item = response.json()["items"][0]
        self.assertEqual(MODE_SUMMARY, item["mode"], "默认档必须是 summary")
        self.assertEqual(2, item["recentTurns"])
        self.assertEqual(MODE_TOKEN_LIMITS[MODE_SUMMARY], item["tokenBudget"])
        self.assertLessEqual(item["tokenEstimate"], MODE_TOKEN_LIMITS[MODE_SUMMARY])
        # 默认档与 recent 档的上限必须真的不同（否则上面那条断言钉不住预算）。
        self.assertLess(MODE_TOKEN_LIMITS[MODE_SUMMARY], MODE_TOKEN_LIMITS[MODE_RECENT])

    async def test_archived_referenced_conversation_is_unreadable(self) -> None:
        """M-3：引用**已归档**会话 ⇒ 404 ``mention_conversation_unreadable``（不静默注入）。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )
        container.chat_repository.set_conversation_status(
            target_id, ConversationStatus.ARCHIVED
        )
        self.assertIs(
            ConversationStatus.ARCHIVED,
            container.chat_repository.get_conversation(target_id).status,
        )
        before = [entry.id for entry in self._entries(container, sender_id)]

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("archived")},
            json={"content": "引用归档会话", "references": [_reference(target_id)]},
        )
        self.assertEqual(404, response.status_code, response.text)
        self.assertIn("mention_conversation_unreadable", response.text)
        self.assertEqual(
            before, [entry.id for entry in self._entries(container, sender_id)]
        )

    async def test_empty_entry_ids_is_accepted(self) -> None:
        """空 ``entryIds`` 是合法的（一期预留字段）。"""

        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话第一轮"]
        )
        response = await client.post(
            f"/api/v2/conversations/{sender_id}/messages",
            headers={"Idempotency-Key": _key("emptyids")},
            json={
                "content": "空 entryIds",
                "references": [_reference(target_id, entryIds=[])],
            },
        )
        self.assertEqual(202, response.status_code, response.text)

    async def test_reference_in_a_fresh_conversation_is_named_not_silent(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话"]
        )
        workspace_id = (
            await client.get(f"/conversations/{target_id}")
        ).json()["conversation"]["workspaceId"]
        fresh = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        fresh_id = fresh.json()["id"]
        response = await client.post(
            f"/api/v2/conversations/{fresh_id}/messages",
            headers={"Idempotency-Key": _key("fresh")},
            json={"content": "第一条就引用", "references": [_reference(target_id)]},
        )
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("mention_no_lane_to_anchor", response.text)
        # 没有偷偷建 lane / pointer。
        self.assertIsNone(
            container.runtime_v2_repository.get_conversation_pointer(fresh_id)
        )

    # -- 惰性：不带 references 行为逐字不变 ------------------------------

    async def test_message_without_references_writes_no_notice(self) -> None:
        client, container = await self._client()
        (conversation_id,) = await self._prepare(
            client, container, ["普通第一轮"]
        )
        before = {entry.id for entry in self._entries(container, conversation_id)}

        response = await client.post(
            f"/api/v2/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": _key("plain")},
            json={"content": "普通第二轮"},
        )
        self.assertEqual(202, response.status_code, response.text)
        payload = response.json()
        # 响应体不加新键（老客户端逐字不变）。
        self.assertEqual(
            {
                "conversationId",
                "laneId",
                "runId",
                "userMessageId",
                "eventsUrl",
                "requestedSkills",
            },
            set(payload),
        )
        await wait_for_run_terminal(container, payload["runId"])
        entries = self._entries(container, conversation_id)
        self.assertEqual([], list(self._notices(container, conversation_id)))
        new_ids = [entry.id for entry in entries if entry.id not in before]
        self.assertEqual(2, len(new_ids), "只应多出用户消息与助手回答各一条")

    # -- 预算与预览 ------------------------------------------------------

    async def test_three_references_are_warned_about(self) -> None:
        client, container = await self._client()
        target_ids = await self._prepare(
            client,
            container,
            ["被引用会话 0", "被引用会话 1", "被引用会话 2", "本会话"],
        )
        sender = target_ids.pop()

        response = await client.post(
            f"/api/v2/conversations/{sender}/messages",
            headers={"Idempotency-Key": _key("three")},
            json={
                "content": "三条引用",
                "references": [_reference(target_id) for target_id in target_ids],
            },
        )
        self.assertEqual(202, response.status_code, response.text)
        payload = response.json()
        self.assertTrue(
            any("超过默认的 2 个" in warning for warning in payload["referenceWarnings"])
        )
        self.assertLessEqual(payload["referenceTokenEstimate"], 4096)

    async def test_total_token_budget_clamps_and_skips_exhausted_references(self) -> None:
        """单轮总量 ≈4K：按顺序分配，用尽之后的引用**不注入但告警**（不许静默丢）。"""

        client, container = await self._client(chunks=("回" * 800,))
        target_ids = await self._prepare(
            client, container, [f"被引用会话 {index}" for index in range(3)]
        )
        long_text = "问" * 900
        for target_id in target_ids:
            for turn in range(10):
                await self._seed_lane(
                    client, container, target_id, f"{long_text}-{turn}"
                )
        sender = await client.post(
            "/conversations",
            json={
                "workspaceId": (
                    await client.get(f"/conversations/{target_ids[0]}")
                ).json()["conversation"]["workspaceId"]
            },
        )
        sender_id = sender.json()["id"]
        await self._seed_lane(client, container, sender_id, "本会话")

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={
                "references": [
                    _reference(target_id, mode="recent", recentTurns=10)
                    for target_id in target_ids
                ]
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertLessEqual(payload["totalTokenEstimate"], 4096)
        self.assertTrue(
            any(
                "已用尽" in warning and "未被注入" in warning
                for warning in payload["warnings"]
            ),
            payload["warnings"],
        )
        # 被截断的那些必须如实标出来。
        truncated = [item for item in payload["items"] if item["truncated"]]
        self.assertTrue(truncated)

    async def test_reference_preview_returns_projection_without_writing(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用会话的问题", "本会话第一轮"]
        )
        before = [entry.id for entry in self._entries(container, sender_id)]

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={
                "references": [
                    _reference(target_id, mode="recent", recentTurns=5)
                ]
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        payload = response.json()
        self.assertEqual(1, len(payload["items"]))
        item = payload["items"][0]
        self.assertIn("<referenced_conversation", item["text"])
        self.assertGreater(item["tokenEstimate"], 0)
        self.assertEqual(payload["totalTokenEstimate"], item["tokenEstimate"])
        self.assertEqual("recent", item["mode"])
        self.assertEqual(5, item["recentTurns"])
        self.assertFalse(item["truncated"])
        # 预览零写入。
        self.assertEqual(
            before, [entry.id for entry in self._entries(container, sender_id)]
        )

    async def test_preview_rejects_illegal_reference_the_same_way(self) -> None:
        client, container = await self._client()
        target_id, sender_id = await self._prepare(
            client, container, ["被引用内容", "本会话"]
        )
        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={"references": [_reference(target_id, mode="full")]},
        )
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("mention_full_mode_disabled", response.text)

    async def test_full_mode_preview_works_when_enabled(self) -> None:
        client, container = await self._client(full_mode=True)
        target_id, sender_id = await self._prepare(
            client, container, ["被引用会话的问题", "本会话"]
        )
        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={"references": [_reference(target_id, mode="full")]},
        )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("full", response.json()["items"][0]["mode"])

    # -- 临时对话：来源与文案义务（B5 / 02 §5.3） ------------------------

    async def test_ephemeral_reference_carries_origin_and_memory_notice(self) -> None:
        client, container = await self._client()
        source_id, sender_id = await self._prepare(
            client, container, ["源会话的问题", "本会话第一轮"]
        )
        created = await client.post(
            f"/api/v2/conversations/{source_id}/temporary-conversations",
            json={"title": "临时探索"},
        )
        self.assertEqual(201, created.status_code, created.text)
        ephemeral_id = created.json()["conversation"]["id"]

        response = await client.post(
            f"/api/v2/conversations/{sender_id}/references/preview",
            json={"references": [_reference(ephemeral_id)]},
        )
        self.assertEqual(200, response.status_code, response.text)
        item = response.json()["items"][0]
        self.assertEqual("ephemeral", item["kind"])
        self.assertIn("临时 · 源自《", item["origin"])
        self.assertIn("显式引用后其内容可能被确认为长期记忆", item["text"])


class ArtifactNameFailureVisibilityTest(unittest.TestCase):
    """M-5：``_artifact_names`` 只吞**预期内**的"没有产物"，其余必须上抛（不许静默成"（无）"）。"""

    class _Repository:
        def __init__(self, error: Exception) -> None:
            self._error = error

        def list_artifacts_for_conversation(self, conversation_id: str):
            raise self._error

    def _container(self, error: Exception):
        return SimpleNamespace(
            artifact_repository=ArtifactNameFailureVisibilityTest._Repository(error)
        )

    def test_expected_missing_artifacts_render_as_empty(self) -> None:
        self.assertEqual((), _artifact_names(self._container(NotFoundError("没有产物")), "conv_x"))

    def test_unexpected_failure_is_not_swallowed(self) -> None:
        with self.assertRaises(RuntimeError):
            _artifact_names(self._container(RuntimeError("产物仓储坏了")), "conv_x")


class MentionTurnSelectionReuseTest(unittest.TestCase):
    """轮次选取**只有一份实现**：压缩器与 @ 引用共用 ``history_turns``。"""

    def _entry(
        self,
        entry_id: str,
        entry_type: TranscriptEntryType,
        *,
        status: TranscriptEntryStatus = TranscriptEntryStatus.FINAL,
        source_run_id=None,
        content: str = "",
    ):
        from endless_task.runtime_v2.domain import TranscriptEntryRecord

        return TranscriptEntryRecord(
            id=entry_id,
            conversation_id="conv",
            parent_id=None,
            lane_id="lane",
            seq=0,
            type=entry_type,
            type_version=1,
            actor=Actor.USER,
            status=status,
            created_at="2026-09-15T00:00:00Z",
            updated_at="2026-09-15T00:00:00Z",
            payload={"content": content},
            context_policy={},
            display={},
            source_run_id=source_run_id,
        )

    def _entries(self):
        return (
            self._entry("u1", TranscriptEntryType.USER_MESSAGE, content="问题一"),
            self._entry(
                "a1",
                TranscriptEntryType.ASSISTANT_MESSAGE,
                source_run_id="run_1",
                content="回答一",
            ),
            self._entry("u2", TranscriptEntryType.USER_MESSAGE, content="问题二"),
            self._entry(
                "a2",
                TranscriptEntryType.ASSISTANT_MESSAGE,
                status=TranscriptEntryStatus.STREAMING,
                source_run_id="run_2",
                content="回答二（未完成）",
            ),
            self._entry("u3", TranscriptEntryType.USER_MESSAGE, content="问题三"),
            self._entry(
                "a3",
                TranscriptEntryType.ASSISTANT_MESSAGE,
                source_run_id="run_2",
                content="回答三",
            ),
            self._entry(
                "a4",
                TranscriptEntryType.ASSISTANT_MESSAGE,
                source_run_id="run_3",
                content="回答四（本轮）",
            ),
        )

    def test_module_function_matches_the_compaction_method(self) -> None:
        """抽取后的模块级函数与压缩器方法**逐字同义**（含 skip_ids 与边界）。"""

        entries = self._entries()
        service = RuntimeV2ContextCompactionService(
            repository=None,  # 只调用纯函数，不碰仓储
            max_context_tokens=131_000,
        )
        for skip_ids in (set(), {"u1"}):
            with self.subTest(skip_ids=skip_ids):
                self.assertEqual(
                    [
                        (turn.user_content, turn.assistant_content)
                        for turn in history_turns(
                            entries, boundary_run_id="run_3", skip_ids=skip_ids
                        )
                    ],
                    [
                        (turn.user_content, turn.assistant_content)
                        for turn in service._history_turns(
                            entries, run_id="run_3", skip_ids=skip_ids
                        )
                    ],
                )

    def test_no_boundary_returns_every_complete_turn(self) -> None:
        turns = history_turns(self._entries(), boundary_run_id=None)
        self.assertEqual(
            [("问题一", "回答一"), ("问题三", "回答三")],
            [(turn.user_content, turn.assistant_content) for turn in turns],
        )

    def test_incomplete_tail_turn_is_not_a_turn(self) -> None:
        # u2 后面跟的是 streaming 的 a2（不成轮），随后 u3 重新开始 ⇒ 没有"半轮"。
        turns = history_turns(self._entries(), boundary_run_id=None)
        self.assertNotIn(
            "回答二（未完成）", [turn.assistant_content for turn in turns]
        )

    def test_boundary_run_stops_the_walk(self) -> None:
        # run_1 边界：a1 属于 run_1 ⇒ 走到 a1 就停，前面的 u1 不成轮。
        turns = history_turns(self._entries(), boundary_run_id="run_1")
        self.assertEqual([], [(turn.user_content, turn.assistant_content) for turn in turns])


if __name__ == "__main__":
    unittest.main()
