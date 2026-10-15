"""E3/B：把预算硬停后的终态 run **原地复活**（同一条 run 接着跑）。

本文件钉住四类事实，每一类都**两向**（能成 / 不能成都要指名）：

1. **复活成功且 run id 不变** —— 这是本批的核心断言：`revive_terminal_run` 把
   ``COMPLETED + awaiting_user(willStop=true)`` 改回 ``running``，**同一行、同一 id**，
   并发一条**同 run** 的恢复事件（否则旧的硬停横幅不会过期：后端只认"**别的** run 的
   `run.started`"，见 `gateway_impl/views.py` 的 `_awaiting_user_scope`）。
2. **不该复活的一律指名拒绝**（409 + `code=run_not_revivable`，**不是 500**，也不靠
   `idx_v2_runs_one_active_per_lane` 报错兜底）：`FAILED` / `CANCELLED` / 仍活动态 /
   没有硬停升级 / **该 lane 另有活动 run** / 有"结果无法确认"的工具体
   （E2 通道负责，复活会立刻再停）。
3. **不重复已完成的模型轮** —— 复活的执行走 E1 的进度感知：只发一次"继续"请求，
   该请求带着前任已完成轮的助手文本与工具结果，已完成的工具**一次都不重放**，
   新轮的 `turn_index` 是 MAX+1（不是把第 1 轮再开一遍）。
4. **口径**：复活 = **新的一段**（`started_at` 改写、`finished_at` 清空 ⇒ 停机时长
   **不计入**"已用秒"；旧的一段写进事件 payload）；用量**不许假装连续** ——
   新的一段由执行器**重新声明** `run_budget_declared`（快照取最后一条）。

恢复前提与拒绝原因由 ``runtime_v2.revive`` 统一判定。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Sequence

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.repositories import ConflictError, RunNotRevivableError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime.provider_manager import SelectedProvider
from endless_task.runtime_v2 import (
    Actor,
    LaneKind,
    ModelTurnStatus,
    RunStatus,
    RuntimeV2SessionGateway,
    ToolExecutionStatus,
    TranscriptEntryType,
)
from endless_task.runtime_v2.escalation import (
    TIME_REASON,
    EscalationBudget,
    EscalationProgress,
    build_escalation_report,
)
from endless_task.runtime_v2.revive import (
    REFUSAL_LANE_HAS_OTHER_ACTIVE_RUN,
    REFUSAL_NO_HARD_STOP,
    REFUSAL_RUN_NOT_COMPLETED,
    REFUSAL_UNCERTAIN_SIDE_EFFECTS,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import ToolDefinition, ToolEffect, ToolRegistry, ToolResult


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = [list(response) for response in responses]
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("No scripted provider response remains")
        events = self.responses.pop(0)
        for event in events:
            cancellation_token.raise_if_cancelled()
            yield event


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
        self.calls: list[object] = []

    async def execute(self, call, cancellation_token):
        del cancellation_token
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content=self.content)

    def requires_explicit_confirmation(self, call) -> bool:
        del call
        return False


class _Clock:
    """可控时钟（毫秒 ISO，与 `storage/v2/base._utc_now` 同形）。

    默认**以真实 now 起锚**：快照侧的"已用秒"是 `started_at → now` 的活值，而 `now`
    来自真实时钟（`views._elapsed_seconds`），所以断言"停机不计入已用"必须让存进库的
    时间戳与真实 now **可比**（否则差出几小时，"新的一段"就看不出来了）。
    """

    def __init__(self, start: str | None = None) -> None:
        self.current = start or (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )

    def __call__(self) -> str:
        return self.current

    def offset(self, seconds: float) -> str:
        """当前时刻 + seconds 的 ISO（**不改**当前时刻）。"""
        moment = datetime.fromisoformat(self.current) + timedelta(seconds=seconds)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def advance_to(self, value: str) -> None:
        self.current = value


def _hard_stop_payload(*, elapsed_seconds: float = 900.0) -> dict[str, object]:
    """生产里那条 `run_awaiting_user` 的 payload（`_finalize_time_budget` 发的形状）。"""
    return build_escalation_report(
        reason=TIME_REASON,
        progress=EscalationProgress(
            model_turns=1,
            tool_calls=1,
            produced_characters=7,
            input_tokens=11,
            output_tokens=4,
            elapsed_seconds=elapsed_seconds,
        ),
        budget=EscalationBudget(
            used_tokens=15,
            limit_tokens=None,
            used_seconds=elapsed_seconds,
            limit_seconds=900.0,
        ),
        will_stop=True,
    ).as_json()


class _RunFixtureMixin:
    """只用**公开仓储 API** 造出"预算硬停"那一刻的真实形态。"""

    repository: SqliteRuntimeV2Repository
    chat_repository: SqliteChatRepository

    def _create_run(self, *, content: str = "读取文件"):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": content},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
            is_active_variant=True,
        )
        return conversation, lane, trigger, run

    def _persist_completed_turn(self, run_id: str, *, call_id: str = "call_1"):
        turn = self.repository.start_model_turn(
            run_id=run_id,
            provider="scripted",
            model="scripted-model",
        )
        record, _entry = self.repository.record_tool_call(
            model_turn_id=turn.id,
            call_id=call_id,
            tool_name="read_file",
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

    def _advance_clock(self, value: str) -> None:
        clock = getattr(self, "clock", None)
        if clock is not None:
            clock.advance_to(value)

    def _hard_stop(
        self,
        run_id: str,
        *,
        started: str | None = None,
        finished: str | None = None,
        inflight_tool: bool = False,
    ):
        """先升级（willStop=true）、后收尾 COMPLETED —— 与生产顺序一致。

        ``inflight_tool=True`` 额外造一个"结果无法确认"的工具体（E2 形态）：真实的
        时间墙超时**可能**发生在工具执行中间（`_finalize_time_budget` 的 TimeoutError
        分支不检查工具体），收尾时那些行**故意留在活动态**。
        """
        if started is not None:
            self._advance_clock(started)
        self.repository.start_run(run_id)
        self._persist_completed_turn(run_id)
        uncertain_tool_row = None
        if inflight_tool:
            second_turn = self.repository.start_model_turn(
                run_id=run_id,
                provider="scripted",
                model="scripted-model",
            )
            uncertain_tool_row, _entry = self.repository.record_tool_call(
                model_turn_id=second_turn.id,
                call_id="call_2",
                tool_name="send_message",
                arguments={"text": "hello"},
            )
            self.repository.transition_tool_execution_status(
                uncertain_tool_row.id,
                ToolExecutionStatus.RUNNING,
                event_type="tool_execution_started",
            )
        if finished is not None:
            self._advance_clock(finished)
        self.repository.append_runtime_event(
            run_id=run_id,
            event_type="run_budget_declared",
            payload={
                "softSeconds": 180.0,
                "hardSeconds": 900.0,
                "perTurnSeconds": 120.0,
                "costCapUsd": 0.0,
                "stopOnCost": False,
            },
        )
        self.repository.append_runtime_event(
            run_id=run_id,
            event_type="run_awaiting_user",
            payload=_hard_stop_payload(),
        )
        final_run, entry = self.repository.finalize_run(
            run_id,
            content="我先读取文件。\n\n[时间到墙，已安全停止]",
            finish_reason="time_budget_stop",
            input_tokens=11,
            output_tokens=4,
        )
        self.assertEqual(RunStatus.COMPLETED, final_run.status)
        return final_run, entry, uncertain_tool_row


class RunReviveRepositoryTest(_RunFixtureMixin, unittest.TestCase):
    """仓储行闸 + 口径（同步；时钟可控 ⇒ 秒数可断言）。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "revive.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.clock = _Clock(start="2026-09-15T10:00:00.000Z")
        self.repository = SqliteRuntimeV2Repository(self.database, clock=self.clock)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    # ------------------------------------------------ ① 复活成功：同一条 run

    def test_revive_keeps_same_run_id_and_opens_a_new_segment(self) -> None:
        _conversation, _lane, _trigger, run = self._create_run()
        # 第 1 段：10:00:00 → 10:15:00 硬停。
        hard_stopped, entry, _uncertain = self._hard_stop(
            run.id,
            started="2026-09-15T10:00:00.000Z",
            finished="2026-09-15T10:15:00.000Z",
        )
        # 硬停收尾发生在 10:15（`finalize_run` 用的是那一刻的时钟）。
        self.assertEqual(
            "2026-09-15T10:15:00.000Z",
            self.repository.get_run(run.id).finished_at,
        )

        # 停机 5 分钟后用户点「继续」。
        self.clock.advance_to("2026-09-15T10:20:00.000Z")
        revived = self.repository.revive_terminal_run(run.id)

        # ① 核心断言：**同一条 run**，id 不变，只是状态回到 running。
        self.assertEqual(run.id, revived.id)
        self.assertEqual(RunStatus.RUNNING, revived.status)
        self.assertEqual(run.trigger_entry_id, revived.trigger_entry_id)
        self.assertEqual(run.sibling_group_id, revived.sibling_group_id)
        self.assertTrue(revived.is_active_variant)
        # ② **新的一段**：started_at 改写、finished_at 清空 ⇒ 停机 5 分钟不计入"已用"。
        self.assertEqual("2026-09-15T10:20:00.000Z", revived.started_at)
        self.assertIsNone(revived.finished_at)
        # ③ 旧的一段没有被抹掉：进了恢复事件的 payload（审计可回溯、不假装连续）。
        resumed = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_progress_resumed"
        ]
        self.assertEqual(1, len(resumed))
        payload = resumed[0].payload
        self.assertEqual(run.id, payload["runId"])
        self.assertEqual("hard_stop_revive", payload["source"])
        self.assertEqual("2026-09-15T10:00:00.000Z", payload["previousStartedAt"])
        self.assertEqual("2026-09-15T10:15:00.000Z", payload["previousFinishedAt"])
        self.assertEqual(900.0, payload["previousSegmentSeconds"])
        # ④ 已完成轮的行与用量一个字都没动（复活不重置进度）。
        turns = self.repository.list_model_turns(run.id)
        self.assertEqual(1, len(turns))
        self.assertEqual(ModelTurnStatus.COMPLETED, turns[0].status)
        self.assertEqual(11, turns[0].input_tokens)
        self.assertEqual(4, turns[0].output_tokens)
        self.assertIsNotNone(entry.id)
        del hard_stopped

    def test_revive_makes_the_run_the_active_variant_again(self) -> None:
        conversation, _lane, _trigger, run = self._create_run()
        self._hard_stop(run.id)
        # 同 sibling group 的**另一条 lane** 上的新变体把本 run 压成非活动变体
        # （`create_run(is_active_variant=True)` 自己压的，与 regenerate 同口径）。
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
        other_run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=other_lane.id,
            trigger_entry_id=other_trigger.id,
            sibling_group_id=run.sibling_group_id,
            is_active_variant=True,
        )
        self.assertFalse(self.repository.get_run(run.id).is_active_variant)

        revived = self.repository.revive_terminal_run(run.id)

        # 复活的是"这一条"⇒ 它重新成为活动变体，同组兄弟被压回 0（与 start_run 同口径）。
        self.assertTrue(revived.is_active_variant)
        self.assertFalse(self.repository.get_run(other_run.id).is_active_variant)
        variants = self.repository.list_run_variants(run.id)
        self.assertEqual(
            [run.id], [item.id for item in variants if item.is_active_variant]
        )

    # ------------------------------------------------ ② 不该复活：指名拒绝

    def test_revive_refuses_failed_cancelled_and_active_runs(self) -> None:
        # FAILED
        _conversation, _lane, _trigger, failed_run = self._create_run()
        self.repository.start_run(failed_run.id)
        self.repository.transition_run_status(
            failed_run.id,
            RunStatus.FAILED,
            event_type="run_failed",
            error_code="provider_error",
            safe_message="模型调用失败。",
        )
        with self.assertRaises(RunNotRevivableError) as failed_ctx:
            self.repository.revive_terminal_run(failed_run.id)
        self.assertIn(REFUSAL_RUN_NOT_COMPLETED, str(failed_ctx.exception))
        self.assertIn("failed", str(failed_ctx.exception))
        self.assertEqual(
            RunStatus.FAILED, self.repository.get_run(failed_run.id).status
        )

        # CANCELLED
        _conversation2, _lane2, _trigger2, cancelled_run = self._create_run()
        self.repository.start_run(cancelled_run.id)
        self.repository.transition_run_status(
            cancelled_run.id,
            RunStatus.CANCELLED,
            event_type="run_cancelled",
            cancelled_by="user",
        )
        with self.assertRaises(RunNotRevivableError) as cancelled_ctx:
            self.repository.revive_terminal_run(cancelled_run.id)
        self.assertIn(REFUSAL_RUN_NOT_COMPLETED, str(cancelled_ctx.exception))

        # 还在活动态（running）
        _conversation3, _lane3, _trigger3, running_run = self._create_run()
        self.repository.start_run(running_run.id)
        with self.assertRaises(RunNotRevivableError) as running_ctx:
            self.repository.revive_terminal_run(running_run.id)
        self.assertIn(REFUSAL_RUN_NOT_COMPLETED, str(running_ctx.exception))
        self.assertEqual(
            RunStatus.RUNNING, self.repository.get_run(running_run.id).status
        )

    def test_revive_refuses_completed_run_without_hard_stop_escalation(self) -> None:
        _conversation, _lane, _trigger, run = self._create_run()
        self.repository.start_run(run.id)
        self.repository.finalize_run(
            run.id,
            content="正常完成。",
            finish_reason="stop",
            input_tokens=3,
            output_tokens=2,
        )
        with self.assertRaises(RunNotRevivableError) as ctx:
            self.repository.revive_terminal_run(run.id)
        self.assertIn(REFUSAL_NO_HARD_STOP, str(ctx.exception))
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(run.id).status)

    def test_revive_refuses_when_lane_has_another_active_run(self) -> None:
        _conversation, lane, _trigger, run = self._create_run()
        self._hard_stop(run.id)
        # 本 run 已终态 ⇒ 同 lane 可以再有活动 run（036 的部分唯一索引只约束活动态）。
        other_trigger = self.repository.append_entry(
            conversation_id=lane.conversation_id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "另一个请求"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        other_run = self.repository.create_run(
            conversation_id=lane.conversation_id,
            lane_id=lane.id,
            trigger_entry_id=other_trigger.id,
        )
        self.repository.start_run(other_run.id)

        with self.assertRaises(RunNotRevivableError) as ctx:
            self.repository.revive_terminal_run(run.id)
        self.assertIn(REFUSAL_LANE_HAS_OTHER_ACTIVE_RUN, str(ctx.exception))
        self.assertIn(other_run.id, str(ctx.exception))
        # 拒绝必须是**纯读**：两边状态都不受影响，也没有偷偷发恢复事件。
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(run.id).status)
        self.assertEqual(
            RunStatus.RUNNING, self.repository.get_run(other_run.id).status
        )
        self.assertEqual(
            [],
            [
                event.event_type
                for event in self.repository.list_runtime_events(run.id)
                if event.event_type == "run_progress_resumed"
            ],
        )


