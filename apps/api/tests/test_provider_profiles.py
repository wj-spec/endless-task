from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.storage import Database
from endless_task.storage.sqlite_provider_profile_repository import (
    ProviderProfileDraft,
    SqliteProviderProfileRepository,
)


class ProviderProfileRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "providers.db")
        self.database.initialize()
        self.repository = SqliteProviderProfileRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_builtin_profile_and_default_selection(self) -> None:
        builtin = self.repository.ensure_builtin_profile(
            name="fake",
            default_model="fake-model",
            base_url="",
            api_key_ref="",
            timeout_seconds=60,
        )
        self.assertTrue(builtin.is_builtin)
        self.assertEqual(
            "provider_environment", self.repository.get_default_profile_id()
        )

        created = self.repository.create_profile(
            ProviderProfileDraft(
                name="Local",
                default_model="qwen3:8b",
                base_url="http://127.0.0.1:11434/v1",
                api_key_ref="${OLLAMA_API_KEY}",
            )
        )
        self.repository.set_default_profile_id(created.id)
        self.assertEqual(created.id, self.repository.get_default_profile_id())
        self.assertFalse(created.is_builtin)

    def test_profile_validation_and_delete(self) -> None:
        created = self.repository.create_profile(
            ProviderProfileDraft(
                name="Local",
                default_model="qwen3:8b",
                api_key_ref="${LOCAL_API_KEY}",
            )
        )
        self.repository.delete_profile(created.id)
        self.assertEqual((), self.repository.list_profiles())

        with self.assertRaises(Exception):
            self.repository.create_profile(
                ProviderProfileDraft(
                    name="Bad",
                    default_model="model",
                    api_key_ref="literal-key",
                )
            )


class ProviderApiAndRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_crud_default_and_conversation_model_override(self) -> None:
        provider = FakeProvider(chunks=("ok",))
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=provider,
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    health = await client.get("/health")
                    self.assertEqual(200, health.status_code)
                    self.assertEqual("fake", health.json()["provider"])
                    self.assertEqual("fake-model", health.json()["model"])

                    listed = await client.get("/providers")
                    self.assertEqual(200, listed.status_code)
                    self.assertTrue(listed.json()["items"][0]["isDefault"])

                    created = await client.post(
                        "/providers",
                        json={
                            "name": "Local",
                            "defaultModel": "qwen3:8b",
                            "baseUrl": "http://127.0.0.1:11434/v1",
                            "apiKeyRef": "${OLLAMA_API_KEY}",
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    profile = created.json()["profile"]
                    self.assertFalse(profile["isDefault"])

                    made_default = await client.post(
                        "/providers/default",
                        json={"profileId": profile["id"]},
                    )
                    self.assertEqual(200, made_default.status_code)
                    self.assertTrue(made_default.json()["profile"]["isDefault"])
                    local_health = (await client.get("/health")).json()
                    self.assertEqual("qwen3:8b", local_health["model"])
                    self.assertTrue(local_health["providerConfigured"])

                    conversation = (await client.post("/conversations")).json()
                    patched = await client.patch(
                        f"/conversations/{conversation['id']}",
                        json={
                            "providerProfileId": "provider_environment",
                            "modelOverride": "conversation-model",
                        },
                    )
                    self.assertEqual(200, patched.status_code)
                    self.assertEqual(
                        "conversation-model",
                        patched.json()["modelOverride"],
                    )

                    turn = await client.post(
                        f"/conversations/{conversation['id']}/turns",
                        headers={"Idempotency-Key": "provider-model"},
                        json={"content": "你好"},
                    )
                    turn_id = turn.json()["turnId"]
                    for _ in range(200):
                        detail = (await client.get(f"/turns/{turn_id}")).json()
                        if detail["turnStatus"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual("completed", detail["turnStatus"])
                    self.assertEqual("conversation-model", provider.requests[-1].model)

                    deleted = await client.delete(f"/providers/{profile['id']}")
                    self.assertEqual(204, deleted.status_code)
                    health_after_delete = await client.get("/health")
                    self.assertEqual(
                        "fake-model", health_after_delete.json()["model"]
                    )
            finally:
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
