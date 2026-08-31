from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.storage import Database
from endless_task.storage.provider_secret_store import ProviderSecretStore
from endless_task.storage.sqlite_chat_repository import SqliteChatRepository
from endless_task.storage.sqlite_provider_profile_repository import (
    ProviderProfileDraft,
    SqliteProviderProfileRepository,
)
from tests.fixtures.workspace_client import create_bound_conversation


class DiscoveringProvider:
    name = "discovery-stub"

    async def list_models(self) -> tuple[tuple[str, str], ...]:
        return (("model-b", "Model B"), ("model-a", "Model A"))

    async def close(self) -> None:
        return None


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

    def test_discovery_preserves_manual_models_and_repairs_default(self) -> None:
        profile = self.repository.create_profile(
            ProviderProfileDraft(
                name="Remote",
                default_model="",
                base_url="https://models.example/v1",
            )
        )
        self.repository.replace_discovered_models(
            profile.id,
            (("model-b", "Model B"), ("model-a", "Model A")),
        )
        self.repository.set_default_model(profile.id, "model-b")
        self.repository.add_model(profile.id, model_id="manual-model")

        models = self.repository.replace_discovered_models(
            profile.id,
            (("model-a", "Model A"),),
        )

        by_id = {model.model_id: model for model in models}
        self.assertFalse(by_id["model-b"].enabled)
        self.assertTrue(by_id["model-a"].enabled)
        self.assertTrue(by_id["manual-model"].enabled)
        self.assertEqual("manual", by_id["manual-model"].source)
        self.assertEqual(
            "manual-model",
            self.repository.get_profile(profile.id).default_model,
        )

    def test_migration_backfills_existing_conversation_model_overrides(self) -> None:
        legacy_path = Path(self._temporary_directory.name) / "legacy.db"
        legacy_database = Database(legacy_path)
        with legacy_database.connect() as connection:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            migration_root = resources.files("endless_task.storage.migrations")
            for migration in sorted(
                item for item in migration_root.iterdir() if item.name.endswith(".sql")
            ):
                if migration.name == "046_provider_model_catalog.sql":
                    break
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                    (migration.name, "2026-01-01T00:00:00Z"),
                )
            connection.execute(
                """
                INSERT INTO provider_profiles (
                    id, name, kind, base_url, api_key_ref, default_model,
                    timeout_seconds, enabled, is_builtin, created_at, updated_at
                ) VALUES (
                    'provider-old', 'Legacy', 'openai_compatible', '', '',
                    'default-model', 60, 1, 0, ?, ?
                )
                """,
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "UPDATE preferences SET default_provider_profile_id = 'provider-old' WHERE id = 1"
            )
        conversations = SqliteChatRepository(legacy_database)
        explicit = conversations.create_conversation()
        conversations.set_conversation_model(
            explicit.id,
            provider_profile_id="provider-old",
            model_override="legacy-explicit",
        )
        inherited = conversations.create_conversation()
        conversations.set_conversation_model(
            inherited.id,
            provider_profile_id=None,
            model_override="legacy-inherited",
        )

        legacy_database.initialize()

        migrated = SqliteProviderProfileRepository(legacy_database)
        self.assertTrue(
            migrated.get_model("provider-old", "legacy-explicit").enabled
        )
        self.assertTrue(
            migrated.get_model("provider-old", "legacy-inherited").enabled
        )


class ProviderSecretStoreTest(unittest.TestCase):
    def test_secret_write_replace_and_delete_use_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-secrets.json"
            store = ProviderSecretStore(path)

            reference = store.put("provider-1", "first-secret")
            self.assertEqual("secret://provider/provider-1", reference)
            self.assertEqual("first-secret", store.resolve(reference))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

            store.put("provider-1", "replacement-secret")
            self.assertEqual("replacement-secret", store.resolve(reference))
            self.assertNotIn("first-secret", path.read_text(encoding="utf-8"))

            store.delete("provider-1")
            self.assertIsNone(store.resolve(reference))

    def test_environment_reference_is_resolved_without_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider-secrets.json"
            store = ProviderSecretStore(path)
            previous = os.environ.get("ENDLESS_TASK_TEST_PROVIDER_KEY")
            os.environ["ENDLESS_TASK_TEST_PROVIDER_KEY"] = "environment-secret"
            try:
                self.assertEqual(
                    "environment-secret",
                    store.resolve("${ENDLESS_TASK_TEST_PROVIDER_KEY}"),
                )
                self.assertFalse(path.exists())
            finally:
                if previous is None:
                    os.environ.pop("ENDLESS_TASK_TEST_PROVIDER_KEY", None)
                else:
                    os.environ["ENDLESS_TASK_TEST_PROVIDER_KEY"] = previous


class ProviderApiAndRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_profiles_crud_default_and_conversation_model_override(self) -> None:
        provider = FakeProvider(chunks=("ok",))
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    runtime="v1",
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

                    conversation = await create_bound_conversation(client)
                    added_model = await client.post(
                        "/providers/provider_environment/models",
                        json={"modelId": "conversation-model"},
                    )
                    self.assertEqual(201, added_model.status_code)
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

                    disabled_model = await client.patch(
                        "/providers/provider_environment/models/conversation-model",
                        json={"enabled": False},
                    )
                    self.assertEqual(200, disabled_model.status_code)
                    fallback_turn = await client.post(
                        f"/conversations/{conversation['id']}/turns",
                        headers={"Idempotency-Key": "provider-model-fallback"},
                        json={"content": "继续"},
                    )
                    fallback_turn_id = fallback_turn.json()["turnId"]
                    for _ in range(200):
                        fallback_detail = (
                            await client.get(f"/turns/{fallback_turn_id}")
                        ).json()
                        if fallback_detail["turnStatus"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual("completed", fallback_detail["turnStatus"])
                    self.assertEqual("fake-model", provider.requests[-1].model)
                    self.assertIn(
                        "选择的模型已停用",
                        provider.requests[-1].messages[0].content,
                    )

                    deleted = await client.delete(f"/providers/{profile['id']}")
                    self.assertEqual(204, deleted.status_code)
                    health_after_delete = await client.get("/health")
                    self.assertEqual(
                        "fake-model", health_after_delete.json()["model"]
                    )
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_refresh_models_populates_catalog_and_selects_default(self) -> None:
        provider = FakeProvider(chunks=("ok",))
        with tempfile.TemporaryDirectory() as directory, patch(
            "endless_task.runtime.provider_manager.OpenAICompatibleProvider",
            return_value=DiscoveringProvider(),
        ):
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    runtime="v1",
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
                    created = await client.post(
                        "/providers",
                        json={
                            "name": "Discovery service",
                            "baseUrl": "https://models.example/v1",
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    profile_id = created.json()["profile"]["id"]

                    refreshed = await client.post(
                        f"/providers/{profile_id}/refresh-models"
                    )

                    self.assertEqual(200, refreshed.status_code)
                    profile = refreshed.json()["profile"]
                    self.assertEqual("ready", profile["connectionState"])
                    self.assertEqual("model-a", profile["defaultModel"])
                    self.assertEqual(
                        {"model-a", "model-b"},
                        {model["modelId"] for model in profile["models"]},
                    )
                    self.assertTrue(
                        all(model["source"] == "discovered" for model in profile["models"])
                    )
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_credentials_are_not_returned_and_blank_key_is_not_destructive(
        self,
    ) -> None:
        provider = FakeProvider(chunks=("ok",))
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "api.db"
            app = create_app(
                settings=AppSettings(
                    database_path=database_path,
                    runtime="v1",
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
                    created = await client.post(
                        "/providers",
                        json={
                            "name": "Credential service",
                            "defaultModel": "model-a",
                            "baseUrl": "https://models.example/v1",
                            "apiKey": "first-api-secret",
                        },
                    )
                    self.assertEqual(201, created.status_code)
                    profile = created.json()["profile"]
                    self.assertTrue(profile["apiKeyConfigured"])
                    self.assertNotIn("first-api-secret", created.text)
                    self.assertNotIn("apiKeyRef", profile)

                    store = ProviderSecretStore(
                        database_path.parent / "provider-secrets.json"
                    )
                    reference = store.reference_for(profile["id"])
                    self.assertEqual("first-api-secret", store.resolve(reference))

                    blank = await client.patch(
                        f"/providers/{profile['id']}",
                        json={"apiKey": ""},
                    )
                    self.assertEqual(200, blank.status_code)
                    self.assertTrue(blank.json()["profile"]["apiKeyConfigured"])
                    self.assertEqual("first-api-secret", store.resolve(reference))

                    replaced = await client.patch(
                        f"/providers/{profile['id']}",
                        json={"apiKey": "replacement-api-secret"},
                    )
                    self.assertEqual(200, replaced.status_code)
                    self.assertNotIn("replacement-api-secret", replaced.text)
                    self.assertEqual("replacement-api-secret", store.resolve(reference))

                    conversation = await create_bound_conversation(client)
                    invalid_model = await client.patch(
                        f"/conversations/{conversation['id']}",
                        json={
                            "providerProfileId": profile["id"],
                            "modelOverride": "missing-model",
                        },
                    )
                    self.assertEqual(409, invalid_model.status_code)
                    self.assertEqual(
                        "model_not_available",
                        invalid_model.json()["error"]["code"],
                    )

                    deleted = await client.delete(f"/providers/{profile['id']}")
                    self.assertEqual(204, deleted.status_code)
                    self.assertIsNone(store.resolve(reference))
            finally:
                await lifespan.__aexit__(None, None, None)

    async def test_disabled_conversation_profile_falls_back_with_context_note(
        self,
    ) -> None:
        provider = FakeProvider(chunks=("ok",))
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                settings=AppSettings(
                    database_path=Path(directory) / "api.db",
                    runtime="v1",
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
                    created = await client.post(
                        "/providers",
                        json={
                            "name": "Local",
                            "defaultModel": "qwen3:8b",
                            "baseUrl": "http://127.0.0.1:11434/v1",
                        },
                    )
                    profile = created.json()["profile"]
                    conversation = await create_bound_conversation(client)
                    patched = await client.patch(
                        f"/conversations/{conversation['id']}",
                        json={"providerProfileId": profile["id"]},
                    )
                    self.assertEqual(200, patched.status_code)
                    disabled = await client.patch(
                        f"/providers/{profile['id']}",
                        json={"enabled": False},
                    )
                    self.assertEqual(200, disabled.status_code)

                    turn = await client.post(
                        f"/conversations/{conversation['id']}/turns",
                        headers={"Idempotency-Key": "provider-fallback"},
                        json={"content": "你好"},
                    )
                    turn_id = turn.json()["turnId"]
                    for _ in range(200):
                        detail = (await client.get(f"/turns/{turn_id}")).json()
                        if detail["turnStatus"] in {"completed", "failed"}:
                            break
                        await asyncio.sleep(0.01)

                    self.assertEqual("completed", detail["turnStatus"])
                    self.assertEqual("fake-model", provider.requests[-1].model)
                    self.assertIn(
                        "已停用，已回落到全局默认模型",
                        provider.requests[-1].messages[0].content,
                    )
            finally:
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()