from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersionOperation,
)
from endless_task.domain.repositories import (
    InvalidStateError,
    NotFoundError,
    ValidationError,
)
from endless_task.storage import Database, SqliteArtifactRepository


class StepClock:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"2026-08-23T00:00:{self._value:02d}.000Z"


class StepIdFactory:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, prefix: str) -> str:
        self._value += 1
        return f"{prefix}_{self._value:04d}"


class SqliteArtifactRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "artifacts.db")
        self.database.initialize()
        self.clock = StepClock()
        self.repository = SqliteArtifactRepository(
            self.database,
            clock=self.clock,
            id_factory=StepIdFactory(),
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create(self, **overrides):
        payload = dict(
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="# 发布计划\n第一版内容",
            source_conversation_id="conv_1",
            source_turn_id="turn_1",
        )
        payload.update(overrides)
        return self.repository.create_artifact(**payload)

    def test_create_artifact_writes_version_one(self) -> None:
        snapshot = self._create(
            source_labels=("read me.txt:L1-L7", "mem_0001"),
            note="首轮整理",
        )
        artifact = snapshot.artifact
        self.assertEqual("art_0001", artifact.id)
        self.assertEqual("发布计划", artifact.title)
        self.assertEqual(ArtifactKind.MARKDOWN, artifact.kind)
        self.assertEqual(ArtifactStatus.ACTIVE, artifact.status)
        self.assertEqual(1, artifact.current_version_ordinal)
        self.assertEqual("2026-08-23T00:00:01.000Z", artifact.created_at)
        self.assertEqual(artifact.created_at, artifact.updated_at)
        self.assertIsNone(artifact.deleted_at)

        version = snapshot.current_version
        self.assertEqual("artv_0002", version.id)
        self.assertEqual(artifact.id, version.artifact_id)
        self.assertEqual(1, version.ordinal)
        self.assertEqual("# 发布计划\n第一版内容", version.content)
        self.assertEqual(ArtifactVersionOperation.CREATE, version.operation)
        self.assertEqual("conv_1", version.source_conversation_id)
        self.assertEqual("turn_1", version.source_turn_id)
        self.assertEqual(("read me.txt:L1-L7", "mem_0001"), version.source_labels)
        self.assertEqual("首轮整理", version.note)

        self.assertEqual(version, self.repository.get_current_version(artifact.id))
        self.assertEqual(version, self.repository.get_version(artifact.id, 1))

    def test_append_version_advances_current_ordinal(self) -> None:
        snapshot = self._create()
        artifact_id = snapshot.artifact.id

        updated = self.repository.append_version(
            artifact_id=artifact_id,
            content="第二版内容",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id="conv_2",
            source_turn_id="turn_2",
            note="按反馈改写",
        )
        self.assertEqual(2, updated.artifact.current_version_ordinal)
        self.assertEqual("2026-08-23T00:00:02.000Z", updated.artifact.updated_at)
        self.assertEqual(2, updated.current_version.ordinal)
        self.assertEqual("第二版内容", updated.current_version.content)
        self.assertEqual(ArtifactVersionOperation.UPDATE, updated.current_version.operation)

        versions = self.repository.list_versions(artifact_id)
        self.assertEqual([1, 2], [item.ordinal for item in versions])
        first = versions[0]
        self.assertEqual("# 发布计划\n第一版内容", first.content)
        self.assertEqual(ArtifactVersionOperation.CREATE, first.operation)
        self.assertEqual("conv_1", first.source_conversation_id)

        default_listing = self.repository.list_artifacts()
        self.assertEqual([artifact_id], [item.id for item in default_listing])

    def test_soft_delete_rejects_further_versions(self) -> None:
        snapshot = self._create()
        artifact_id = snapshot.artifact.id

        deleted = self.repository.delete_artifact(artifact_id)
        self.assertEqual(ArtifactStatus.DELETED, deleted.status)
        self.assertEqual("2026-08-23T00:00:02.000Z", deleted.deleted_at)

        with self.assertRaises(InvalidStateError):
            self.repository.append_version(
                artifact_id=artifact_id,
                content="不应写入",
                operation=ArtifactVersionOperation.UPDATE,
                source_conversation_id="conv_2",
                source_turn_id="turn_2",
            )
        with self.assertRaises(InvalidStateError):
            self.repository.delete_artifact(artifact_id)

        self.assertEqual((), self.repository.list_artifacts())
        listing = self.repository.list_artifacts(include_deleted=True)
        self.assertEqual([artifact_id], [item.id for item in listing])
        self.assertEqual(1, len(self.repository.list_versions(artifact_id)))

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self._create(title="   ")
        with self.assertRaises(ValidationError):
            self._create(title="字" * 201)
        with self.assertRaises(ValidationError):
            self._create(content="字" * 200_001)
        with self.assertRaises(ValidationError):
            self._create(content="  ")
        with self.assertRaises(ValidationError):
            self._create(kind="pdf")
        with self.assertRaises(ValidationError):
            self._create(source_conversation_id="")
        with self.assertRaises(ValidationError):
            self._create(source_labels=("ok", 42))
        with self.assertRaises(ValidationError):
            self._create(source_labels=("  ",))

        snapshot = self._create()
        artifact_id = snapshot.artifact.id

        with self.assertRaises(ValidationError):
            self.repository.append_version(
                artifact_id=artifact_id,
                content="新版本",
                operation="rewrite",
                source_conversation_id="conv_2",
                source_turn_id="turn_2",
            )
        with self.assertRaises(ValidationError):
            self.repository.append_version(
                artifact_id=artifact_id,
                content="新版本",
                operation=ArtifactVersionOperation.CREATE,
                source_conversation_id="conv_2",
                source_turn_id="turn_2",
            )
        with self.assertRaises(ValidationError):
            self.repository.append_version(
                artifact_id=artifact_id,
                content="新版本",
                operation=ArtifactVersionOperation.ROLLBACK,
                source_conversation_id="conv_2",
                source_turn_id="turn_2",
            )
        with self.assertRaises(NotFoundError):
            self.repository.get_artifact("art_missing")
        with self.assertRaises(NotFoundError):
            self.repository.append_version(
                artifact_id="art_missing",
                content="新版本",
                operation=ArtifactVersionOperation.UPDATE,
                source_conversation_id="conv_2",
                source_turn_id="turn_2",
            )
        with self.assertRaises(NotFoundError):
            self.repository.get_version(artifact_id, 9)
        self.assertEqual(1, snapshot.artifact.current_version_ordinal)


if __name__ == "__main__":
    unittest.main()
