from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import ArtifactKind, ArtifactVersionOperation
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from endless_task.runtime import FakeProvider
from endless_task.storage import Database, SqliteArtifactRepository


class StepClock:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"2026-08-23T09:{self._value // 60:02d}:{self._value % 60:02d}.000Z"


class StepIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


class ArtifactVersioningRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "versioning.db")
        self.database.initialize()
        self.repository = SqliteArtifactRepository(
            self.database,
            clock=StepClock(),
            id_factory=StepIdFactory(),
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _seed_three_versions(self, **create_overrides) -> str:
        payload = dict(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="第一版内容",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
            source_labels=("read plan.md:L1-L9",),
        )
        payload.update(create_overrides)
        snapshot = self.repository.create_artifact(**payload)
        artifact_id = snapshot.artifact.id
        self.repository.append_version(
            artifact_id=artifact_id,
            content="第二版内容",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
            source_labels=("memory:mem_0001",),
        )
        self.repository.append_version(
            artifact_id=artifact_id,
            content="第三版内容",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id="conv_3",
            source_turn_id="turn_3",
        )
        return artifact_id

    def test_rollback_appends_version_and_preserves_history(self) -> None:
        artifact_id = self._seed_three_versions()
        before = self.repository.list_versions(artifact_id)
        self.assertEqual([1, 2, 3], [item.ordinal for item in before])

        snapshot = self.repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=1,
            source_conversation_id="conv_4",
            source_turn_id="turn_4",
        )

        after = self.repository.list_versions(artifact_id)
        self.assertEqual([1, 2, 3, 4], [item.ordinal for item in after])
        for old_version, new_version in zip(before, after[:3]):
            self.assertEqual(old_version, new_version)

        rolled = after[3]
        self.assertEqual(ArtifactVersionOperation.ROLLBACK, rolled.operation)
        self.assertEqual("第一版内容", rolled.content)
        self.assertEqual(4, snapshot.artifact.current_version_ordinal)

    def test_rollback_copies_content_and_labels(self) -> None:
        artifact_id = self._seed_three_versions()

        snapshot = self.repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=2,
            source_conversation_id="conv_9",
            source_turn_id="turn_9",
        )
        rolled = snapshot.current_version
        self.assertEqual("第二版内容", rolled.content)
        self.assertEqual(("memory:mem_0001",), rolled.source_labels)
        self.assertEqual("conv_9", rolled.source_conversation_id)
        self.assertEqual("turn_9", rolled.source_turn_id)
        self.assertEqual("回滚到版本 2", rolled.note)

        custom = self.repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=1,
            source_conversation_id="conv_9",
            source_turn_id="turn_10",
            note="恢复到初稿",
        )
        self.assertEqual("恢复到初稿", custom.current_version.note)

    def test_invalid_rollback_is_rejected(self) -> None:
        artifact_id = self._seed_three_versions()

        with self.assertRaises(InvalidStateError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=3,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )
        with self.assertRaises(InvalidStateError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=9,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )
        with self.assertRaises(InvalidStateError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=0,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )
        with self.assertRaises(InvalidStateError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=True,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )
        with self.assertRaises(NotFoundError):
            self.repository.rollback_to_version(
                artifact_id="art_missing",
                target_ordinal=1,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )
        with self.assertRaises(ValidationError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=1,
                source_conversation_id="  ",
                source_turn_id="turn_4",
            )
        with self.assertRaises(ValidationError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=1,
                source_conversation_id="conv_4",
                source_turn_id="",
            )
        self.assertEqual(3, self.repository.get_artifact(artifact_id).current_version_ordinal)

    def test_deleted_artifact_readable_but_not_rollbackable(self) -> None:
        artifact_id = self._seed_three_versions()
        self.repository.delete_artifact(artifact_id)

        self.assertEqual(3, self.repository.get_artifact(artifact_id).current_version_ordinal)
        self.assertEqual(3, len(self.repository.list_versions(artifact_id)))
        with self.assertRaises(InvalidStateError):
            self.repository.rollback_to_version(
                artifact_id=artifact_id,
                target_ordinal=1,
                source_conversation_id="conv_4",
                source_turn_id="turn_4",
            )

    def test_current_ordinal_tracks_max(self) -> None:
        artifact_id = self._seed_three_versions()

        def assert_tracking() -> None:
            artifact = self.repository.get_artifact(artifact_id)
            versions = self.repository.list_versions(artifact_id)
            self.assertEqual(len(versions), artifact.current_version_ordinal)
            self.assertEqual(
                max(item.ordinal for item in versions),
                artifact.current_version_ordinal,
            )

        assert_tracking()
        self.repository.append_version(
            artifact_id=artifact_id,
            content="第四版内容",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id="conv_4",
            source_turn_id="turn_4",
        )
        assert_tracking()
        self.repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=2,
            source_conversation_id="conv_4",
            source_turn_id="turn_5",
        )
        assert_tracking()


class ArtifactVersioningApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "api.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
            ),
            provider=FakeProvider(chunks=("你好",)),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        return client, app

    @staticmethod
    def _seed(app) -> str:
        repository = app.state.container.artifact_repository
        snapshot = repository.create_artifact(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="第一版内容",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
            source_labels=("read plan.md:L1-L9",),
        )
        repository.append_version(
            artifact_id=snapshot.artifact.id,
            content="第二版内容",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
        )
        return snapshot.artifact.id

    async def test_api_read_and_rollback_flow(self) -> None:
        client, app = await self._client()
        artifact_id = self._seed(app)

        listing = await client.get("/artifacts")
        self.assertEqual(200, listing.status_code)
        items = listing.json()["items"]
        self.assertEqual([artifact_id], [item["id"] for item in items])
        self.assertEqual(2, items[0]["currentVersionOrdinal"])

        detail = await client.get(f"/artifacts/{artifact_id}")
        self.assertEqual(200, detail.status_code)
        self.assertEqual(2, detail.json()["currentVersion"]["ordinal"])
        self.assertEqual("第二版内容", detail.json()["currentVersion"]["content"])

        versions = await client.get(f"/artifacts/{artifact_id}/versions")
        self.assertEqual(200, versions.status_code)
        payload = versions.json()["items"]
        self.assertEqual([1, 2], [item["ordinal"] for item in payload])
        self.assertEqual("第一版内容", payload[0]["content"])
        self.assertEqual(["read plan.md:L1-L9"], payload[0]["sourceLabels"])

        rolled = await client.post(
            f"/artifacts/{artifact_id}/rollback",
            json={
                "targetOrdinal": 1,
                "sourceConversationId": "conv_3",
                "sourceTurnId": "turn_3",
            },
        )
        self.assertEqual(200, rolled.status_code)
        body = rolled.json()
        self.assertEqual(3, body["artifact"]["currentVersionOrdinal"])
        self.assertEqual("第一版内容", body["currentVersion"]["content"])
        self.assertEqual("rollback", body["currentVersion"]["operation"])
        self.assertEqual("回滚到版本 1", body["currentVersion"]["note"])
        self.assertEqual(["read plan.md:L1-L9"], body["currentVersion"]["sourceLabels"])

        conflict = await client.post(
            f"/artifacts/{artifact_id}/rollback",
            json={
                "targetOrdinal": 3,
                "sourceConversationId": "conv_3",
                "sourceTurnId": "turn_3",
            },
        )
        self.assertEqual(409, conflict.status_code)

        missing_version = await client.post(
            f"/artifacts/{artifact_id}/rollback",
            json={
                "targetOrdinal": 99,
                "sourceConversationId": "conv_3",
                "sourceTurnId": "turn_3",
            },
        )
        self.assertEqual(409, missing_version.status_code)

        missing_artifact = await client.post(
            "/artifacts/art_missing/rollback",
            json={
                "targetOrdinal": 1,
                "sourceConversationId": "conv_3",
                "sourceTurnId": "turn_3",
            },
        )
        self.assertEqual(404, missing_artifact.status_code)

        empty_source = await client.post(
            f"/artifacts/{artifact_id}/rollback",
            json={
                "targetOrdinal": 2,
                "sourceConversationId": "  ",
                "sourceTurnId": "turn_3",
            },
        )
        self.assertEqual(400, empty_source.status_code)

        detail_after = await client.get(f"/artifacts/{artifact_id}")
        self.assertEqual(3, detail_after.json()["currentVersion"]["ordinal"])


if __name__ == "__main__":
    unittest.main()
