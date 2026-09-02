from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import FinishReason, KnowledgeScope, MemoryKind
from endless_task.runtime import P0ContextBuilder
from endless_task.runtime.provider import ProviderMessage
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteContextRepository,
    SqliteMemoryRepository,
)

from test_sqlite_chat_repository import SequenceClock, SequenceIdFactory


class CharacterTokenEstimator:
    """Simple exact estimator that keeps budget assertions easy to audit."""

    def estimate_text(self, content: str) -> int:
        return len(content)

    def estimate_messages(self, messages) -> int:
        return 2 + sum(4 + len(message.content) for message in messages)


class _FakeKnowledgeHit:
    scope = KnowledgeScope.SOURCE
    title = "参考标题"
    snippet = "参考片段内容"
    ref_id = "ref_1"
    source_id = "source_1"
    chunk_seq = 1
    score = 1.0


class _FakeKnowledgeRepository:
    def __init__(self) -> None:
        self.search_count = 0

    def search(self, query, scopes, max_hits, workspace_id=None):
        del query, scopes, max_hits, workspace_id
        self.search_count += 1
        return {KnowledgeScope.SOURCE: [_FakeKnowledgeHit()]}


class SystemLayeringTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "layering.db")
        self.database.initialize()
        self.clock = SequenceClock()
        self.ids = SequenceIdFactory()
        self.chat_repository = SqliteChatRepository(
            self.database, clock=self.clock, id_factory=self.ids
        )
        self.context_repository = SqliteContextRepository(
            self.database, clock=self.clock, id_factory=self.ids
        )
        self.memory_repository = SqliteMemoryRepository(
            self.database, clock=self.clock, id_factory=self.ids
        )
        self.estimator = CharacterTokenEstimator()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _conversation_with_memory(self):
        conversation = self.chat_repository.create_conversation()
        self.memory_repository.create_memory(
            kind=MemoryKind.PREFERENCE,
            content="用户偏好简洁回答。",
            source_conversation_id=conversation.id,
            source_turn_id="turn_1",
        )
        return conversation

    def _builder(self, *, skill_prompt_builder=None, **overrides) -> P0ContextBuilder:
        return P0ContextBuilder(
            self.chat_repository,
            system_prompt="SYSTEM",
            system_prompt_version="test-v1",
            max_context_tokens=10_000,
            context_repository=self.context_repository,
            memory_repository=self.memory_repository,
            token_estimator=self.estimator,
            skill_prompt_builder=skill_prompt_builder,
            **overrides,
        )

    def _new_turn(self, conversation_id: str, content: str = "当前问题"):
        return self.chat_repository.create_turn(
            conversation_id=conversation_id,
            client_request_id="request-1",
            content=content,
        )

    def test_layered_messages_cover_all_block_content(self) -> None:
        conversation = self._conversation_with_memory()
        current = self._new_turn(conversation.id)
        builder = self._builder(
            skill_prompt_builder=lambda workspace_id: (
                "<available_skills><skill><name>demo</name></skill></available_skills>"
            )
        )
        layered = builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
            workspace_id=None,
        )
        self.assertTrue(layered)
        self.assertTrue(all(message.role == "system" for message in layered))
        contents = [message.content for message in layered]
        self.assertEqual("SYSTEM", contents[0])
        self.assertIn("用户偏好简洁回答。", "\n".join(contents))
        self.assertIn("<available_skills>", "\n".join(contents))

    def test_layer_order_and_empty_layers_skipped(self) -> None:
        conversation = self.chat_repository.create_conversation()
        current = self._new_turn(conversation.id)
        builder = self._builder()
        layered = builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
            workspace_id=None,
            provider_fallback_note="模型服务暂时降级。",
        )
        # 无记忆、无知识、无技能、无文件时只有 S0 指令基座与 S1 运行时通知。
        self.assertEqual(2, len(layered))
        self.assertEqual("SYSTEM", layered[0].content)
        self.assertIn("模型服务暂时降级", layered[1].content)

    def test_retrieval_layer_is_last_and_marks_untrusted(self) -> None:
        conversation = self.chat_repository.create_conversation()
        current = self._new_turn(conversation.id, content="查询一个较长的知识问题")
        fake = _FakeKnowledgeRepository()
        builder = self._builder(knowledge_repository=fake)
        layered = builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
        )
        retrieval = [m for m in layered if "参考片段内容" in m.content]
        self.assertEqual(1, len(retrieval))
        self.assertIn("不可信内容", retrieval[0].content)
        self.assertIs(layered[-1], retrieval[0])
        self.assertEqual(1, fake.search_count)

    def test_knowledge_injection_happens_once_across_exports(self) -> None:
        conversation = self.chat_repository.create_conversation()
        current = self._new_turn(conversation.id, content="查询一个较长的知识问题")
        fake = _FakeKnowledgeRepository()
        builder = self._builder(knowledge_repository=fake)
        # 两个出口各自调用一次块生成，埋点只应触发一次（块生成同源）。
        builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
        )
        builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
        )
        self.assertEqual(2, fake.search_count)

    def test_v1_single_system_message_matches_layered_parts(self) -> None:
        conversation = self._conversation_with_memory()
        current = self._new_turn(conversation.id)
        builder = self._builder(
            skill_prompt_builder=lambda workspace_id: (
                "<available_skills>skill</available_skills>"
            )
        )
        single = builder.build_system_context(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
            workspace_id=None,
        )
        layered = builder.build_system_messages(
            conversation.id,
            current.user_message.content,
            turn_id=current.turn.id,
            workspace_id=None,
        )
        # v1 单条出口以指令基座开头，且每个分层消息的内容都应完整出现在其中。
        self.assertTrue(single.startswith("SYSTEM"))
        for message in layered:
            self.assertIn(message.content, single)
        self.assertIn("用户偏好简洁回答。", single)
        self.assertIn("<available_skills>", single)

    def test_build_keeps_single_system_message_for_v1(self) -> None:
        conversation = self._conversation_with_memory()
        current = self._new_turn(conversation.id)
        built = self._builder().build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )
        self.assertEqual("system", built.messages[0].role)
        self.assertIn("用户偏好简洁回答。", built.messages[0].content)
        self.assertEqual("user", built.messages[-1].role)


if __name__ == "__main__":
    unittest.main()