class RunReviveGatewayTest(_RunFixtureMixin, unittest.IsolatedAsyncioTestCase):
    """经网关的复活：真的接着跑（不重复已完成的轮）+ 不确定副作用挡在门口。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "gateway.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.clock = _Clock()
        self.repository = SqliteRuntimeV2Repository(self.database, clock=self.clock)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _gateway(self, provider, tool=None, **kwargs) -> RuntimeV2SessionGateway:
        registry = ToolRegistry()
        registry.register(tool or FakeTool())
        return RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            max_output_tokens=128,
            **kwargs,
        )

    async def test_resume_continues_same_run_without_redoing_completed_turn(self) -> None:
        conversation, lane, _trigger, run = self._create_run()
        self.repository.set_conversation_pointer(
            conversation_id=conversation.id,
            active_lane_id=lane.id,
            active_run_id=run.id,
            active_run_variant_id=run.id,
        )
        # 第 1 段：起锚 → +900s 硬停。停机到复活之间又过了 300s。
        started = self.clock.current
        finished = self.clock.offset(900)
        self._hard_stop(run.id, started=started, finished=finished)

        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop", input_tokens=13, output_tokens=6
                    ),
                )
            ]
        )
        tool = FakeTool()
        gateway = self._gateway(provider, tool)
        # 前置事实：硬停后快照给出的"已用"是**旧的那一段**（900s）。
        self.assertEqual(
            900.0, snapshot_budget_used_seconds(gateway, conversation.id)
        )

        self.clock.advance_to(self.clock.offset(1200))
        result = await gateway.resolve_recovery(run.id, retry=True)

        # ① 同一个 run id（**不是**新开一轮）、newRunId 为空、动作如名。
        self.assertEqual(run.id, result.run_id)
        self.assertEqual("resumed", result.action)
        self.assertIsNone(result.new_run_id)
        await self._wait_until(
            lambda: self.repository.get_run(run.id).status is RunStatus.COMPLETED
        )
        self.assertEqual(
            RunStatus.COMPLETED, self.repository.get_run(run.id).status
        )
        # ② 只发出"继续"这一次请求：已完成的第 1 轮没有被重做，工具也没被重放。
        self.assertEqual(1, len(provider.requests))
        self.assertEqual([], tool.calls)
        request = provider.requests[0]
        self.assertEqual(
            ["user", "assistant", "tool"],
            [message.role for message in request.messages],
        )
        self.assertEqual("我先读取文件。", request.messages[1].content)
        self.assertEqual("file content", request.messages[2].content)
        turns = self.repository.list_model_turns(run.id)
        self.assertEqual(2, len(turns))
        self.assertEqual(2, turns[1].turn_index)
        self.assertEqual(11 + 13, turns[0].input_tokens + turns[1].input_tokens)
        # ⑥ 同 run 的恢复事件 ⇒ 旧的硬停横幅过期（后端 `_escalation_json` /
        # 前端 `deriveEscalationState` 都按"同一个 run"清空）。
        resumed_events = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_progress_resumed"
        ]
        self.assertEqual(1, len(resumed_events))
        # ④ 口径（视图级、可判别）：同一条 run 的"已用"在复活后**只数新的一段** ——
        #    旧段 900s ⇒ 复活后 0s（同一份冻结时钟：停机那 300s 一秒都没算进去）。
        self.assertEqual(
            0.0, snapshot_budget_used_seconds(gateway, conversation.id)
        )
        # ⑤ 新的一段**重新声明**预算墙（不许假装总预算连续）：第一条是硬停前的旧声明，
        #    复活后执行器又发了一条（快照取**最后**一条 ⇒ 剩余秒数按新段算）。
        declarations = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_budget_declared"
        ]
        self.assertEqual(2, len(declarations))
        # 第二条声明**晚于**复活事件 ⇒ 它声明的是新的一段（而不是把旧的当成连续）。
        self.assertGreater(
            declarations[-1].event_seq,
            resumed_events[0].event_seq,
        )
        await gateway.shutdown()

    async def test_resume_refuses_run_with_uncertain_tool_executions(self) -> None:
        """③：E2 的形态不许复活（复活会**立刻**再停 ⇒ 对用户是空转）。"""
        _conversation, _lane, _trigger, run = self._create_run()
        # 硬停那一刻手上还有一个"结果无法确认"的工具（行故意留在活动态）。
        _final_run, _entry, uncertain = self._hard_stop(run.id, inflight_tool=True)
        self.assertIsNotNone(uncertain)

        provider = ScriptedProvider([])
        gateway = self._gateway(provider)

        with self.assertRaises(RunNotRevivableError) as ctx:
            await gateway.resolve_recovery(run.id, retry=True)
        self.assertIn(REFUSAL_UNCERTAIN_SIDE_EFFECTS, str(ctx.exception))
        # 拒绝是纯读：状态没变、没有模型调用、没有恢复事件、工具体也没被动过。
        self.assertEqual(
            RunStatus.COMPLETED, self.repository.get_run(run.id).status
        )
        self.assertEqual([], provider.requests)
        self.assertEqual(
            ToolExecutionStatus.RUNNING,
            self.repository.get_tool_execution(uncertain.id).status,
        )
        self.assertEqual(
            [],
            [
                event.event_type
                for event in self.repository.list_runtime_events(run.id)
                if event.event_type == "run_progress_resumed"
            ],
        )
        await gateway.shutdown()

    async def test_resume_launch_failure_does_not_leave_a_fake_running(self) -> None:
        """启动接线失败 ⇒ **绝不留**一条没有任务在跑的 running（本批要消灭的形态）。"""

        def exploding_resolver(_conversation):
            raise RuntimeError("provider resolution exploded")

        _conversation, _lane, _trigger, run = self._create_run()
        self._hard_stop(run.id)
        provider = ScriptedProvider([])
        gateway = self._gateway(provider, provider_resolver=exploding_resolver)

        with self.assertRaises(ConflictError) as ctx:
            await gateway.resolve_recovery(run.id, retry=True)
        self.assertIn("revive_launch_failed", str(ctx.exception))
        final_run = self.repository.get_run(run.id)
        self.assertEqual(RunStatus.FAILED, final_run.status)
        self.assertEqual("revive_launch_failed", final_run.error_code)
        await gateway.shutdown()

    async def _wait_until(self, predicate, *, attempts: int = 300) -> None:
        for _ in range(attempts):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition was not met in time")


class RunReviveApiTest(_RunFixtureMixin, unittest.IsolatedAsyncioTestCase):
    """HTTP 面：`action="resume"` 是"同一条 run 接着跑"；拒绝是**指名** 409。"""

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "api.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _client(self, *, provider):
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._temporary_directory.name) / "api.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                scheduler_enabled=False,
                knowledge_decay_enabled=False,
                notifications_enabled=False,
                task_run_review_enabled=False,
                heartbeat_seconds=0.05,
            ),
            provider=provider,
            tool_registry=ToolRegistry(),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        # 用**容器里那一个**仓储造形态（与 HTTP 面共享同一个库与时钟）。
        self.repository = app.state.container.runtime_v2_repository
        self.chat_repository = app.state.container.chat_repository
        return client, app

    def _point_at(self, conversation_id: str, lane_id: str, run_id: str) -> None:
        """把会话指针指向这条 lane/run（快照要走指针那条路径，否则返回空形态）。"""
        self.repository.set_conversation_pointer(
            conversation_id=conversation_id,
            active_lane_id=lane_id,
            active_run_id=run_id,
            active_run_variant_id=run_id,
        )

    async def _snapshot(self, client, conversation_id: str) -> dict:
        response = await client.get(
            f"/api/v2/conversations/{conversation_id}/snapshot"
        )
        self.assertEqual(200, response.status_code)
        return response.json()

    async def test_resume_action_revives_same_run_and_clears_the_banner(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop", input_tokens=13, output_tokens=6
                    ),
                )
            ]
        )
        client, _app = await self._client(provider=provider)
        conversation, lane, _trigger, run = self._create_run()
        self._point_at(conversation.id, lane.id, run.id)
        self._hard_stop(run.id)

        # ① 硬停形态：横幅在场、预算按**旧的那一段**算，且后端判它可复活。
        before = await self._snapshot(client, conversation.id)
        self.assertEqual(run.id, before["escalation"]["runId"])
        self.assertTrue(before["escalation"]["willStop"])
        self.assertIsNone(before["runningRunId"])
        self.assertEqual(run.id, before["revivableRunId"])
        # 横幅自报的"已用时"就是硬停那一段（payload 里的 900s，不是活值）。
        self.assertEqual(900.0, before["escalation"]["budget"]["usedSeconds"])
        self.assertEqual(900.0, before["escalation"]["progress"]["elapsedSeconds"])

        # ② 「继续」= `action="resume"` ⇒ **同一条 run** 复活（不是新开一轮）。
        response = await client.post(
            f"/api/v2/runs/{run.id}/recovery",
            json={"action": "resume"},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {
                "runId": run.id,
                "action": "resumed",
                "laneId": lane.id,
                "newRunId": None,
            },
            response.json(),
        )

        for _ in range(300):
            snapshot = await self._snapshot(client, conversation.id)
            if snapshot["runState"] and snapshot["runState"]["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        final = await self._snapshot(client, conversation.id)
        # ③ 同一条 run 接着跑完：run id 不变、模型轮接了前任（1 → 2）。
        self.assertEqual(run.id, final["runState"]["runId"])
        self.assertEqual("completed", final["runState"]["status"])
        self.assertEqual(2, len(final["runState"]["modelTurns"]))
        # ④ 横幅过期：同 run 的恢复事件让升级投影消失；可复活指针也清了。
        self.assertIsNone(final["escalation"])
        self.assertIsNone(final["revivableRunId"])
        # ⑤ 口径：新的一段从复活时刻起算 ⇒ 停机那 900 秒**不计入**"已用"。
        self.assertLess(final["runState"]["budget"]["usedSeconds"], 60.0)
        # ⑥ 只有一个模型请求（前任那轮没有被重做）。
        self.assertEqual(1, len(provider.requests))

    async def test_resume_action_refuses_named_shapes_with_409(self) -> None:
        provider = ScriptedProvider([])
        client, _app = await self._client(provider=provider)

        # FAILED ⇒ `run_not_completed`
        _conversation, _lane, _trigger, failed_run = self._create_run()
        self.repository.start_run(failed_run.id)
        self.repository.transition_run_status(
            failed_run.id,
            RunStatus.FAILED,
            event_type="run_failed",
            error_code="provider_error",
            safe_message="模型调用失败。",
        )
        response = await client.post(
            f"/api/v2/runs/{failed_run.id}/recovery",
            json={"action": "resume"},
        )
        self.assertEqual(409, response.status_code)
        body = response.json()["error"]
        self.assertEqual("run_not_revivable", body["code"])
        self.assertIn(REFUSAL_RUN_NOT_COMPLETED, body["message"])

        # COMPLETED 但**没有**硬停升级 ⇒ `no_hard_stop`
        _conversation2, lane2, _trigger2, plain_run = self._create_run()
        self.repository.start_run(plain_run.id)
        self.repository.finalize_run(
            plain_run.id,
            content="正常完成。",
            finish_reason="stop",
            input_tokens=3,
            output_tokens=2,
        )
        response = await client.post(
            f"/api/v2/runs/{plain_run.id}/recovery",
            json={"action": "resume"},
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("run_not_revivable", response.json()["error"]["code"])
        self.assertIn(REFUSAL_NO_HARD_STOP, response.json()["error"]["message"])

        # 该 lane 另有活动 run ⇒ `lane_has_other_active_run`
        _conversation3, lane3, _trigger3, hard_run = self._create_run()
        self._hard_stop(hard_run.id)
        other_trigger = self.repository.append_entry(
            conversation_id=lane3.conversation_id,
            lane_id=lane3.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "另一个请求"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        other_run = self.repository.create_run(
            conversation_id=lane3.conversation_id,
            lane_id=lane3.id,
            trigger_entry_id=other_trigger.id,
        )
        self.repository.start_run(other_run.id)
        response = await client.post(
            f"/api/v2/runs/{hard_run.id}/recovery",
            json={"action": "resume"},
        )
        self.assertEqual(409, response.status_code)
        self.assertIn(
            REFUSAL_LANE_HAS_OTHER_ACTIVE_RUN,
            response.json()["error"]["message"],
        )
        # 拒绝发生在任何启动之前：硬的形态只有前任那一轮，**没有**新的模型轮。
        self.assertEqual(1, len(self.repository.list_model_turns(hard_run.id)))
        self.assertEqual(
            RunStatus.COMPLETED, self.repository.get_run(hard_run.id).status
        )


def snapshot_budget_used_seconds(gateway, conversation_id: str) -> float:
    """用**真实快照投影**读"这条 run 已用多少秒"（`runState.budget.usedSeconds`）。

    这正是前端预算条看到的那个数：`started_at → now` 的活值，墙来自执行器**最后**
    一条 `run_budget_declared`。走 `gateway.snapshot` 而不是自己拼投影输入，是为了让
    断言的被测对象就是生产接线。
    """
    snapshot = gateway.snapshot(conversation_id)
    run_state = snapshot["runState"]
    assert isinstance(run_state, dict)
    budget = run_state["budget"]
    assert isinstance(budget, dict)
    return float(budget["usedSeconds"])


if __name__ == "__main__":
    unittest.main()
