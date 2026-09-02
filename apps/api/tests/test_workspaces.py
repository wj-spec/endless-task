"""R5.11 工作区与知识分区验收：实体、会话分区、检索可见性、提案归属。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import (
    ConversationKind,
    KnowledgeProposalType,
    KnowledgeScope,
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
)
from endless_task.domain.repositories import ConflictError, ValidationError
from endless_task.knowledge import KnowledgeProposalService
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteKnowledgeProposalRepository,
    SqliteKnowledgeRepository,
    SqliteWorkspaceRepository,
)


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "workspaces.db"
        )
        self.database.initialize()
        self.workspaces = SqliteWorkspaceRepository(self.database)
        self.chat = SqliteChatRepository(self.database)
        self.knowledge = SqliteKnowledgeRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def add_source(self, *, title: str, content: str, workspace_id=None):
        return self.knowledge.create_source(
            kind=KnowledgeSourceKind.NOTE,
            origin=KnowledgeSourceOrigin.USER,
            title=title,
            content=content,
            workspace_id=workspace_id,
        )


class WorkspaceRepositoryTest(WorkspaceTestCase):
    def test_create_and_list(self) -> None:
        first = self.workspaces.create_workspace("  写作计划  ")
        second = self.workspaces.create_workspace("家庭事务")
        listed = self.workspaces.list_workspaces()
        self.assertEqual([item.id for item in listed], [first.id, second.id])
        self.assertEqual(first.name, "写作计划")

    def test_duplicate_name_rejected(self) -> None:
        self.workspaces.create_workspace("写作计划")
        with self.assertRaises(ConflictError):
            self.workspaces.create_workspace("写作计划")

    def test_empty_name_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.workspaces.create_workspace("   ")


class ConversationPartitionTest(WorkspaceTestCase):
    def test_conversation_carries_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        conversation = self.chat.create_conversation(workspace.id)
        self.assertEqual(conversation.workspace_id, workspace.id)
        general = self.chat.create_conversation()
        self.assertIsNone(general.workspace_id)

    def test_empty_reuse_matches_workspace(self) -> None:
        workspace_a = self.workspaces.create_workspace("计划A")
        workspace_b = self.workspaces.create_workspace("计划B")
        empty_a = self.chat.create_or_reuse_empty_conversation(workspace_a.id)

        reused = self.chat.create_or_reuse_empty_conversation(workspace_a.id)
        self.assertEqual(reused.id, empty_a.id)
        other = self.chat.create_or_reuse_empty_conversation(workspace_b.id)
        self.assertNotEqual(other.id, empty_a.id)
        general = self.chat.create_or_reuse_empty_conversation()
        self.assertNotEqual(general.id, empty_a.id)

    def test_list_conversations_filters_by_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        scoped = self.chat.create_conversation(workspace.id)
        general = self.chat.create_conversation()

        scoped_ids = {
            item.id
            for item in self.chat.list_conversations(workspace_id=workspace.id)
        }
        general_ids = {
            item.id for item in self.chat.list_conversations(general_only=True)
        }
        all_ids = {item.id for item in self.chat.list_conversations()}

        self.assertEqual(scoped_ids, {scoped.id})
        self.assertEqual(general_ids, {general.id})
        self.assertEqual(all_ids, {scoped.id, general.id})

    def test_branch_inherits_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        parent = self.chat.create_conversation(workspace.id)
        self.chat.create_turn(
            conversation_id=parent.id, client_request_id="r1", content="第一轮"
        )
        branch = self.chat.create_branch(parent_conversation_id=parent.id)
        self.assertEqual(branch.workspace_id, workspace.id)

    def test_set_conversation_workspace_migrates(self) -> None:
        workspace_a = self.workspaces.create_workspace("计划A")
        workspace_b = self.workspaces.create_workspace("计划B")
        conversation = self.chat.create_conversation(workspace_a.id)
        self.assertEqual(conversation.workspace_id, workspace_a.id)

        migrated = self.chat.set_conversation_workspace(
            conversation.id, workspace_b.id
        )
        self.assertEqual(migrated.workspace_id, workspace_b.id)

    def test_count_conversations_for_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("计划A")
        self.assertEqual(
            self.chat.count_conversations_for_workspace(workspace.id), 0
        )
        self.chat.create_conversation(workspace.id)
        self.chat.create_conversation(workspace.id)
        self.assertEqual(
            self.chat.count_conversations_for_workspace(workspace.id), 2
        )
        other = self.workspaces.create_workspace("计划B")
        self.chat.create_conversation(other.id)
        self.assertEqual(
            self.chat.count_conversations_for_workspace(workspace.id), 2
        )
        self.assertEqual(
            self.chat.count_conversations_for_workspace(other.id), 1
        )

    def test_list_workspace_conversation_ids(self) -> None:
        workspace = self.workspaces.create_workspace("计划A")
        self.assertEqual(self.chat.list_workspace_conversation_ids(workspace.id), ())
        first = self.chat.create_conversation(workspace.id)
        second = self.chat.create_conversation(workspace.id)
        ids = self.chat.list_workspace_conversation_ids(workspace.id)
        self.assertEqual({first.id, second.id}, set(ids))
        other = self.workspaces.create_workspace("计划B")
        self.chat.create_conversation(other.id)
        self.assertEqual(
            {first.id, second.id}, set(self.chat.list_workspace_conversation_ids(workspace.id))
        )
        self.assertEqual(len(self.chat.list_workspace_conversation_ids(other.id)), 1)

    def test_delete_workspace_removes_registration_only(self) -> None:
        workspace = self.workspaces.create_workspace("计划A")
        same_id = workspace.id
        removed = self.workspaces.delete_workspace(same_id)
        self.assertTrue(removed)
        self.assertFalse(self.workspaces.delete_workspace(same_id))
        # 记录确实被删除。
        with self.assertRaises(Exception):
            self.workspaces.get_workspace(same_id)


class KnowledgePartitionTest(WorkspaceTestCase):
    def test_list_sources_partition_visibility(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        scoped = self.add_source(
            title="构建命令", content="本项目构建命令为 make build。",
            workspace_id=workspace.id,
        )
        global_source = self.add_source(
            title="咖啡机规范", content="使用咖啡机后必须清洗奶管。"
        )

        visible = self.knowledge.list_sources(workspace_id=workspace.id)
        self.assertEqual({item.id for item in visible}, {scoped.id, global_source.id})
        general = self.knowledge.list_sources(workspace_id="general")
        self.assertEqual({item.id for item in general}, {global_source.id})
        everything = self.knowledge.list_sources()
        self.assertEqual(
            {item.id for item in everything}, {scoped.id, global_source.id}
        )

    def test_search_isolates_workspaces_and_keeps_global(self) -> None:
        workspace_a = self.workspaces.create_workspace("计划A")
        workspace_b = self.workspaces.create_workspace("计划B")
        self.add_source(
            title="项目甲部署", content="项目甲部署前必须跑回归测试套件。",
            workspace_id=workspace_a.id,
        )
        self.add_source(
            title="项目乙部署", content="项目乙部署前必须备份数据库。",
            workspace_id=workspace_b.id,
        )
        global_source = self.add_source(
            title="部署通则", content="所有部署前必须通知值班同学。"
        )

        grouped_a = self.knowledge.search(
            "部署前必须", [KnowledgeScope.SOURCE], workspace_id=workspace_a.id
        )
        titles_a = {hit.title for hit in grouped_a[KnowledgeScope.SOURCE]}
        self.assertIn("项目甲部署", titles_a)
        self.assertIn("部署通则", titles_a)
        self.assertNotIn("项目乙部署", titles_a)

        grouped_b = self.knowledge.search(
            "部署前必须", [KnowledgeScope.SOURCE], workspace_id=workspace_b.id
        )
        titles_b = {hit.title for hit in grouped_b[KnowledgeScope.SOURCE]}
        self.assertIn("项目乙部署", titles_b)
        self.assertNotIn("项目甲部署", titles_b)

        grouped_general = self.knowledge.search(
            "部署前必须", [KnowledgeScope.SOURCE], workspace_id="general"
        )
        titles_general = {
            hit.title for hit in grouped_general[KnowledgeScope.SOURCE]
        }
        self.assertEqual(titles_general, {"部署通则"})

    def test_search_invalid_workspace_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.knowledge.search(
                "任意", [KnowledgeScope.SOURCE], workspace_id="bad'; DROP TABLE x;--"
            )

    def test_chunked_file_source_respects_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        first = "第一段。" + "这是工作区文件第一段内容。" * 40
        second = "第二段。" + "这是工作区文件第二段内容。" * 40
        self.knowledge.create_source(
            kind=KnowledgeSourceKind.FILE,
            origin=KnowledgeSourceOrigin.USER,
            title="工作区文件",
            content=f"{first}\n\n{second}",
            file_name="工作区文件.md",
            workspace_id=workspace.id,
        )
        grouped = self.knowledge.search(
            "第一段内容", [KnowledgeScope.SOURCE], workspace_id="general"
        )
        self.assertNotIn(KnowledgeScope.SOURCE, grouped)
        grouped = self.knowledge.search(
            "第一段内容", [KnowledgeScope.SOURCE], workspace_id=workspace.id
        )
        hits = grouped[KnowledgeScope.SOURCE]
        self.assertTrue(hits)
        self.assertIsNotNone(hits[0].chunk_seq)


class AttributionScriptedProvider:
    name = "attribution"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.requests = []

    async def stream(self, request, cancellation_token):
        self.requests.append(request)
        yield ProviderTextDelta(
            text=json.dumps({"proposals": [self.payload]}, ensure_ascii=False)
        )
        yield ProviderCompleted()


class ProposalAttributionTest(WorkspaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.proposals = SqliteKnowledgeProposalRepository(self.database)

    def make_service(self, payload: dict) -> KnowledgeProposalService:
        return KnowledgeProposalService(
            provider=AttributionScriptedProvider(payload),
            proposal_repository=self.proposals,
            knowledge_repository=self.knowledge,
            model="fake-model",
            chat_repository=self.chat,
        )

    def run_turn(self, service, conversation_id: str):
        return asyncio.run(
            service.generate_for_turn(
                conversation_id=conversation_id,
                turn_id="turn_attr",
                user_message="帮我整理一下",
                assistant_message=(
                    "好的，这是整理后的长期规范说明文本，"
                    "涵盖了操作步骤、注意事项与后续维护要求，供以后长期参考。"
                ),
            )
        )

    def test_workspace_conversation_defaults_to_workspace(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        conversation = self.chat.create_conversation(workspace.id)
        service = self.make_service(
            {"type": "add_source", "title": "构建命令",
             "content": "本项目构建命令为 make build。", "reason": "长期有效"}
        )
        proposals = self.run_turn(service, conversation.id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].payload["workspace_id"], workspace.id)

    def test_personal_content_can_go_global(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        conversation = self.chat.create_conversation(workspace.id)
        service = self.make_service(
            {"type": "add_source", "title": "用户的饮品偏好",
             "content": "用户喜欢喝绿茶，生活中偏好清淡口味。",
             "reason": "用户个人习惯", "global": True}
        )
        proposals = self.run_turn(service, conversation.id)
        self.assertEqual(len(proposals), 1)
        self.assertIsNone(proposals[0].payload.get("workspace_id"))

    def test_global_flag_without_personal_signal_downgrades(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        conversation = self.chat.create_conversation(workspace.id)
        service = self.make_service(
            {"type": "add_source", "title": "构建命令",
             "content": "本项目构建命令为 make build。",
             "reason": "长期有效", "global": True}
        )
        proposals = self.run_turn(service, conversation.id)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].payload["workspace_id"], workspace.id)

    def test_general_conversation_goes_global(self) -> None:
        conversation = self.chat.create_conversation()
        service = self.make_service(
            {"type": "add_source", "title": "咖啡机规范",
             "content": "使用咖啡机后必须清洗奶管。", "reason": "长期有效"}
        )
        proposals = self.run_turn(service, conversation.id)
        self.assertEqual(len(proposals), 1)
        self.assertIsNone(proposals[0].payload.get("workspace_id"))

    def test_accept_with_workspace_override(self) -> None:
        workspace = self.workspaces.create_workspace("写作计划")
        conversation = self.chat.create_conversation()
        service = self.make_service(
            {"type": "add_source", "title": "构建命令",
             "content": "本项目构建命令为 make build。", "reason": "长期有效"}
        )
        proposals = self.run_turn(service, conversation.id)
        proposal = proposals[0]

        _, source = self.proposals.accept_proposal(
            proposal.id, workspace_override=workspace.id
        )
        self.assertEqual(source.workspace_id, workspace.id)


class LifecyclePartitionTest(WorkspaceTestCase):
    def test_same_content_across_workspaces_is_not_duplicate(self) -> None:
        from endless_task.knowledge.lifecycle import KnowledgeLifecycleService
        from endless_task.storage import (
            SqliteRetrievalEventRepository,
        )

        workspace_a = self.workspaces.create_workspace("计划A")
        workspace_b = self.workspaces.create_workspace("计划B")
        proposals = SqliteKnowledgeProposalRepository(self.database)
        events = SqliteRetrievalEventRepository(self.database)
        service = KnowledgeLifecycleService(
            knowledge_repository=self.knowledge,
            proposal_repository=proposals,
            retrieval_event_repository=events,
            chat_repository=self.chat,
        )
        self.chat.create_conversation()
        content = "部署前必须跑回归测试，否则不允许上线。"
        self.add_source(
            title="计划甲部署规范", content=content, workspace_id=workspace_a.id
        )
        scoped_duplicate = self.add_source(
            title="计划乙部署规范", content=content, workspace_id=workspace_b.id
        )
        self.assertIsNone(service.detect_duplicates(scoped_duplicate))

        same_workspace = self.add_source(
            title="计划甲部署规范副本", content=content,
            workspace_id=workspace_a.id,
        )
        proposal = service.detect_duplicates(same_workspace)
        self.assertIsNotNone(proposal)
        assert proposal is not None
        self.assertIs(proposal.proposal_type, KnowledgeProposalType.MERGE_SOURCE)


if __name__ == "__main__":
    unittest.main()
