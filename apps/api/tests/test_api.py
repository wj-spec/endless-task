from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider, ProviderCompleted, ProviderTextDelta


class DelayedProvider:
    name = "delayed"

    async def stream(self, request, cancellation_token):
        del request
        await asyncio.sleep(0.04)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta("稍后回答")
        yield ProviderCompleted(finish_reason="stop")


class LocalApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self._clients: list[tuple[httpx.AsyncClient, object]] = []

    async def asyncTearDown(self) -> None:
        for client, lifespan in reversed(self._clients):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._temporary_directory.cleanup()

    async def _client(
        self,
        provider=None,
        *,
        heartbeat_seconds: float = 0.01,
        max_message_characters: int = 100_000,
    ) -> httpx.AsyncClient:
        suffix = len(self._clients)
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / f"api-{suffix}.db",
                heartbeat_seconds=heartbeat_seconds,
                max_message_characters=max_message_characters,
            ),
            provider=provider or FakeProvider(chunks=("你好", "，本地 API。")),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self._clients.append((client, lifespan))
        return client

    @staticmethod
    async def _wait_for_terminal(
        client: httpx.AsyncClient,
        turn_id: str,
    ) -> dict[str, object]:
        for _ in range(200):
            response = await client.get(f"/turns/{turn_id}")
            if response.json()["turnStatus"] in {"completed", "failed", "cancelled"}:
                return response.json()
            await asyncio.sleep(0.01)
        raise AssertionError("Turn did not reach a terminal state")

    @staticmethod
    async def _create_turn(
        client: httpx.AsyncClient,
        conversation_id: str,
        key: str = "request-1",
    ) -> httpx.Response:
        return await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": key},
            json={"content": "你好"},
        )

    async def test_empty_conversation_is_reused_until_first_turn(self) -> None:
        client = await self._client()
        first = await client.post("/conversations")
        second = await client.post("/conversations")

        self.assertEqual(201, first.status_code)
        self.assertEqual(first.json()["id"], second.json()["id"])

        turn = await self._create_turn(client, first.json()["id"])
        self.assertEqual(202, turn.status_code)
        await self._wait_for_terminal(client, turn.json()["turnId"])
        third = await client.post("/conversations")
        self.assertNotEqual(first.json()["id"], third.json()["id"])

    async def test_turn_command_snapshot_and_sse_replay(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)
        self.assertEqual(202, created.status_code)
        payload = created.json()

        snapshot = await self._wait_for_terminal(client, payload["turnId"])
        self.assertEqual("completed", snapshot["turnStatus"])
        self.assertEqual("你好，本地 API。", snapshot["content"])

        replay = await client.get(payload["eventsUrl"])
        self.assertEqual(200, replay.status_code)
        self.assertTrue(replay.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: turn.started", replay.text)
        self.assertIn("event: turn.completed", replay.text)

        after_two = await client.get(
            payload["eventsUrl"],
            headers={"Last-Event-ID": f"{payload['turnId']}:2"},
        )
        self.assertNotIn(f"id: {payload['turnId']}:1\n", after_two.text)
        self.assertNotIn(f"id: {payload['turnId']}:2\n", after_two.text)
        self.assertIn(f"id: {payload['turnId']}:3\n", after_two.text)

        envelopes = [
            json.loads(line.removeprefix("data: "))
            for line in replay.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(
            list(range(1, len(envelopes) + 1)),
            [event["sequence"] for event in envelopes],
        )

    async def test_sse_emits_heartbeat_while_waiting(self) -> None:
        client = await self._client(DelayedProvider(), heartbeat_seconds=0.005)
        conversation_id = (await client.post("/conversations")).json()["id"]
        created = await self._create_turn(client, conversation_id)

        stream = await client.get(created.json()["eventsUrl"])

        self.assertIn(": heartbeat\n\n", stream.text)
        self.assertIn("event: turn.completed", stream.text)

    async def test_idempotent_create_and_regenerate_return_original_ids(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]
        first = await self._create_turn(client, conversation_id)
        first_payload = first.json()
        await self._wait_for_terminal(client, first_payload["turnId"])

        duplicate = await self._create_turn(client, conversation_id)
        self.assertEqual(first_payload, duplicate.json())

        regenerated = await client.post(
            f"/turns/{first_payload['turnId']}/regenerate",
            headers={"Idempotency-Key": "regenerate-1"},
        )
        self.assertEqual(202, regenerated.status_code)
        regenerated_payload = regenerated.json()
        await self._wait_for_terminal(client, first_payload["turnId"])

        duplicate_regenerate = await client.post(
            f"/turns/{first_payload['turnId']}/regenerate",
            headers={"Idempotency-Key": "regenerate-1"},
        )
        self.assertEqual(regenerated_payload, duplicate_regenerate.json())
        self.assertNotEqual(
            first_payload["responseVariantId"],
            regenerated_payload["responseVariantId"],
        )

        selected = await client.post(
            f"/turns/{first_payload['turnId']}/response-variants/"
            f"{first_payload['responseVariantId']}/select"
        )
        self.assertEqual(200, selected.status_code)
        self.assertEqual(
            first_payload["responseVariantId"],
            selected.json()["responseVariantId"],
        )

    async def test_errors_use_stable_envelope(self) -> None:
        client = await self._client()
        conversation_id = (await client.post("/conversations")).json()["id"]

        missing_key = await client.post(
            f"/conversations/{conversation_id}/turns",
            json={"content": "你好"},
        )
        self.assertEqual(400, missing_key.status_code)
        self.assertEqual("invalid_request", missing_key.json()["error"]["code"])
        self.assertIn("correlationId", missing_key.json()["error"])

        missing = await client.get("/turns/turn_missing")
        self.assertEqual(404, missing.status_code)
        self.assertEqual("not_found", missing.json()["error"]["code"])

        invalid_cursor = await client.get(
            "/turns/turn_missing/events",
            headers={"Last-Event-ID": "another:2"},
        )
        self.assertEqual(404, invalid_cursor.status_code)

        created = await self._create_turn(client, conversation_id)
        invalid_cursor = await client.get(
            created.json()["eventsUrl"],
            headers={"Last-Event-ID": "another:2"},
        )
        self.assertEqual(400, invalid_cursor.status_code)
        self.assertEqual(
            "invalid_last_event_id",
            invalid_cursor.json()["error"]["code"],
        )

    async def test_untrusted_host_and_oversized_message_are_rejected(self) -> None:
        client = await self._client(max_message_characters=4)
        untrusted = await client.get("/health", headers={"Host": "attacker.example"})
        self.assertEqual(400, untrusted.status_code)

        conversation_id = (await client.post("/conversations")).json()["id"]
        oversized = await client.post(
            f"/conversations/{conversation_id}/turns",
            headers={"Idempotency-Key": "oversized"},
            json={"content": "12345"},
        )
        self.assertEqual(413, oversized.status_code)
        self.assertEqual("message_too_large", oversized.json()["error"]["code"])
