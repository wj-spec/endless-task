from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import MemoryKind, MemoryStatus
from endless_task.memory import MemoryProposalService
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.storage import (
    Database,
    SqliteMemoryProposalRepository,
    SqliteMemoryRepository,
)
from endless_task.storage.sqlite_memory_repository import AUTO_FACT_ORIGIN


class TextProvider:
    """Yields one scripted text completion per request."""

    name = "auto-fact"

    def __init__(self, texts=()) -> None:
        self.texts = list(texts)
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        index = len(self.requests)
        self.requests.append(request)
        for chunk in self.texts[min(index, len(self.texts) - 1)]:
            yield ProviderTextDelta(text=chunk)
        yield ProviderCompleted()


def _payload(items):
    return json.dumps({"proposals": items}, ensure_ascii=False)


class MemoryAutoFactServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "test.db")
        self.database.initialize()
        self.proposals = SqliteMemoryProposalRepository(self.database)
        self.memories = SqliteMemoryRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _service(self, provider, *, auto_fact_enabled: bool = True) -> MemoryProposalService:
        return MemoryProposalService(
            provider=provider,
            proposal_repository=self.proposals,
            memory_repository=self.memories,
            model="test-model",
            auto_fact_enabled=auto_fact_enabled,
        )

    async def test_high_confidence_fact_is_auto_written_without_proposal(self) -> None:
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户在上海工作。",
                            "reason": "明确陈述",
                            "confidence": "high",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住：我在上海工作。",
            assistant_message="好的，已记住。",
        )
        self.assertEqual(created, ())
        self.assertEqual(self.proposals.list_proposals(conversation_id="conv_1"), ())
        memories = self.memories.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].kind, MemoryKind.FACT)
        self.assertEqual(memories[0].write_origin, AUTO_FACT_ORIGIN)
        self.assertEqual(memories[0].source_conversation_id, "conv_1")
        self.assertEqual(memories[0].source_turn_id, "turn_1")

    async def test_medium_confidence_fact_is_auto_written(self) -> None:
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户养了一只猫。",
                            "reason": "较明确",
                            "confidence": "medium",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="我养了一只猫。",
            assistant_message="好的。",
        )
        memories = self.memories.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].write_origin, AUTO_FACT_ORIGIN)

    async def test_low_confidence_fact_degrades_to_proposal(self) -> None:
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户可能喜欢蓝色。",
                            "reason": "含糊",
                            "confidence": "low",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="我以后可能会多用蓝色。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(self.memories.list_memories(), ())

    async def test_flag_off_keeps_fact_as_proposal(self) -> None:
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户在上海工作。",
                            "reason": "明确陈述",
                            "confidence": "high",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider, auto_fact_enabled=False)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住：我在上海工作。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(self.memories.list_memories(), ())

    async def test_preference_always_requires_confirmation(self) -> None:
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "preference",
                            "content": "用户偏好中文回答。",
                            "reason": "明确表达",
                            "confidence": "high",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="我以后都用中文提问。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(self.memories.list_memories(), ())

    async def test_missing_confidence_keeps_proposal(self) -> None:
        # 模型未输出 confidence 字段时按保守处理：仍走提案确认。
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户在杭州生活。",
                            "reason": "明确陈述",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        created = await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住：我在杭州生活。",
            assistant_message="好的。",
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(self.memories.list_memories(), ())

    async def test_auto_write_cancels_same_content_pending_proposal(self) -> None:
        # 先有一条同内容待确认提案，自动写入后应被取消。
        pending = self.proposals.create_proposal(
            conversation_id="conv_1",
            turn_id="turn_0",
            kind=MemoryKind.FACT,
            content="用户在上海工作。",
            reason="先前提案",
        )
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户在上海工作。",
                            "reason": "明确陈述",
                            "confidence": "high",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住：我在上海工作。",
            assistant_message="好的。",
        )
        remaining = self.proposals.list_proposals(conversation_id="conv_1")
        self.assertEqual(remaining, ())
        memories = self.memories.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].content, "用户在上海工作。")

    async def test_auto_write_skips_content_already_active(self) -> None:
        self.memories.create_memory(
            kind=MemoryKind.FACT,
            content="用户在上海工作。",
            source_conversation_id="conv_1",
            source_turn_id="turn_0",
        )
        provider = TextProvider(
            [
                _payload(
                    [
                        {
                            "kind": "fact",
                            "content": "用户在上海工作。",
                            "reason": "明确陈述",
                            "confidence": "high",
                        }
                    ]
                )
            ]
        )
        service = self._service(provider)
        await service.generate_for_turn(
            conversation_id="conv_1",
            turn_id="turn_1",
            user_message="记住：我在上海工作。",
            assistant_message="好的。",
        )
        memories = self.memories.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].status, MemoryStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
