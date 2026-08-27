"""R5.12a 验收：工作区 Artifact 文件事实源（落盘、快照、惰性迁移、回滚恢复）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ArtifactKind, ArtifactVersionOperation
from endless_task.storage import (
    Database,
    SqliteArtifactProposalRepository,
    SqliteArtifactRepository,
    SqliteChatRepository,
    SqliteWorkspaceRepository,
)
from endless_task.workspace_runtime.artifact_store import ArtifactFileStore
from endless_task.workspace_runtime.resolver import WorkspaceResolver


class ArtifactFileStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temp.name) / "art.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.resolver = WorkspaceResolver(self.chat, self.workspaces)
        self.store = ArtifactFileStore(self.resolver)
        self.proposals = SqliteArtifactProposalRepository(self.database)
        self.proposals.set_artifact_store(self.store)
        self.artifacts = SqliteArtifactRepository(self.database)
        self.artifacts.set_artifact_store(self.store)
        self.root = Path(self._temp.name) / "ws"
        self.root.mkdir()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _bound_workspace(self) -> str:
        workspace = self.workspaces.create_workspace("项目")
        self.workspaces.bind_root_path(workspace.id, str(self.root))
        return self.chat.create_or_reuse_empty_conversation(workspace.id).id

    def _generic_conversation(self) -> str:
        return self.chat.create_or_reuse_empty_conversation(None).id

    def _accept(self, conversation_id: str, *, content: str, **kwargs) -> tuple:
        proposal = self.proposals.create_proposal(
            conversation_id=conversation_id,
            turn_id="turn_1",
            title=kwargs.get("title", "设计文档"),
            kind=ArtifactKind.MARKDOWN,
            content=content,
            reason="需要保存",
        )
        return self.proposals.accept_proposal(proposal.id)

    def test_workspace_artifact_materializes_to_file(self) -> None:
        conversation_id = self._bound_workspace()
        _, artifact = self._accept(conversation_id, content="# 设计方案")
        self.assertIsNotNone(artifact.storage_path)
        self.assertIsNotNone(artifact.content_sha256)
        main_file = self.root / artifact.storage_path
        self.assertTrue(main_file.exists())
        self.assertEqual("# 设计方案", main_file.read_text(encoding="utf-8"))
        self.assertTrue(artifact.storage_path.startswith(".endless-task/artifacts/"))

    def test_generic_artifact_stays_in_database(self) -> None:
        conversation_id = self._generic_conversation()
        _, artifact = self._accept(conversation_id, content="普通内容")
        self.assertIsNone(artifact.storage_path)

    def test_update_creates_version_snapshot(self) -> None:
        conversation_id = self._bound_workspace()
        proposal, artifact = self._accept(conversation_id, content="v1 内容")
        update = self.proposals.create_proposal(
            conversation_id=conversation_id,
            turn_id="turn_2",
            title="设计文档",
            kind=ArtifactKind.MARKDOWN,
            content="v2 内容",
            reason="更新",
            target_artifact_id=artifact.id,
            base_version_ordinal=artifact.current_version_ordinal,
        )
        _, updated = self.proposals.accept_proposal(update.id)
        main_file = self.root / updated.storage_path
        self.assertEqual("v2 内容", main_file.read_text(encoding="utf-8"))
        snapshot = self.root / ".endless-task" / "versions" / artifact.id / "v1.md"
        self.assertTrue(snapshot.exists())
        self.assertEqual("v1 内容", snapshot.read_text(encoding="utf-8"))

    def test_lazy_migration_on_workspace_update(self) -> None:
        conversation_id = self._bound_workspace()
        # 先建一个数据库全文 Artifact（无 store 注入的仓储路径）
        plain_artifacts = SqliteArtifactRepository(self.database)
        created = plain_artifacts.create_artifact(
            title="存量文档",
            kind=ArtifactKind.TEXT,
            content="存量内容",
            source_conversation_id=conversation_id,
            source_turn_id="turn_old",
        )
        self.assertIsNone(created.artifact.storage_path)
        # 工作区会话更新 → 惰性迁移落盘
        update = self.proposals.create_proposal(
            conversation_id=conversation_id,
            turn_id="turn_3",
            title="存量文档",
            kind=ArtifactKind.TEXT,
            content="迁移后内容",
            reason="更新",
            target_artifact_id=created.artifact.id,
            base_version_ordinal=1,
        )
        _, migrated = self.proposals.accept_proposal(update.id)
        self.assertIsNotNone(migrated.storage_path)
        main_file = self.root / migrated.storage_path
        self.assertTrue(main_file.exists())
        self.assertEqual("迁移后内容", main_file.read_text(encoding="utf-8"))

    def test_rollback_restores_main_file(self) -> None:
        conversation_id = self._bound_workspace()
        proposal, artifact = self._accept(conversation_id, content="v1 内容")
        update = self.proposals.create_proposal(
            conversation_id=conversation_id,
            turn_id="turn_2",
            title="设计文档",
            kind=ArtifactKind.MARKDOWN,
            content="v2 内容",
            reason="更新",
            target_artifact_id=artifact.id,
            base_version_ordinal=1,
        )
        _, updated = self.proposals.accept_proposal(update.id)
        rolled = self.artifacts.rollback_to_version(
            artifact_id=artifact.id,
            target_ordinal=1,
            source_conversation_id=conversation_id,
            source_turn_id="turn_3",
        )
        main_file = self.root / rolled.artifact.storage_path
        self.assertEqual("v1 内容", main_file.read_text(encoding="utf-8"))
        self.assertEqual("v1 内容", rolled.current_version.content)


if __name__ == "__main__":
    unittest.main()
