"""回归：投影必须产出「合法」的消息序列（tool 消息必须有配对的 assistant tool_calls）。

背景（线上事故）：`tool_call` 条目的默认策略是 `include_in_llm=False`，而
`tool_result` 是 `include_in_llm=True`。历史里只要有工具结果，投影就会产出一条
**孤儿 tool 消息**；OpenAI 兼容服务（如 DeepSeek）会直接返回 400：

    Messages with role 'tool' must be a response to a preceding message with 'tool_calls'

表现为「模型服务返回错误，可以重试。」且**重试永远失败**（请求形态永久非法）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.runtime_v2 import (
    Actor,
    ContextProjection,
    TranscriptEntryType,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)


class ContextProjectionToolPairingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "pairing.db")
        self.database.initialize()
        self.chat = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.conversation = self.chat.create_conversation()
        self.lane = self.repository.create_lane(
            conversation_id=self.conversation.id
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _entry(self, entry_type, payload, *, policy=None):
        return self.repository.append_entry(
            conversation_id=self.conversation.id,
            lane_id=self.lane.id,
            type=entry_type,
            actor=Actor.TOOL if entry_type in (
                TranscriptEntryType.TOOL_CALL,
                TranscriptEntryType.TOOL_RESULT,
            ) else Actor.USER,
            payload=payload,
            context_policy=policy
            or {"include_in_llm": True, "transform": "full"},
            display={},
        )

    def _tool_call(self, call_id: str = "call_1", name: str = "list_workspace_dir"):
        return self._entry(
            TranscriptEntryType.TOOL_CALL,
            {"callId": call_id, "toolName": name, "arguments": {}},
            policy={
                "include_in_llm": False,
                "transform": "none",
                "trust_level": "untrusted",
            },
        )

    def _tool_result(self, call_id: str = "call_1", content: str = "目录内容"):
        return self._entry(
            TranscriptEntryType.TOOL_RESULT,
            {"callId": call_id, "toolName": "list_workspace_dir", "content": content},
            policy={
                "include_in_llm": True,
                "transform": "tool_result",
                "trust_level": "untrusted",
            },
        )

    def test_orphan_tool_result_is_paired_with_synthetic_tool_call(self) -> None:
        self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "看一下目录"})
        self._tool_call()
        self._tool_result()
        self._entry(TranscriptEntryType.ASSISTANT_MESSAGE, {"content": "有 3 个文件"})
        self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "再讲讲细节"})

        result = ContextProjection().project(
            self.repository.list_lane_context_entries(self.lane.id)
        )
        roles = [message.role for message in result.messages]
        self.assertEqual(["user", "assistant", "tool", "assistant", "user"], roles)

        # 合成的 assistant 消息必须携带与 tool 消息配对的 call id。
        tool_call_message = result.messages[1]
        self.assertEqual(("call_1",), tuple(c.id for c in tool_call_message.tool_calls))
        self.assertEqual("call_1", result.messages[2].tool_call_id)
        self.assertEqual("list_workspace_dir", result.messages[2].name)

    def test_tool_result_without_any_tool_call_entry_is_dropped(self) -> None:
        self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "问题"})
        orphan = self._tool_result(call_id="call_missing")

        result = ContextProjection().project(
            self.repository.list_lane_context_entries(self.lane.id)
        )
        self.assertEqual(["user"], [message.role for message in result.messages])
        # 无法配对的历史结果只能丢弃（保留会破坏请求合法性）。
        self.assertIn(orphan.id, result.skipped_entry_ids)

    def test_tool_call_without_result_stays_out_of_context(self) -> None:
        self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "问题"})
        self._tool_call()

        result = ContextProjection().project(
            self.repository.list_lane_context_entries(self.lane.id)
        )
        self.assertEqual(["user"], [message.role for message in result.messages])

    def test_two_results_share_one_synthetic_tool_call(self) -> None:
        self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "问题"})
        self._tool_call()
        self._tool_result(content="第一次")
        self._tool_result(content="第二次")

        result = ContextProjection().project(
            self.repository.list_lane_context_entries(self.lane.id)
        )
        roles = [message.role for message in result.messages]
        self.assertEqual(["user", "assistant", "tool", "tool"], roles)
        self.assertEqual(
            ("call_1",), tuple(c.id for c in result.messages[1].tool_calls)
        )

    def test_plain_history_is_unchanged(self) -> None:
        user = self._entry(TranscriptEntryType.USER_MESSAGE, {"content": "你好"})
        assistant = self._entry(
            TranscriptEntryType.ASSISTANT_MESSAGE, {"content": "你好，有什么可以帮你？"}
        )

        result = ContextProjection().project(
            self.repository.list_lane_context_entries(self.lane.id)
        )
        self.assertEqual(["user", "assistant"], [m.role for m in result.messages])
        self.assertEqual((user.id, assistant.id), result.included_entry_ids)


if __name__ == "__main__":
    unittest.main()
