"""E1 交付物 A：**进度读取器**（模块级纯函数）+ 一致性用例。

这一条用例是本批的"行为验证"：它不信任何读码结论，只信"实际发给 provider 的
消息序列"——在真跑的 run 里，把**第 2 次请求前**那一刻的库状态快照下来，
再用"进度读取器 + 既有投影"重建一次，两者必须逐字段相等。

它同时也钉住了 ④（``v2_runtime_events.model_text_delta``）**不是可选项**：
投影为 TOOL_CALL 合成的 assistant 消息 ``content`` 恒为 ``""``，而实际发出的是该轮的
助手文本；不看 delta 通道，重建就少掉"已完成轮对模型说过的话"（红检查 ④）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    ModelTurnStatus,
    RunStatus,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.execution_impl.run_progress import (
    collect_run_progress,
    is_uncertain_tool_execution,
    read_run_progress,
    rebuild_provider_messages,
    run_message_entries,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolDefinition, ToolEffect, ToolRegistry, ToolResult


class SnapshottingScriptedProvider:
    """ScriptedProvider + 在**第 N 次请求发出前**把库里的进度原样快照下来。

    为什么必须在 provider 内部取快照：一致性用例要比的是"崩溃前**已经发出**的
    序列"，而 run 结束之后库里的状态已经往前走（收尾会再写一条 ASSISTANT_MESSAGE）。
    在请求发出那一刻取，才是同一个时刻的两份视图。
    """

    name = "snapshotting"

    def __init__(
        self,
        responses: Sequence[Sequence[ProviderStreamEvent]],
        *,
        repository: SqliteRuntimeV2Repository,
        run_id: str,
        lane_id: str,
        trigger_entry_id: str,
        snapshot_at: int = 2,
    ) -> None:
        self.responses = [list(response) for response in responses]
        self.requests: list[ProviderRequest] = []
        self._repository = repository
        self._run_id = run_id
        self._lane_id = lane_id
        self._trigger_entry_id = trigger_entry_id
        self._snapshot_at = snapshot_at
        self.snapshot: dict[str, Any] | None = None

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        index = len(self.requests)
        if index == self._snapshot_at:
            self.snapshot = {
                # 立刻拷成 tuple：`provider_messages` 这个 list 后面还会被 extend。
                "messages": tuple(request.messages),
                "progress": collect_run_progress(
                    repository=self._repository,
                    run_id=self._run_id,
                ),
                "entries": (
                    *self._repository.list_entry_context_entries(self._trigger_entry_id),
                    *run_message_entries(
                        self._repository.list_entries(
                            self._lane_id,
                            include_variants=True,
                        ),
                        run_id=self._run_id,
                    ),
                ),
            }
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class FakeTool:
    def __init__(self, name: str = "read_file", *, content: str = "file content") -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fake {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            timeout_seconds=2.0,
        )
        self.content = content
        self.calls: list[Any] = []

    async def execute(self, call, cancellation_token):
        del cancellation_token
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content=self.content)

    def requires_explicit_confirmation(self, call) -> bool:
        del call
        return False


def _two_turn_responses() -> list[list[ProviderStreamEvent]]:
    return [
        [
            ProviderTextDelta("我先读取文件。"),
            ProviderToolCall(id="call_1", name="read_file", arguments={"path": "a.txt"}),
            ProviderCompleted(finish_reason="tool_calls", input_tokens=11, output_tokens=4),
        ],
        [
            ProviderTextDelta("文件内容是 file content。"),
            ProviderCompleted(finish_reason="stop", input_tokens=13, output_tokens=6),
        ],
    ]


class RunProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, lane, trigger, run

    def _run_two_turns(self):
        """真跑一段两轮的 run，返回 (provider, tool, result, run, lane, trigger)。"""
        _, lane, trigger, run = self._create_run()
        provider = SnapshottingScriptedProvider(
            _two_turn_responses(),
            repository=self.repository,
            run_id=run.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        tool = FakeTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        return provider, tool, result, run, lane, trigger

    # ------------------------------------------------------------------ 一致性

    def test_rebuild_matches_messages_actually_sent(self) -> None:
        """重建出的消息序列 == 崩溃前**已发出**的序列（含 tool_call ↔ tool_result 配对）。"""
        provider, _tool, _result, run, _lane, _trigger = self._run_two_turns()

        snapshot = provider.snapshot
        assert snapshot is not None
        sent = snapshot["messages"]
        progress = snapshot["progress"]
        entries = snapshot["entries"]

        # 前置事实（先把"实际发出的是什么"钉死，避免重建只是"自己对自己相等"）：
        self.assertEqual(
            ["user", "assistant", "tool"],
            [message.role for message in sent],
        )
        self.assertEqual("读取文件", sent[0].content)
        self.assertEqual("我先读取文件。", sent[1].content)
        self.assertEqual("call_1", sent[1].tool_calls[0].id)
        self.assertEqual("file content", sent[2].content)
        self.assertEqual("call_1", sent[2].tool_call_id)
        self.assertEqual("read_file", sent[2].name)

        rebuilt = rebuild_provider_messages(entries=entries, progress=progress)

        self.assertEqual(sent, rebuilt)
        # 快照时刻：只有第 1 轮完成（第 2 轮正在飞）。
        self.assertEqual(1, progress.completed_turn_count)
        self.assertEqual(11, progress.input_tokens)
        self.assertEqual(4, progress.output_tokens)
        self.assertEqual(1, len(progress.confirmed_tools))
        self.assertEqual((), progress.uncertain_tools)
        self.assertEqual("我先读取文件。", progress.completed_turns[0].assistant_text)
        self.assertEqual(run.id, progress.run_id)

    def test_rebuild_drops_covered_entries_like_the_projection_does(self) -> None:
        """压缩语义：``context_summary.coveredEntryIds`` 覆盖的条目不再进上下文。"""
        _conversation, lane, _trigger, run = self._create_run()
        user = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "读取文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.TOOL_CALL,
            actor=Actor.TOOL,
            payload={"callId": "call_1", "toolName": "read_file", "arguments": {"path": "a"}},
            context_policy={"include_in_llm": False, "transform": "none", "trust_level": "untrusted"},
            source_run_id=run.id,
        )
        tool_result = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.TOOL_RESULT,
            actor=Actor.TOOL,
            payload={"callId": "call_1", "toolName": "read_file", "content": "file content"},
            context_policy={"include_in_llm": True, "transform": "tool_result", "trust_level": "untrusted"},
            source_run_id=run.id,
        )
        summary = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.CONTEXT_SUMMARY,
            actor=Actor.RUNTIME,
            payload={"content": "此前读了 a.txt", "coveredEntryIds": [tool_result.id]},
            context_policy={"include_in_llm": True, "transform": "full"},
            source_run_id=run.id,
        )

        empty_progress = read_run_progress(
            run_id=run.id,
            model_turns=(),
            tool_executions_by_turn={},
        )
        rebuilt = rebuild_provider_messages(
            entries=(user, *run_message_entries(
                self.repository.list_entries(lane.id, include_variants=True),
                run_id=run.id,
            )),
            progress=empty_progress,
        )

        self.assertEqual(["user", "system"], [message.role for message in rebuilt])
        self.assertEqual("此前读了 a.txt", rebuilt[1].content)
        self.assertEqual([summary.id], [entry.id for entry in (summary,)])

    # ------------------------------------------------------------------ 读取器

    def test_read_run_progress_reports_completed_turns_and_confirmed_tools(self) -> None:
        _provider, _tool, result, run, _lane, _trigger = self._run_two_turns()

        progress = collect_run_progress(repository=self.repository, run_id=run.id)

        self.assertEqual(2, progress.completed_turn_count)
        self.assertEqual(tuple(result.model_turn_ids), progress.completed_turn_ids)
        self.assertEqual(3, progress.next_turn_index)
        self.assertEqual(24, progress.input_tokens)
        self.assertEqual(10, progress.output_tokens)
        self.assertEqual(1, progress.tool_call_count)
        self.assertEqual(1, len(progress.confirmed_tools))
        self.assertEqual((), progress.uncertain_tools)
        # 这里 run 已经 COMPLETED（`_run_two_turns` 跑完收尾）⇒ **终态 run 不可续**
        # （I-2 口径 b 的读取器判据）：已完成轮还在，但 `resumable` 必须为假。
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(run.id).status)
        self.assertFalse(progress.resumable)
        self.assertEqual(
            ["我先读取文件。", "文件内容是 file content。"],
            [turn.assistant_text for turn in progress.completed_turns],
        )

    def test_uncertain_tools_are_reported_before_and_after_crash_flattening(self) -> None:
        """ADR §3.2 判据 2 的输入：崩溃时处于活动态的工具 ⇒ 结果**无法确认**。

        两个时刻都要认：
        * 崩溃**前**（行仍活动）：``running`` / ``created`` 都在活动集合里；
          而 ``coordinator`` 在批量执行**之前**就把整批工具一律置 ``RUNNING``，
          所以 ``running`` 不等于"真被调用过"——只能按"可能已产生副作用"处理。
        * 崩溃**后**（``finalize_interrupted_run`` 已把四个活动态压成同一个
          ``FAILED`` + ``retryable=0`` + 同一句话）：行上不可区分"未调用"与
          "已调用但结果未知"，同样只能算不确定。
        """
        _conversation, _lane, _trigger, run = self._create_run()
        self.repository.start_run(run.id)
        turn = self.repository.start_model_turn(
            run_id=run.id,
            provider="scripted",
            model="scripted-model",
        )
        running, _entry = self.repository.record_tool_call(
            model_turn_id=turn.id,
            call_id="call_running",
            tool_name="send_message",
            arguments={"text": "hello"},
        )
        created, _entry = self.repository.record_tool_call(
            model_turn_id=turn.id,
            call_id="call_created",
            tool_name="send_message",
            arguments={"text": "world"},
        )
        self.repository.transition_tool_execution_status(
            running.id,
            ToolExecutionStatus.RUNNING,
            event_type="tool_execution_started",
        )

        # ① 崩溃前
        before = collect_run_progress(repository=self.repository, run_id=run.id)
        self.assertEqual(
            {"call_running", "call_created"},
            {tool.call_id for tool in before.uncertain_tools},
        )
        self.assertEqual((), before.confirmed_tools)
        self.assertEqual(0, before.completed_turn_count)

        # ② 崩溃后（启动清扫/恢复路径都会走这一步）
        self.repository.finalize_interrupted_run(run.id)
        flattened_running = self.repository.get_tool_execution(running.id)
        flattened_created = self.repository.get_tool_execution(created.id)
        for record in (flattened_running, flattened_created):
            self.assertEqual(ToolExecutionStatus.FAILED, record.status)
            self.assertEqual(0, record.retryable)
            self.assertEqual({"reason": "interrupted"}, record.error_details)
            self.assertTrue(is_uncertain_tool_execution(record))

        after = collect_run_progress(repository=self.repository, run_id=run.id)
        self.assertEqual(
            {"call_running", "call_created"},
            {tool.call_id for tool in after.uncertain_tools},
        )
        self.assertTrue(all(tool.retryable is False for tool in after.uncertain_tools))

    def test_confirmed_tool_is_not_uncertain(self) -> None:
        _conversation, _lane, _trigger, run = self._create_run()
        self.repository.start_run(run.id)
        turn = self.repository.start_model_turn(run_id=run.id, provider="p", model="m")
        record, _entry = self.repository.record_tool_call(
            model_turn_id=turn.id,
            call_id="call_1",
            tool_name="read_file",
            arguments={"path": "a.txt"},
        )
        self.repository.record_tool_result(
            record.id,
            status=ToolExecutionStatus.COMPLETED,
            content="file content",
            event_type="tool_execution_completed",
        )
        self.repository.transition_model_turn_status(
            turn.id,
            ModelTurnStatus.COMPLETED,
            event_type="model_turn_completed",
            input_tokens=7,
            output_tokens=3,
        )

        progress = collect_run_progress(repository=self.repository, run_id=run.id)

        self.assertEqual(1, progress.completed_turn_count)
        self.assertEqual((), progress.uncertain_tools)
        self.assertEqual(1, len(progress.confirmed_tools))
        self.assertEqual("call_1", progress.confirmed_tools[0].call_id)
        self.assertEqual("read_file", progress.confirmed_tools[0].tool_name)
        self.assertIsNotNone(progress.confirmed_tools[0].result_entry_id)

    # ------------------------------------------------------------------ 作用域

    def test_run_message_entries_are_scoped_by_run_id(self) -> None:
        """按 run id 直取运行期条目：**不靠 lane 链自动继承**（ADR §7 / 评审 M5）。"""
        _conversation, lane, trigger, run = self._create_run()
        # 同一个 lane 上**先后**两个 run（lane 层只允许一条活动行）：本 run 先落终态，
        # 再建另一个 run。另一个 run 的条目**不该**被算进本 run 的重建输入。
        self.repository.transition_run_status(
            run.id,
            RunStatus.FAILED,
            event_type="run_failed",
        )
        other_run = self.repository.create_run(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        mine = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.TOOL_RESULT,
            actor=Actor.TOOL,
            payload={"callId": "call_1", "toolName": "read_file", "content": "file content"},
            context_policy={"include_in_llm": True, "transform": "tool_result"},
            source_run_id=run.id,
        )
        other = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.TOOL_RESULT,
            actor=Actor.TOOL,
            payload={"callId": "call_2", "toolName": "read_file", "content": "other"},
            context_policy={"include_in_llm": True, "transform": "tool_result"},
            source_run_id=other_run.id,
        )
        assistant = self.repository.append_entry(
            conversation_id=run.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.ASSISTANT_MESSAGE,
            actor=Actor.ASSISTANT,
            payload={"content": "全轮拼接的助手文本"},
            context_policy={"include_in_llm": True, "transform": "full"},
            source_run_id=run.id,
        )

        selected = run_message_entries(
            self.repository.list_entries(lane.id, include_variants=True),
            run_id=run.id,
        )

        self.assertEqual((mine.id,), tuple(entry.id for entry in selected))
        self.assertNotIn(other.id, {entry.id for entry in selected})
        # 收尾时写的那条 ASSISTANT_MESSAGE 是"全轮拼接"，不是某一轮的文本 ⇒ 不进重建。
        self.assertNotIn(assistant.id, {entry.id for entry in selected})


if __name__ == "__main__":
    unittest.main()
