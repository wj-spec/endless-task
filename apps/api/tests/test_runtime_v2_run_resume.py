"""E1 交付物 B/C：循环里的"进度感知"与"不确定副作用"的用户决定通道。

交付物 B（本批最容易自欺的一条）：**续跑时不许重复已完成的模型轮**。
判据不靠引用代码位置，靠两向行为：

* **已有已完成轮 ⇒ 不重做**：``execute`` 只发出"继续"这一次请求，且这一次请求的
  消息序列里**必须**带着已完成轮的工作（该轮的助手文本 + 工具结果）；已完成的工具
  执行**不再被重放**；局部计数器（用量/轮次/工具数）接着前任算。
* **没有已完成轮 ⇒ 正常从头做**：第一次请求只有触发消息，路径逐字不变。

交付物 C（E2）：崩溃时处于活动态的工具体"结果**无法确认**"——续跑**不重放**它，
并且**必须让用户看到**这件事（在既有 ``run_awaiting_user`` / 升级面上如实呈现），
既不许"默认重放"，也不许"默认跳过且不告知"。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2.escalation import (
    UNCERTAIN_SIDE_EFFECTS_REASON,
    UNCERTAIN_SIDE_EFFECTS_GUIDANCE,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    LaneKind,
    ModelTurnStatus,
    RunStatus,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.execution_impl.run_progress import (
    INTERRUPTED_REASON,
    collect_run_progress,
    is_uncertain_tool_execution,
    run_message_entries,
)
from endless_task.runtime_v2.user_profile import UserProfileBlock
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolDefinition, ToolEffect, ToolRegistry, ToolResult


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = [list(response) for response in responses]
        self.requests: list[ProviderRequest] = []

    async def stream(self, request, cancellation_token):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event
        cancellation_token.raise_if_cancelled()


class FakeTool:
    def __init__(
        self,
        name: str = "read_file",
        *,
        content: str = "file content",
        effect: ToolEffect = ToolEffect.READ_ONLY,
    ) -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fake {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            effect=effect,
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


class RunResumeTest(unittest.TestCase):
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

    def _persist_completed_first_turn(
        self,
        run_id: str,
        *,
        call_id: str = "call_1",
        tool_name: str = "read_file",
    ):
        """把"第 1 轮已完成"这件事按既有表落库（= 崩溃前那一刻的真实形态）。

        只用公开仓储 API，不伪造任何新结构：``start_run`` → ``start_model_turn`` →
        ``record_tool_call`` → ``record_tool_result`` → ``model_text_delta`` →
        ``transition_model_turn_status(COMPLETED)``。
        """
        self.repository.start_run(run_id)
        turn = self.repository.start_model_turn(
            run_id=run_id,
            provider="scripted",
            model="scripted-model",
        )
        record, _entry = self.repository.record_tool_call(
            model_turn_id=turn.id,
            call_id=call_id,
            tool_name=tool_name,
            arguments={"path": "a.txt"},
        )
        self.repository.record_tool_result(
            record.id,
            status=ToolExecutionStatus.COMPLETED,
            content="file content",
            event_type="tool_execution_completed",
        )
        self.repository.append_runtime_event(
            run_id=run_id,
            model_turn_id=turn.id,
            event_type="model_text_delta",
            payload={"delta": "我先读取文件。"},
        )
        self.repository.transition_model_turn_status(
            turn.id,
            ModelTurnStatus.COMPLETED,
            event_type="model_turn_completed",
            input_tokens=11,
            output_tokens=4,
        )
        return turn

    def _executor(self, provider, tool, **kwargs):
        registry = ToolRegistry()
        registry.register(tool)
        return AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            **kwargs,
        )

    def _flip_variant_off(self, conversation, run) -> None:
        """造**变体翻转**：同 ``sibling_group_id`` 的新活动变体让本 run 变成
        ``is_active_variant=0``（真实的 regenerate/resend 就是这么干的）。

        新变体放在**另一条 lane** 上：``idx_v2_runs_one_active_per_lane``
        （``036_runtime_v2.sql``）不许同 lane 同时有两条活动 run，而本 run 此刻还是
        RUNNING（= 崩溃前后、还没被收尾的形态）。sibling group 的唯一索引由
        ``create_run(is_active_variant=True)`` 自己把旧的压成 0 来满足。
        """
        other_lane = self.repository.create_lane(
            conversation_id=conversation.id,
            kind=LaneKind.PERSISTENT_BRANCH,
        )
        other_trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=other_lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "重新生成"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=other_lane.id,
            trigger_entry_id=other_trigger.id,
            sibling_group_id=run.sibling_group_id,
            is_active_variant=True,
        )

    # ------------------------------------------------------- 交付物 B：不重做

    def test_resume_reuses_completed_turns_instead_of_redoing_them(self) -> None:
        _conversation, _lane, _trigger, run = self._create_run()
        first_turn = self._persist_completed_first_turn(run.id)

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=13, output_tokens=6),
                )
            ]
        )
        tool = FakeTool()
        executor = self._executor(provider, tool)

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        # ① 只发出"继续"这一次请求：已完成的第 1 轮没有被重做一遍。
        self.assertEqual(1, len(provider.requests))
        request = provider.requests[0]
        self.assertEqual(
            ["user", "assistant", "tool"],
            [message.role for message in request.messages],
        )
        self.assertEqual("读取文件", request.messages[0].content)
        # ② 已完成的轮**在该进来的位置进来了**（ADR §7 / 评审 M5）：助手文本 + 工具结果。
        self.assertEqual("我先读取文件。", request.messages[1].content)
        self.assertEqual("call_1", request.messages[1].tool_calls[0].id)
        self.assertEqual("file content", request.messages[2].content)
        self.assertEqual("call_1", request.messages[2].tool_call_id)
        self.assertEqual("read_file", request.messages[2].name)
        # ③ 已完成的工具没有被重放（一次都没进工具实现）。
        self.assertEqual([], tool.calls)

        # ④ 新轮是 MAX+1（不是把第 1 轮再开一遍），且它的消息是"接着前任"发的。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(2, len(result.model_turn_ids))
        self.assertEqual(first_turn.id, result.model_turn_ids[0])
        new_turn = self.repository.get_model_turn(result.model_turn_ids[1])
        self.assertEqual(2, new_turn.turn_index)
        self.assertEqual(ModelTurnStatus.COMPLETED, new_turn.status)
        self.assertEqual(2, len(self.repository.list_model_turns(run.id)))

        # ⑤ 局部计数器接上前任：用量/文本都不从头算。
        self.assertEqual(24, result.input_tokens)
        self.assertEqual(10, result.output_tokens)
        self.assertEqual("我先读取文件。文件内容是 file content。", result.content)
        self.assertEqual(1, result.tool_call_count)

    def test_resume_keeps_entries_of_a_deactivated_variant(self) -> None:
        """红检查 ①：生产接线 ``include_variants=True``（ADR §7 / 评审 M5）。

        造出**变体翻转**：本 run 在同一个 ``sibling_group_id`` 下不再是活动变体
        （``is_active_variant=0``）。这时 ``list_entries(..., include_variants=False)``
        （**默认值**）会把本 run 自己写下的前任条目**静默剔除**——生产接线必须显式
        传 ``True``，否则续跑就丢掉"已完成的那一轮做过什么"。

        关键在**被测对象是生产调用点**（``AgentRunExecutor.execute`` 内部那行接线），
        不是把 ``include_variants=True`` 自己传进纯函数：后者
        （``test_run_message_entries_are_scoped_by_run_id``）永远抓不到这行接线，
        所以把生产行改成 ``False`` 时它照样绿（I-1 的原始发现）。
        """
        conversation, lane, _trigger, run = self._create_run()
        self._persist_completed_first_turn(run.id)
        self._flip_variant_off(conversation, run)
        self.assertFalse(self.repository.get_run(run.id).is_active_variant)

        # 前置事实：默认口径下本 run 的前任条目**真的**会被剔掉（不是假想的风险）。
        self.assertEqual(
            (),
            run_message_entries(
                self.repository.list_entries(lane.id),
                run_id=run.id,
            ),
        )

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=13, output_tokens=6),
                )
            ]
        )
        tool = FakeTool()
        executor = self._executor(provider, tool)

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        # 该进来的条目**一定**进来了：前任的助手文本 + 工具调用/结果都在重建序列里。
        self.assertEqual(1, len(provider.requests))
        request = provider.requests[0]
        self.assertEqual(
            ["user", "assistant", "tool"],
            [message.role for message in request.messages],
        )
        self.assertEqual("我先读取文件。", request.messages[1].content)
        self.assertEqual("call_1", request.messages[1].tool_calls[0].id)
        self.assertEqual("file content", request.messages[2].content)
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("我先读取文件。文件内容是 file content。", result.content)

    def test_resume_keeps_context_prefix_and_user_profile(self) -> None:
        """红检查 ②：生产接线 ``prefix_messages=head_messages``。

        续跑重建时如果把 ``prefix_messages`` 传成空，就会丢掉静态前缀
        （``context_prefix_messages``）与用户画像 system 消息——`head_messages`
        正是"投影消息之前那一段"（``runner.py`` 装配处），续跑必须原样带回去。
        """
        _conversation, _lane, _trigger, run = self._create_run()
        self._persist_completed_first_turn(run.id)

        prefix = ProviderMessage(role="system", content="平台静态前缀：只读不写。")
        profile = UserProfileBlock(
            content="用户画像：偏好中文回答。",
            version=3,
            signature="sig-3",
            lines=("用户画像：偏好中文回答。",),
        )
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=13, output_tokens=6),
                )
            ]
        )
        tool = FakeTool()
        executor = self._executor(
            provider,
            tool,
            context_prefix_messages=(prefix,),
            user_profile_provider=lambda conversation_id: profile,
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        request = provider.requests[0]
        self.assertEqual(
            ["system", "system", "user", "assistant", "tool"],
            [message.role for message in request.messages],
        )
        self.assertEqual(prefix.content, request.messages[0].content)
        self.assertEqual(profile.content, request.messages[1].content)
        self.assertEqual("读取文件", request.messages[2].content)
        self.assertEqual("我先读取文件。", request.messages[3].content)
        self.assertEqual("file content", request.messages[4].content)
        self.assertEqual(RunStatus.COMPLETED, result.status)

    def test_resume_counts_predecessor_confirmed_side_effects_as_evidence(self) -> None:
        """红检查 ③：生产接线 ``side_effect_evidence.extend(...)``。

        "前任**已确认**的副作用仍算这次运行的副作用"——验证闸门（``_maybe_verify``）
        看的正是这份证据，续跑把它清零等于假装没动过手。这条用例让继承来的证据
        **真的**撑起一次验证调用，并断言验证器收到的证据里就有前任那个工具。
        """
        _conversation, _lane, _trigger, run = self._create_run()
        # 前任第 1 轮做的是一个**有副作用**的外部动作（读文件不产生证据，钉不住这条）。
        self._persist_completed_first_turn(run.id, tool_name="send_message")

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("后续步骤完成。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=3, output_tokens=2),
                ),
                (
                    ProviderTextDelta(
                        '{"verdict": "pass", "reasons": ["证据齐全"], "missing": []}'
                    ),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool(name="send_message", effect=ToolEffect.LOCAL_WRITE)
        executor = self._executor(provider, tool, verifier_mode="side_effects")

        asyncio.run(executor.execute(run.id, cancellation_token=CancellationToken()))

        # ① 验证器**真的**被调了：证据非空 ⇒ `should_verify("side_effects")` 为真。
        verifying = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_verifying"
        ]
        self.assertEqual(1, len(verifying))
        self.assertEqual(2, len(provider.requests))
        # ② 证据里出现的正是**前任**那个已确认的副作用工具。
        verifier_messages = provider.requests[1].messages
        self.assertIn("## 本次运行的工具证据", verifier_messages[1].content)
        self.assertIn("- send_message（local_write）", verifier_messages[1].content)

    def test_run_without_completed_turns_starts_from_scratch(self) -> None:
        """反向：没有已完成轮（=今天绝大多数路径）时行为逐字不变。"""
        _conversation, _lane, _trigger, run = self._create_run()

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("我先读取文件。"),
                    ProviderToolCall(id="call_1", name="read_file", arguments={"path": "a.txt"}),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=11, output_tokens=4),
                ),
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=13, output_tokens=6),
                ),
            ]
        )
        tool = FakeTool()
        executor = self._executor(provider, tool)

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(2, len(provider.requests))
        # 第一次请求**只有**触发消息：没有凭空多出"前任"的消息。
        self.assertEqual(
            ["user"],
            [message.role for message in provider.requests[0].messages],
        )
        self.assertEqual("读取文件", provider.requests[0].messages[0].content)
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(24, result.input_tokens)
        self.assertEqual("我先读取文件。文件内容是 file content。", result.content)
        self.assertEqual(1, len(tool.calls))
        self.assertEqual(2, len(result.model_turn_ids))
        self.assertEqual(
            [1, 2],
            [self.repository.get_model_turn(turn_id).turn_index for turn_id in result.model_turn_ids],
        )


    # ------------------------------------------------- C：不确定副作用的通道

    def test_uncertain_tool_is_not_replayed_and_user_is_told(self) -> None:
        """崩溃时工具处于活动态 ⇒ 续跑**不重放**，且用户能看到"结果无法确认"。"""
        _conversation, _lane, _trigger, run = self._create_run()
        first_turn = self._persist_completed_first_turn(run.id)
        # 崩溃窗口：第 2 轮在飞，它的工具已被"批量执行之前"置成 RUNNING，结果没回来。
        second_turn = self.repository.start_model_turn(
            run_id=run.id,
            provider="scripted",
            model="scripted-model",
        )
        uncertain, _entry = self.repository.record_tool_call(
            model_turn_id=second_turn.id,
            call_id="call_2",
            tool_name="send_message",
            arguments={"text": "hello"},
        )
        self.repository.transition_tool_execution_status(
            uncertain.id,
            ToolExecutionStatus.RUNNING,
            event_type="tool_execution_started",
        )

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("这一步本不该发出去。"),
                    ProviderCompleted(finish_reason="stop"),
                )
            ]
        )
        tool = FakeTool(name="send_message")
        # 验证器故意开成"每次都验"：M-1 的断言要能抓住"谁给 E2 收尾又接上了
        # `_maybe_verify`"——开了模式还看不到验证器调用，才说明这条路径**故意**不验证。
        executor = self._executor(provider, tool, verifier_mode="1")

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        # ① 不重放：**一次 provider 调用都没有**，工具实现一次都没进。
        self.assertEqual([], provider.requests)
        self.assertEqual([], tool.calls)
        self.assertEqual(1, len(self.repository.list_tool_executions(second_turn.id)))
        self.assertEqual(
            ToolExecutionStatus.RUNNING,
            self.repository.get_tool_execution(uncertain.id).status,
        )
        # ①b M-1：这条收尾路径**故意**不调 `_maybe_verify`（理由见 runner.py 的实现处
        # 说明：E2 已经升级过，再验证会双升级，而验证器也答不了"副作用到底发生没有"）。
        # 谁把它接回去，这里就红。
        self.assertEqual(
            [],
            [
                event.event_type
                for event in self.repository.list_runtime_events(run.id)
                if event.event_type in ("run_verifying", "run_verified")
            ],
        )

        # ② 用户能看到：既有的 `run_awaiting_user` 事件，reason 如实、文案说清"无法确认"。
        escalation = self._awaiting_user_events(run.id)
        self.assertEqual(1, len(escalation))
        report = escalation[0]
        self.assertEqual(UNCERTAIN_SIDE_EFFECTS_REASON, report["reason"])
        self.assertTrue(report["willStop"])
        self.assertIn("无法确认", report["summary"])
        self.assertIn("send_message", report["summary"])
        self.assertEqual(UNCERTAIN_SIDE_EFFECTS_GUIDANCE, report["guidance"])
        self.assertIn("没有重放", report["guidance"])

        # ③ 运行在这里收尾（COMPLETED + 如实引导语），没有静默继续。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertIn("无法确认", result.content)
        self.assertIn("E 不支持自动重放", result.content)
        self.assertEqual((first_turn.id,), result.model_turn_ids)
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(run.id).status)
        # 没有新建任何模型轮（不重放 ⇒ 也不为该轮开新轮）。
        self.assertEqual(2, len(self.repository.list_model_turns(run.id)))

        # ④ I-2（口径 b）：终态 run 下**故意保留**两个活跃子行——它们是"可能已产生
        # 副作用 / 结果无法确认"这份信息的唯一载体，任何路径都不会去收尾它们
        # （`run_sweep` 只挑活动 run；`finalize_interrupted_run` 对终态 run 直接拒）。
        # 这条断言是**故意保留**的钉子：谁"顺手"把它们压平，这里就红。
        self.assertEqual(
            ModelTurnStatus.PROJECTING_CONTEXT,
            self.repository.get_model_turn(second_turn.id).status,
        )
        self.assertEqual(
            ToolExecutionStatus.RUNNING,
            self.repository.get_tool_execution(uncertain.id).status,
        )

        # ⑤ 但"终态 run 不该自称可续"：进度读取器必须对已定案的 run 报 resumable=False
        # （口径 b 的判据修复）。同时"不确定清单"不许丢——E3 靠它决定怎么续。
        progress = collect_run_progress(repository=self.repository, run_id=run.id)
        self.assertFalse(progress.resumable)
        self.assertEqual(
            [uncertain.id],
            [view.execution_id for view in progress.uncertain_tools],
        )

    def test_uncertain_tool_is_recognised_after_crash_flattening(self) -> None:
        """崩溃收尾把四个活动态压平成同一个 FAILED/retryable=0 之后，仍要认出来。"""
        _conversation, _lane, _trigger, run = self._create_run()
        self._persist_completed_first_turn(run.id)
        second_turn = self.repository.start_model_turn(
            run_id=run.id,
            provider="scripted",
            model="scripted-model",
        )
        uncertain, _entry = self.repository.record_tool_call(
            model_turn_id=second_turn.id,
            call_id="call_2",
            tool_name="send_message",
            arguments={"text": "hello"},
        )
        self.repository.transition_tool_execution_status(
            uncertain.id,
            ToolExecutionStatus.RUNNING,
            event_type="tool_execution_started",
        )
        # = 崩溃 + 重启清扫（`finalize_interrupted_run`）之后的真实形态：行上再也不可
        # 区分"未调用"与"已调用但结果未知"。
        self.repository.finalize_interrupted_run(run.id)

        flattened = self.repository.get_tool_execution(uncertain.id)
        self.assertEqual(ToolExecutionStatus.FAILED, flattened.status)
        self.assertEqual(0, flattened.retryable)
        self.assertEqual(INTERRUPTED_REASON, flattened.error_details["reason"])
        self.assertTrue(is_uncertain_tool_execution(flattened))
        progress = collect_run_progress(repository=self.repository, run_id=run.id)
        self.assertEqual(
            [uncertain.id],
            [tool.execution_id for tool in progress.uncertain_tools],
        )
        # 结果无法确认 ⇒ 不重放；今天这条续跑入口被状态机拒绝（终态 run），
        # E3 才放开——判据本身在这里可验证。
        self.assertEqual(RunStatus.FAILED, self.repository.get_run(run.id).status)

    def _awaiting_user_events(self, run_id: str) -> list[dict[str, Any]]:
        return [
            event.payload
            for event in self.repository.list_runtime_events(run_id)
            if event.event_type == "run_awaiting_user"
        ]


if __name__ == "__main__":
    unittest.main()
