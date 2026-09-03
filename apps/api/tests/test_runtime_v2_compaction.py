from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.context import ApproximateTokenEstimator
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    ContextProjection,
    ContextProjectionResult,
    RunStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.compaction import RuntimeV2ContextCompactionService
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)
from endless_task.tooling import ToolRegistry


class ScriptedProvider:
    name = "compaction"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        events = self.responses.pop(0) if self.responses else ()
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class CompactionTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "compaction.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _complete_history_turn(self, conversation_id: str, content: str):
        submission = self.repository.create_message_submission(
            conversation_id=conversation_id,
            lane_id=None,
            content=content,
            client_request_id=f"history-{content}",
        )
        self.repository.start_run(submission.run.id)
        self.repository.finalize_run(
            submission.run.id,
            content=f"回答:{content}",
            finish_reason="stop",
        )
        return submission

    def _start_run(self, conversation_id: str, content: str):
        return self.repository.create_message_submission(
            conversation_id=conversation_id,
            lane_id=None,
            content=content,
            client_request_id=f"run-{content}",
        )


class ContextCompactionServiceTest(CompactionTestBase):
    def _service(self, **overrides) -> RuntimeV2ContextCompactionService:
        options = {"max_context_tokens": 2_000}
        options.update(overrides)
        return RuntimeV2ContextCompactionService(
            repository=self.repository,
            **options,
        )

    async def _compact(
        self,
        service: RuntimeV2ContextCompactionService,
        run,
        messages: Sequence[ProviderMessage],
    ):
        return await service.compact(run, messages)

    def test_below_threshold_returns_none(self) -> None:
        conversation = self.chat_repository.create_conversation()
        service = self._service()
        run = self._start_run(conversation.id, "当前问题").run
        self.repository.start_run(run.id)
        messages = (ProviderMessage(role="system", content="短上下文"),)
        result = asyncio.run(self._compact(service, run, messages))
        self.assertIsNone(result)

    def test_compact_creates_summary_entry_and_record(self) -> None:
        conversation = self.chat_repository.create_conversation()
        for index in range(8):
            self._complete_history_turn(
                conversation.id, "长历史" + "字" * 120 + str(index)
            )
        run_submission = self._start_run(conversation.id, "当前问题")
        run = run_submission.run
        self.repository.start_run(run.id)
        entries = self.repository.list_lane_context_entries(run.lane_id)
        messages = ContextProjection().project(entries).messages
        service = self._service()

        result = asyncio.run(self._compact(service, run, messages))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsNotNone(result.summary_entry_id)
        self.assertTrue(result.covered_entry_ids)

        summary_entry = self.repository.get_entry(result.summary_entry_id)
        self.assertEqual(summary_entry.type, TranscriptEntryType.CONTEXT_SUMMARY)
        covered = summary_entry.payload.get("coveredEntryIds")
        self.assertEqual(set(covered), set(result.covered_entry_ids))
        # 覆盖的是最早的完整轮次,不含当前 run 的 trigger。
        self.assertNotIn(run.trigger_entry_id, result.covered_entry_ids)

        compactions = self.repository.list_context_compactions(run.lane_id)
        self.assertEqual(len(compactions), 1)
        self.assertEqual(
            set(compactions[0].covered_entry_ids), set(result.covered_entry_ids)
        )
        # 幂等:再次 compact 不重复覆盖已 covered 的轮次。
        result_again = asyncio.run(self._compact(service, run, messages))
        if result_again is not None:
            self.assertFalse(
                set(result_again.covered_entry_ids) & set(result.covered_entry_ids)
            )


class ContextProjectionCompactionTest(CompactionTestBase):
    def test_projection_skips_covered_entries_and_projects_summary(self) -> None:
        conversation = self.chat_repository.create_conversation()
        for index in range(4):
            self._complete_history_turn(conversation.id, f"历史{index}")
        run_submission = self._start_run(conversation.id, "当前问题")
        run = run_submission.run
        self.repository.start_run(run.id)
        entries = self.repository.list_lane_context_entries(run.lane_id)
        history_ids = [
            entry.id
            for entry in entries
            if entry.type
            in (TranscriptEntryType.USER_MESSAGE, TranscriptEntryType.ASSISTANT_MESSAGE)
        ]
        covered_ids = history_ids[:4]
        self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=run.lane_id,
            type=TranscriptEntryType.CONTEXT_SUMMARY,
            actor=Actor.RUNTIME,
            payload={
                "content": "较早对话摘要（仅供当前会话延续）：\n第 1 轮…",
                "coveredEntryIds": list(covered_ids),
            },
            context_policy={"include_in_llm": True, "transform": "full"},
            display={},
            source_run_id=run.id,
        )
        projection_entries = self.repository.list_lane_context_entries(run.lane_id)
        result: ContextProjectionResult = ContextProjection().project(
            projection_entries
        )
        for covered_id in covered_ids:
            self.assertIn(covered_id, result.skipped_entry_ids)
        summary_messages = [
            message
            for message in result.messages
            if message.role == "system" and "较早对话摘要" in message.content
        ]
        self.assertEqual(len(summary_messages), 1)


class AgentRunExecutorCompactionTest(CompactionTestBase):
    def test_executor_compacts_before_first_model_turn(self) -> None:
        conversation = self.chat_repository.create_conversation()
        for index in range(10):
            self._complete_history_turn(
                conversation.id, "很长的历史轮次内容" + "内容" * 100 + str(index)
            )
        run_submission = self._start_run(conversation.id, "当前问题")

        # 压缩前的全量投影估算(作为收敛基准)。
        projection_before = ContextProjection().project(
            self.repository.list_lane_context_entries(run_submission.run.lane_id)
        )
        estimator = ApproximateTokenEstimator()
        estimate_before = estimator.estimate_messages(projection_before.messages)

        service = RuntimeV2ContextCompactionService(
            repository=self.repository,
            max_context_tokens=4_000,
        )
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("最终回答"),
                    ProviderCompleted(),
                )
            ]
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="compaction-model",
            max_output_tokens=128,
            compaction_hook=service,
        )

        result = asyncio.run(
            executor.execute(
                run_submission.run.id,
                cancellation_token=CancellationToken(),
            )
        )
        self.assertEqual(result.status, RunStatus.COMPLETED)

        compactions = self.repository.list_context_compactions(
            run_submission.run.lane_id
        )
        self.assertEqual(len(compactions), 1)
        self.assertTrue(provider.requests)
        first_request = provider.requests[0]
        all_content = "".join(m.content for m in first_request.messages)
        self.assertIn("较早对话摘要", all_content)
        # 第一个模型请求的上下文已收敛:估算 token 显著低于压缩前全量,
        # 并降到触发阈值(0.8 * 4000)以下。
        estimate_after = estimator.estimate_messages(first_request.messages)
        self.assertLess(estimate_after, estimate_before)
        self.assertLess(estimate_after, 3_200)


if __name__ == "__main__":
    unittest.main()
