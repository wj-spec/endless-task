from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import (
    FakeProvider,
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeRepository


class ContextAuditProvider:
    name = "context-audit"

    def __init__(self) -> None:
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        yield ProviderTextDelta(f"回答{len(self.requests)}")
        yield ProviderCompleted(finish_reason="stop")


class FlakyProvider:
    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request, cancellation_token):
        del request
        cancellation_token.raise_if_cancelled()
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(
                "provider_unavailable",
                "模拟服务暂时不可用。",
                retryable=True,
            )
        yield ProviderTextDelta("重试成功")
        yield ProviderCompleted(finish_reason="stop")


class EchoProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        await asyncio.sleep(0.01)
        current_user = request.messages[-1].content
        yield ProviderTextDelta(f"echo:{current_user}")
        yield ProviderCompleted(finish_reason="stop")


@asynccontextmanager
async def local_client(database_path: Path, provider, **setting_overrides):
    setting_overrides.setdefault("memory_proposals_enabled", False)
    setting_overrides.setdefault("knowledge_proposals_enabled", False)
    app = create_app(
        settings=AppSettings(database_path=database_path, **setting_overrides),
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


class P0ReleaseGateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "release.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @staticmethod
    async def _wait_for_terminal(client: httpx.AsyncClient, turn_id: str):
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            payload = response.json()
            if payload["turnStatus"] in {"completed", "failed", "cancelled"}:
                return payload
            await asyncio.sleep(0.005)
        raise AssertionError("Turn did not reach a terminal state")

    @staticmethod
    async def _submit(
        client: httpx.AsyncClient,
        conversation_id: str,
        ordinal: int,
        content: str,
    ):
        response = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": f"request-{ordinal}"},
            json={"content": content},
        )
        if response.status_code != 202:
            raise AssertionError(response.text)
        return response.json()

    async def test_ten_turns_refresh_and_service_restart_restore_state(self) -> None:
        provider = ContextAuditProvider()
        async with local_client(self.database_path, provider) as (client, app):
            conversation_id = (await client.post("/conversations")).json()["id"]
            for ordinal in range(1, 11):
                created = await self._submit(
                    client,
                    conversation_id,
                    ordinal,
                    f"问题{ordinal}",
                )
                terminal = await self._wait_for_terminal(client, created["turnId"])
                self.assertEqual("completed", terminal["turnStatus"])
                self.assertEqual(f"回答{ordinal}", terminal["content"])

            self.assertEqual(10, len(provider.requests))
            for ordinal, request in enumerate(provider.requests, start=1):
                self.assertEqual(ordinal * 2, len(request.messages))
                self.assertEqual(f"问题{ordinal}", request.messages[-1].content)

            # A fresh browser load uses these two endpoints to hydrate the same state.
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as refreshed_client:
                listed = await refreshed_client.get("/conversations")
                restored = await refreshed_client.get(f"/conversations/{conversation_id}")
            self.assertEqual(conversation_id, listed.json()["items"][0]["id"])
            self.assertEqual(10, len(restored.json()["turns"]))

        async with local_client(self.database_path, ContextAuditProvider()) as (
            restarted_client,
            _,
        ):
            restored = await restarted_client.get(f"/conversations/{conversation_id}")
            self.assertEqual(200, restored.status_code)
            self.assertEqual(10, len(restored.json()["turns"]))
            self.assertEqual("回答10", restored.json()["turns"][-1]["responseVariants"][0]["assistantMessage"]["content"])

    async def test_stop_retry_regenerate_and_select_completed_variant(self) -> None:
        provider = FakeProvider(
            chunks=("已生成部分", "，完整回答"),
            pause_after_chunks=1,
        )
        async with local_client(self.database_path, provider) as (client, _):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await self._submit(client, conversation_id, 1, "请慢慢回答")
            await asyncio.wait_for(provider.paused.wait(), timeout=1)

            cancelled = await client.post(f"/turns/{created['turnId']}/cancel")
            self.assertEqual(200, cancelled.status_code)
            cancelled_snapshot = await self._wait_for_terminal(client, created["turnId"])
            self.assertEqual("cancelled", cancelled_snapshot["turnStatus"])
            self.assertEqual("已生成部分", cancelled_snapshot["content"])

            provider.resume()
            retried = await client.post(
                f"/turns/{created['turnId']}/retry",
                headers={"Idempotency-Key": "retry-1"},
            )
            self.assertEqual(202, retried.status_code)
            retried_payload = retried.json()
            retried_snapshot = await self._wait_for_terminal(client, created["turnId"])
            self.assertEqual("completed", retried_snapshot["turnStatus"])

            regenerated = await client.post(
                f"/turns/{created['turnId']}/regenerate",
                headers={"Idempotency-Key": "regenerate-1"},
            )
            self.assertEqual(202, regenerated.status_code)
            regenerated_payload = regenerated.json()
            await self._wait_for_terminal(client, created["turnId"])
            self.assertNotEqual(
                retried_payload["responseVariantId"],
                regenerated_payload["responseVariantId"],
            )

            selected = await client.post(
                f"/turns/{created['turnId']}/response-variants/"
                f"{retried_payload['responseVariantId']}/select"
            )
            self.assertEqual(200, selected.status_code)
            self.assertEqual(
                retried_payload["responseVariantId"],
                selected.json()["responseVariantId"],
            )

    async def test_provider_failure_is_retryable_through_public_api(self) -> None:
        provider = FlakyProvider()
        async with local_client(self.database_path, provider) as (client, _):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await self._submit(client, conversation_id, 1, "请回答")
            failed = await self._wait_for_terminal(client, created["turnId"])
            self.assertEqual("failed", failed["turnStatus"])
            self.assertEqual("provider_unavailable", failed["error"]["code"])
            self.assertTrue(failed["error"]["retryable"])

            retried = await client.post(
                f"/turns/{created['turnId']}/retry",
                headers={"Idempotency-Key": "retry-after-failure"},
            )
            self.assertEqual(202, retried.status_code)
            completed = await self._wait_for_terminal(client, created["turnId"])
            self.assertEqual("completed", completed["turnStatus"])
            self.assertEqual("重试成功", completed["content"])
            self.assertEqual(2, provider.calls)

    async def test_abnormal_restart_recovers_interrupted_turn(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        chat_repository = SqliteChatRepository(database)
        runtime_repository = SqliteRuntimeRepository(database)
        conversation = chat_repository.create_conversation()
        turn = chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="interrupted-request",
            content="中断前的问题",
        )
        variant_id = turn.turn.active_response_variant_id
        runtime_repository.start_response(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            provider="fake",
            model="fake-model",
        )
        runtime_repository.start_message(turn_id=turn.turn.id, variant_id=variant_id)
        runtime_repository.append_text_delta(
            turn_id=turn.turn.id,
            variant_id=variant_id,
            delta="中断前内容",
            accumulated_content="中断前内容",
        )

        async with local_client(self.database_path, FakeProvider()) as (client, _):
            recovered = await client.get(f"/turns/{turn.turn.id}")

        self.assertEqual("failed", recovered.json()["turnStatus"])
        self.assertEqual("中断前内容", recovered.json()["content"])
        self.assertEqual("runtime_interrupted", recovered.json()["error"]["code"])

    async def test_secret_is_not_persisted_with_chat_data(self) -> None:
        secret = "sk-release-gate-secret-123456"
        async with local_client(
            self.database_path,
            FakeProvider(chunks=("安全回答",)),
            provider_name="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key=secret,
        ) as (client, _):
            conversation_id = (await client.post("/conversations")).json()["id"]
            created = await self._submit(client, conversation_id, 1, "普通消息")
            await self._wait_for_terminal(client, created["turnId"])

        self.assertNotIn(secret.encode(), self.database_path.read_bytes())

    async def test_concurrent_conversations_do_not_mix_events_or_context(self) -> None:
        provider = EchoProvider("echo-a")
        async with local_client(self.database_path, provider) as (client, _):
            first_conversation = (await client.post("/conversations")).json()["id"]
            first_created = await self._submit(
                client,
                first_conversation,
                1,
                "只属于第一会话",
            )
            second_conversation = (await client.post("/conversations")).json()["id"]
            second_created = await self._submit(
                client,
                second_conversation,
                1,
                "只属于第二会话",
            )
            first, second = await asyncio.gather(
                self._wait_for_terminal(client, first_created["turnId"]),
                self._wait_for_terminal(client, second_created["turnId"]),
            )

        self.assertEqual("echo:只属于第一会话", first["content"])
        self.assertEqual("echo:只属于第二会话", second["content"])
        self.assertEqual(
            {"只属于第一会话", "只属于第二会话"},
            {request.messages[-1].content for request in provider.requests},
        )
        for request in provider.requests:
            user_messages = [item.content for item in request.messages if item.role == "user"]
            self.assertEqual(1, len(user_messages))

    async def test_provider_replacement_keeps_the_public_client_contract(self) -> None:
        first_path = Path(self._temporary_directory.name) / "provider-a.db"
        second_path = Path(self._temporary_directory.name) / "provider-b.db"
        payloads = []
        for path, provider in (
            (first_path, EchoProvider("provider-a")),
            (second_path, EchoProvider("provider-b")),
        ):
            async with local_client(path, provider) as (client, _):
                health = (await client.get("/health")).json()
                conversation_id = (await client.post("/conversations")).json()["id"]
                created = await self._submit(client, conversation_id, 1, "相同客户端请求")
                completed = await self._wait_for_terminal(client, created["turnId"])
                payloads.append((health, created, completed))

        first_health, first_created, first_completed = payloads[0]
        second_health, second_created, second_completed = payloads[1]
        self.assertEqual(set(first_health), set(second_health))
        self.assertEqual(set(first_created), set(second_created))
        self.assertEqual(set(first_completed), set(second_completed))
        self.assertNotEqual(first_health["provider"], second_health["provider"])


if __name__ == "__main__":
    unittest.main()
