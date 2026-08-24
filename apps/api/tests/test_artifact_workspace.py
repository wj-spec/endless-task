from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import (
    ArtifactKind,
    ArtifactStatus,
    ArtifactVersionOperation,
)
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime import FakeProvider


class ArtifactWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "api.db",
                memory_proposals_enabled=False,
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

    def _container(self, app):
        return app.state.container

    def _new_conversation(self, app):
        return self._container(app).chat_repository.create_conversation()

    def _seed_artifact(self, app, conversation, *, title="发布计划", content=None):
        return self._container(app).artifact_repository.create_artifact(
            title=title,
            kind=ArtifactKind.MARKDOWN,
            content=content if content is not None else "# 初稿\n\n第一版内容。",
            source_conversation_id=conversation.id,
            source_turn_id="turn_seed",
        ).artifact

    async def test_workspace_membership_derived_from_versions(self) -> None:
        client, app = await self._client()
        owner = self._new_conversation(app)
        stranger = self._new_conversation(app)
        artifact = self._seed_artifact(app, owner)

        response = await client.get(f"/conversations/{owner.id}/workspace")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(owner.id, body["conversationId"])
        self.assertTrue(body["visible"])
        self.assertEqual([artifact.id], [item["id"] for item in body["artifacts"]])
        self.assertEqual([], body["pendingProposals"])

        stranger_body = (
            await client.get(f"/conversations/{stranger.id}/workspace")
        ).json()
        self.assertFalse(stranger_body["visible"])
        self.assertEqual([], stranger_body["artifacts"])

        container = self._container(app)
        container.artifact_repository.append_version(
            artifact_id=artifact.id,
            content="# 初稿\n\n在另一个会话继续修改后的完整内容。",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id=stranger.id,
            source_turn_id="turn_stranger",
        )

        owner_ids = [
            item["id"]
            for item in (
                await client.get(f"/conversations/{owner.id}/workspace")
            ).json()["artifacts"]
        ]
        stranger_ids = [
            item["id"]
            for item in (
                await client.get(f"/conversations/{stranger.id}/workspace")
            ).json()["artifacts"]
        ]
        self.assertEqual([artifact.id], owner_ids)
        self.assertEqual([artifact.id], stranger_ids)

    async def test_deleted_artifact_leaves_workspace(self) -> None:
        client, app = await self._client()
        owner = self._new_conversation(app)
        artifact = self._seed_artifact(app, owner)

        self._container(app).artifact_repository.delete_artifact(artifact.id)
        self.assertEqual(
            ArtifactStatus.DELETED,
            self._container(app).artifact_repository.get_artifact(artifact.id).status,
        )

        body = (await client.get(f"/conversations/{owner.id}/workspace")).json()
        self.assertFalse(body["visible"])
        self.assertEqual([], body["artifacts"])

    async def test_workspace_visible_flag_rules(self) -> None:
        client, app = await self._client()
        container = self._container(app)

        empty = self._new_conversation(app)
        empty_body = (
            await client.get(f"/conversations/{empty.id}/workspace")
        ).json()
        self.assertFalse(empty_body["visible"])

        with_artifact = self._new_conversation(app)
        self._seed_artifact(app, with_artifact)
        artifact_body = (
            await client.get(f"/conversations/{with_artifact.id}/workspace")
        ).json()
        self.assertTrue(artifact_body["visible"])
        self.assertEqual([], artifact_body["pendingProposals"])

        with_proposal = self._new_conversation(app)
        container.artifact_proposal_repository.create_proposal(
            conversation_id=with_proposal.id,
            turn_id="turn_proposal",
            title="待决提案",
            kind=ArtifactKind.MARKDOWN,
            content="# 提案内容\n\n尚未接受的完整文档。",
            reason="等待用户确认",
        )
        proposal_body = (
            await client.get(f"/conversations/{with_proposal.id}/workspace")
        ).json()
        self.assertTrue(proposal_body["visible"])
        self.assertEqual([], proposal_body["artifacts"])
        self.assertEqual(1, len(proposal_body["pendingProposals"]))
        self.assertIsNone(proposal_body["pendingProposals"][0]["baseVersionOrdinal"])

    async def test_workspace_unknown_conversation(self) -> None:
        client, _ = await self._client()
        response = await client.get("/conversations/conv_missing/workspace")
        self.assertEqual(404, response.status_code)

    async def test_workspace_is_read_only(self) -> None:
        client, app = await self._client()
        owner = self._new_conversation(app)
        self._seed_artifact(app, owner)
        self._container(app).artifact_proposal_repository.create_proposal(
            conversation_id=owner.id,
            turn_id="turn_proposal",
            title="待决提案",
            kind=ArtifactKind.MARKDOWN,
            content="# 提案内容\n\n只读性验证用文档。",
            reason="等待用户确认",
        )

        database = self._container(app).artifact_repository._database

        def snapshot():
            with database.connect() as connection:
                return {
                    table: connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                    for table in ("artifacts", "artifact_versions", "artifact_proposals")
                }

        before = snapshot()
        response = await client.get(f"/conversations/{owner.id}/workspace")
        self.assertEqual(200, response.status_code)
        self.assertEqual(before, snapshot())

    async def test_workspace_ordering(self) -> None:
        client, app = await self._client()
        owner = self._new_conversation(app)
        container = self._container(app)
        first = self._seed_artifact(app, owner, title="第一篇", content="内容一。")
        second = self._seed_artifact(app, owner, title="第二篇", content="内容二。")

        container.artifact_repository.append_version(
            artifact_id=first.id,
            content="内容一的更新版本。",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id=owner.id,
            source_turn_id="turn_touch",
        )

        body = (await client.get(f"/conversations/{owner.id}/workspace")).json()
        self.assertEqual(
            [first.id, second.id], [item["id"] for item in body["artifacts"]]
        )

    async def test_stale_update_proposal_conflicts(self) -> None:
        client, app = await self._client()
        creator = self._new_conversation(app)
        editor = self._new_conversation(app)
        container = self._container(app)
        artifact = self._seed_artifact(app, creator)

        stale = container.artifact_proposal_repository.create_proposal(
            conversation_id=editor.id,
            turn_id="turn_stale",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="# 初稿\n\n基于第一版的修改。",
            reason="继续修改",
            target_artifact_id=artifact.id,
            base_version_ordinal=1,
        )
        self.assertEqual(1, stale.base_version_ordinal)

        container.artifact_repository.append_version(
            artifact_id=artifact.id,
            content="# 初稿\n\n别的会话先推进了一版。",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id=creator.id,
            source_turn_id="turn_advance",
        )

        with self.assertRaises(InvalidStateError):
            container.artifact_proposal_repository.accept_proposal(stale.id)
        refreshed = container.artifact_proposal_repository.get_proposal(stale.id)
        self.assertEqual("pending", refreshed.status.value)
        self.assertEqual(2, container.artifact_repository.get_artifact(artifact.id).current_version_ordinal)

        conflict = await client.post(
            f"/artifact-proposals/{stale.id}/resolve", json={"decision": "accept"}
        )
        self.assertEqual(409, conflict.status_code)

        fresh = container.artifact_proposal_repository.create_proposal(
            conversation_id=editor.id,
            turn_id="turn_fresh",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="# 初稿\n\n基于最新版本的修改。",
            reason="基于新基线继续修改",
            target_artifact_id=artifact.id,
            base_version_ordinal=2,
        )
        _, accepted_artifact = container.artifact_proposal_repository.accept_proposal(
            fresh.id
        )
        self.assertEqual(3, accepted_artifact.current_version_ordinal)
        self.assertEqual(
            "# 初稿\n\n基于最新版本的修改。",
            container.artifact_repository.get_current_version(artifact.id).content.strip(),
        )

    async def test_null_baseline_accept_unchanged(self) -> None:
        client, app = await self._client()
        creator = self._new_conversation(app)
        container = self._container(app)
        artifact = self._seed_artifact(app, creator)

        legacy = container.artifact_proposal_repository.create_proposal(
            conversation_id=creator.id,
            turn_id="turn_legacy",
            title="发布计划",
            kind=ArtifactKind.MARKDOWN,
            content="# 初稿\n\n存量提案不校验基线。",
            reason="兼容迁移前提案",
            target_artifact_id=artifact.id,
        )
        self.assertIsNone(legacy.base_version_ordinal)

        container.artifact_repository.append_version(
            artifact_id=artifact.id,
            content="# 初稿\n\n目标先行推进了一版。",
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id=creator.id,
            source_turn_id="turn_advance",
        )

        resolved = await client.post(
            f"/artifact-proposals/{legacy.id}/resolve", json={"decision": "accept"}
        )
        self.assertEqual(200, resolved.status_code)
        self.assertEqual(
            3,
            container.artifact_repository.get_artifact(artifact.id)
            .current_version_ordinal,
        )


if __name__ == "__main__":
    unittest.main()
