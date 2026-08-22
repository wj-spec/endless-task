from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import FinishReason, ResponseVariantOperation
from endless_task.runtime import (
    ApproximateTokenEstimator,
    ContextBuildError,
    P0ContextBuilder,
)
from endless_task.runtime.provider import ProviderMessage
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteContextRepository,
)

from test_sqlite_chat_repository import SequenceClock, SequenceIdFactory


class CharacterTokenEstimator:
    """Simple exact estimator that keeps budget assertions easy to audit."""

    def estimate_text(self, content: str) -> int:
        return len(content)

    def estimate_messages(self, messages) -> int:
        return 2 + sum(4 + len(message.content) for message in messages)


class ContextBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "context.db")
        self.database.initialize()
        self.clock = SequenceClock()
        self.ids = SequenceIdFactory()
        self.chat_repository = SqliteChatRepository(
            self.database,
            clock=self.clock,
            id_factory=self.ids,
        )
        self.context_repository = SqliteContextRepository(
            self.database,
            clock=self.clock,
            id_factory=self.ids,
        )
        self.estimator = CharacterTokenEstimator()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _complete_turn(self, conversation_id: str, ordinal: int, answer: str):
        snapshot = self.chat_repository.create_turn(
            conversation_id=conversation_id,
            client_request_id=f"request-{ordinal}",
            content=f"用户消息{ordinal}-" + "U" * 10,
        )
        variant_id = snapshot.turn.active_response_variant_id
        self.chat_repository.mark_response_running(
            turn_id=snapshot.turn.id,
            variant_id=variant_id,
        )
        return self.chat_repository.complete_response(
            turn_id=snapshot.turn.id,
            variant_id=variant_id,
            content=answer,
            finish_reason=FinishReason.STOP,
        )

    def _builder(self, *, max_context_tokens: int = 220) -> P0ContextBuilder:
        return P0ContextBuilder(
            self.chat_repository,
            system_prompt="SYSTEM",
            system_prompt_version="test-v1",
            max_context_tokens=max_context_tokens,
            summary_token_limit=60,
            context_repository=self.context_repository,
            token_estimator=self.estimator,
        )

    def test_long_history_is_summarized_and_snapshot_is_persisted(self) -> None:
        conversation = self.chat_repository.create_conversation()
        completed = [
            self._complete_turn(conversation.id, index, "助手回答-" + "A" * 18)
            for index in range(1, 5)
        ]
        current = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-5",
            content="当前问题",
        )

        context = self._builder().build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )

        self.assertLessEqual(context.input_token_estimate, 200)
        self.assertIsNotNone(context.summary_revision_id)
        self.assertEqual("system", context.messages[0].role)
        self.assertIn("较早对话摘要", context.messages[1].content)
        self.assertEqual("当前问题", context.messages[-1].content)
        self.assertTrue(context.included_turns)
        self.assertEqual(4, context.included_turns[-1][0])
        self.assertNotIn(completed[0].user_message.content, [m.content for m in context.messages])

        snapshot = self.context_repository.get_context_snapshot(
            current.turn.active_response_variant_id
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(context.summary_revision_id, snapshot.summary_revision_id)
        self.assertEqual(context.input_token_estimate, snapshot.input_token_estimate)
        self.assertEqual(20, snapshot.reserved_output_tokens)

        rebuilt = self._builder().build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )
        self.assertEqual(snapshot.id, rebuilt.snapshot.id)
        self.assertEqual(context.messages, rebuilt.messages)

    def test_failed_historical_turn_is_excluded_as_a_complete_pair(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self._complete_turn(conversation.id, 1, "有效回答")
        failed = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-2",
            content="失败问题不应进入上下文",
        )
        failed_variant_id = failed.turn.active_response_variant_id
        self.chat_repository.mark_response_running(
            turn_id=failed.turn.id,
            variant_id=failed_variant_id,
        )
        self.chat_repository.fail_response(
            turn_id=failed.turn.id,
            variant_id=failed_variant_id,
            partial_content="不完整回答不应进入上下文",
            error_code="provider_error",
        )
        current = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-3",
            content="继续",
        )

        context = self._builder(max_context_tokens=1_000).build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )
        contents = [message.content for message in context.messages]

        self.assertIn("有效回答", contents)
        self.assertNotIn("失败问题不应进入上下文", contents)
        self.assertNotIn("不完整回答不应进入上下文", contents)
        self.assertEqual("继续", contents[-1])

    def test_current_message_is_never_silently_truncated(self) -> None:
        conversation = self.chat_repository.create_conversation()
        current = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-1",
            content="X" * 100,
        )

        with self.assertRaises(ContextBuildError) as raised:
            self._builder(max_context_tokens=40).build(
                current.turn.id,
                response_variant_id=current.turn.active_response_variant_id,
                reserved_output_tokens=10,
            )

        self.assertEqual("context_too_large", raised.exception.code)
        self.assertIsNone(
            self.context_repository.get_context_snapshot(
                current.turn.active_response_variant_id
            )
        )

    def test_regeneration_rebuilds_the_same_logical_context(self) -> None:
        conversation = self.chat_repository.create_conversation()
        self._complete_turn(conversation.id, 1, "前序回答")
        current = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-2",
            content="请重新回答",
        )
        first_variant_id = current.turn.active_response_variant_id
        first_context = self._builder(max_context_tokens=1_000).build(
            current.turn.id,
            response_variant_id=first_variant_id,
            reserved_output_tokens=20,
        )
        self.chat_repository.mark_response_running(
            turn_id=current.turn.id,
            variant_id=first_variant_id,
        )
        self.chat_repository.complete_response(
            turn_id=current.turn.id,
            variant_id=first_variant_id,
            content="第一次回答",
            finish_reason=FinishReason.STOP,
        )
        regenerated = self.chat_repository.create_response_variant(
            turn_id=current.turn.id,
            command_request_id="regenerate-2",
            operation=ResponseVariantOperation.REGENERATE,
        )

        regenerated_context = self._builder(max_context_tokens=1_000).build(
            current.turn.id,
            response_variant_id=regenerated.response_variant_id,
            reserved_output_tokens=20,
        )

        self.assertEqual(first_context.messages, regenerated_context.messages)
        self.assertEqual(first_context.included_turns, regenerated_context.included_turns)
        self.assertNotEqual(first_context.snapshot.id, regenerated_context.snapshot.id)

    def test_context_never_reads_another_conversation(self) -> None:
        first_conversation = self.chat_repository.create_conversation()
        second_conversation = self.chat_repository.create_conversation()
        self._complete_turn(first_conversation.id, 1, "第一会话回答")
        self._complete_turn(second_conversation.id, 1, "第二会话机密")
        current = self.chat_repository.create_turn(
            conversation_id=first_conversation.id,
            client_request_id="request-2",
            content="继续第一会话",
        )

        context = self._builder(max_context_tokens=1_000).build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )

        contents = [message.content for message in context.messages]
        self.assertIn("第一会话回答", contents)
        self.assertNotIn("第二会话机密", contents)

    def test_estimator_counts_chat_framing_and_chinese_text(self) -> None:
        estimator = ApproximateTokenEstimator()
        messages = (
            ProviderMessage(role="system", content="系统"),
            ProviderMessage(role="user", content="hello"),
        )
        self.assertEqual(14, estimator.estimate_messages(messages))

    def test_delete_cascades_through_context_and_summary_records(self) -> None:
        conversation = self.chat_repository.create_conversation()
        for index in range(1, 5):
            self._complete_turn(conversation.id, index, "助手回答-" + "A" * 18)
        current = self.chat_repository.create_turn(
            conversation_id=conversation.id,
            client_request_id="request-5",
            content="当前问题",
        )
        self._builder().build(
            current.turn.id,
            response_variant_id=current.turn.active_response_variant_id,
            reserved_output_tokens=20,
        )
        self.chat_repository.cancel_response(
            turn_id=current.turn.id,
            variant_id=current.turn.active_response_variant_id,
            partial_content="",
        )

        self.chat_repository.delete_conversation(conversation.id)

        with self.database.connect() as connection:
            summary_count = connection.execute(
                "SELECT COUNT(*) FROM conversation_summary_revisions"
            ).fetchone()[0]
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM context_snapshots"
            ).fetchone()[0]
        self.assertEqual(0, summary_count)
        self.assertEqual(0, snapshot_count)


if __name__ == "__main__":
    unittest.main()
