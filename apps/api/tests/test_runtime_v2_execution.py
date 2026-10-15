from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, AsyncIterator, Sequence
from unittest import mock

from endless_task.domain.repositories import NotFoundError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.fake_provider import FakeProvider
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    ContextCompactionResult,
    ContextProjection,
    ModelTurnStatus,
    RunStatus,
    RuntimeV2ReplayService,
    StaticToolApprovalGate,
    ToolApprovalDecision,
    ToolExecutionLimits,
    ToolExecutionStatus,
    TranscriptEntryType,
    UnattendedToolApprovalGate,
)
from endless_task.runtime_v2.escalation import TIME_REASON as RTIME
from endless_task.runtime_v2.run_budget import RunBudget
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.storage.v2.base import ACTIVE_RUN_STATUSES
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolRegistry,
    ToolResult,
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]]) -> None:
        self.responses = list(responses)
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
        cancellation_token.raise_if_cancelled()


class PausedProvider:
    name = "paused"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.paused = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderTextDelta("部分")
        self.paused.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class HangingProvider:
    """永不返回、也不理会取消令牌的 Provider：模拟宿主取消（任务取消）。"""

    name = "hanging"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.entered = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del cancellation_token
        self.requests.append(request)
        yield ProviderTextDelta("开始")
        self.entered.set()
        # 挂住，直到被宿主取消（CancelledError），永远不主动返回。
        await asyncio.Event().wait()


class RepeatingReadProvider:
    """无限生成只读工具调用的 Provider，用于验证墙钟 watchdog 兜底。"""

    name = "repeating"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        yield ProviderToolCall(
            id=f"call_{len(self.requests)}",
            name="read_file",
            arguments={"path": "a.txt"},
        )
        yield ProviderCompleted(finish_reason="tool_calls")
        cancellation_token.raise_if_cancelled()


class FakeTool:
    def __init__(
        self,
        name: str = "read_file",
        *,
        content: str = "file content",
        structured_content: Any = None,
        delay: float = 0.0,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        approval_mode: ToolApprovalMode = ToolApprovalMode.AUTO,
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
            approval_mode=approval_mode,
            timeout_seconds=2.0,
        )
        self.content = content
        self.structured_content = structured_content
        self.delay = delay
        self.calls: list[ToolCall] = []
        self.started_at: list[float] = []
        self.finished_at: list[float] = []
        self.active_calls = 0
        self.max_active_calls = 0

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        self.started_at.append(asyncio.get_running_loop().time())
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            await asyncio.sleep(self.delay)
            return ToolResult(
                tool_call_id=call.id,
                content=self.content,
                structured_content=self.structured_content,
            )
        finally:
            self.active_calls -= 1
            self.finished_at.append(asyncio.get_running_loop().time())

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        return False


class ReadOnlyToolWithoutConfirmationJudge:
    """读工具的真实形态：不定义 requires_explicit_confirmation（继承自 Protocol）。"""

    def __init__(self, name: str = "list_workspace_dir") -> None:
        self.definition = ToolDefinition(
            name=name,
            description=f"Fake {name}",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            effect=ToolEffect.READ_ONLY,
            approval_mode=ToolApprovalMode.AUTO,
            timeout_seconds=2.0,
        )
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del cancellation_token
        self.calls.append(call)
        return ToolResult(tool_call_id=call.id, content="listing")


class PausedTool(FakeTool):
    def __init__(self) -> None:
        super().__init__("paused_read")
        self.paused = asyncio.Event()

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        self.calls.append(call)
        self.paused.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class CancellationDuringPreparationTool(FakeTool):
    def __init__(self, token: CancellationToken) -> None:
        super().__init__("cancel_during_prepare")
        self._token = token

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        del call
        self._token.cancel()
        return False


class NormalizedFailureTool:
    def __init__(self, name: str, *, return_failure: bool) -> None:
        self.definition = ToolDefinition(
            name=name,
            description="Return a normalized failure",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            max_output_characters=4,
        )
        self.return_failure = return_failure

    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        cancellation_token.raise_if_cancelled()
        failure = ToolCallError(
            code="temporary_unavailable",
            safe_message="工具暂时不可用。",
            retryable=True,
            correlation_id="corr_tool_failure",
            path="$.path",
            keyword="available",
            expected=True,
        )
        if self.return_failure:
            return ToolResult.failed(tool_call_id=call.id, error=failure)
        raise ToolError(
            failure.code,
            failure.safe_message,
            retryable=failure.retryable,
            correlation_id=failure.correlation_id,
            path=failure.path,
            keyword=failure.keyword,
            expected=failure.expected,
        )


class UnexpectedFailureTool(FakeTool):
    async def execute(self, call: ToolCall, cancellation_token: CancellationToken):
        del call, cancellation_token
        raise RuntimeError("internal database password: do-not-leak")


class StaticCompactionHook:
    def __init__(self) -> None:
        self.calls: list[tuple[ProviderMessage, ...]] = []

    async def compact(self, run, messages):
        del run
        self.calls.append(tuple(messages))
        # 纯消息替换语义:不固化 summary entry,直接给出替换后的完整消息。
        return ContextCompactionResult(
            messages=(ProviderMessage(role="system", content="压缩摘要"),),
            summary_entry_id=None,
            covered_entry_ids=(),
        )


class RuntimeV2ExecutionTest(unittest.TestCase):
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

    def test_run_executes_multiple_model_turns_and_persists_final_entry(self) -> None:
        _, lane, trigger, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("我先读取文件。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=11,
                        output_tokens=4,
                    ),
                ),
                (
                    ProviderTextDelta("文件内容是 file content。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=13,
                        output_tokens=6,
                    ),
                ),
            ]
        )
        tool = FakeTool(
            structured_content=["alpha", {"count": 2}, None]
        )
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

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("我先读取文件。文件内容是 file content。", result.content)
        self.assertEqual(24, result.input_tokens)
        self.assertEqual(10, result.output_tokens)
        self.assertEqual(2, len(result.model_turn_ids))
        # 工具调用数由执行循环自己数，供上层（子运行用量）O(1) 取用。
        self.assertEqual(1, result.tool_call_count)
        self.assertEqual(2, len(provider.requests))

        self.assertEqual("user", provider.requests[0].messages[0].role)
        self.assertEqual("读取文件", provider.requests[0].messages[0].content)
        self.assertEqual("assistant", provider.requests[1].messages[1].role)
        self.assertEqual("我先读取文件。", provider.requests[1].messages[1].content)
        self.assertEqual("call_1", provider.requests[1].messages[1].tool_calls[0].id)
        self.assertEqual("tool", provider.requests[1].messages[2].role)
        self.assertEqual("file content", provider.requests[1].messages[2].content)

        entries = self.repository.list_entries(lane.id)
        self.assertEqual(
            (
                TranscriptEntryType.USER_MESSAGE,
                TranscriptEntryType.TOOL_CALL,
                TranscriptEntryType.TOOL_RESULT,
                TranscriptEntryType.ASSISTANT_MESSAGE,
            ),
            tuple(entry.type for entry in entries),
        )
        self.assertEqual(trigger.id, entries[1].parent_id)
        self.assertEqual(
            ["alpha", {"count": 2}, None],
            entries[2].payload["structuredContent"],
        )
        self.assertEqual(result.assistant_entry_id, entries[-1].id)
        self.assertEqual(run.id, entries[-1].source_run_id)
        self.assertEqual(
            ModelTurnStatus.COMPLETED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.COMPLETED, tool_execution.status)
        self.assertIsNotNone(tool_execution.result_entry_id)

        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.COMPLETED, replay.derived_status)
        self.assertEqual(result.content, replay.partial_content)
        self.assertEqual((), replay.warnings)
        self.assertEqual(
            (ModelTurnStatus.COMPLETED, ModelTurnStatus.COMPLETED),
            tuple(turn.derived_status for turn in replay.model_turns),
        )

    def test_prelude_failure_before_start_run_finalises_run_and_releases_conversation(
        self,
    ) -> None:
        """R10：`start_run` **之前**的故障也要落终态，否则 `created` 行占死会话。

        起点：`AgentRunExecutor.execute` 的 `try` 只覆盖 `start_run` **之后**的
        步骤，而 run 行是网关先建的（状态 `created`，**也在** `ACTIVE_RUN_STATUSES`
        里）。前奏（`list_entry_context_entries` → `build_plan_shadow` →
        `context_projection.project`）抛错时行停在 `created`：交互路径无人收尾
        （网关 `_on_run_done` 吞异常、任务路径只收 `task_run`），落点会话被
        `storage/v2/transcript.py` 的活跃闸永久占住（`POST /messages` 恒 409），
        只能等下次启动清扫才自愈。
        """
        conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="前奏故障的一轮",
            client_request_id="r10-prelude-failure",
        )
        self.assertTrue(submission.created)
        # 网关先建的行本身就是活跃态——这正是占死会话的原因。
        self.assertIs(RunStatus.CREATED, submission.run.status)
        self.assertIn(submission.run.status, ACTIVE_RUN_STATUSES)
        self.assertIsNone(submission.run.started_at)

        executor = AgentRunExecutor(
            repository=self.repository,
            provider=ScriptedProvider(
                [
                    (
                        ProviderTextDelta("之后的一轮也能跑。"),
                        ProviderCompleted(
                            finish_reason="stop", input_tokens=4, output_tokens=2
                        ),
                    )
                ]
            ),
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )

        # 注入式故障：把**前奏**里的 `build_plan_shadow` 换成必抛的实现。补丁打在
        # `execution_impl.context_segments`——runner 函数体内的
        # `from .context_segments import build_plan_shadow` 解析的就是这个别名模块
        # （`execution_impl/context_segments.py` 只是把父包同名函数重新绑定过来），
        # 所以命中 `start_run` **之前**那一步。
        from endless_task.runtime_v2.execution_impl import (
            context_segments as runner_segments,
        )

        with mock.patch.object(
            runner_segments,
            "build_plan_shadow",
            side_effect=RuntimeError("prelude exploded"),
        ):
            result = asyncio.run(
                executor.execute(
                    submission.run.id,
                    cancellation_token=CancellationToken(),
                )
            )

        # ① 该 v2 行落终态（起点：永久停在 `created`，活跃闸占死会话）。
        final = self.repository.get_run(submission.run.id)
        self.assertIs(RunStatus.FAILED, final.status)
        self.assertNotIn(final.status, ACTIVE_RUN_STATUSES)
        self.assertIs(RunStatus.FAILED, result.status)
        self.assertEqual("runtime_internal_error", final.error_code)
        # ② 从未开始的行不许伪造 `started_at`（沿用 T2 口径：保持 NULL）。
        self.assertIsNone(final.started_at)
        self.assertIsNotNone(final.finished_at)

        # ③ 运行时事件里**恰好一条**终态事件；`start_run` 没跑过就不该有 run_started。
        event_types = [
            event.event_type
            for event in self.repository.list_runtime_events(submission.run.id)
        ]
        self.assertEqual(["run_failed"], event_types)

        # ④ 落点会话不再 409：还能再发一轮（起点：恒 ConflictError）。
        follow_up = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="之后的一轮",
            client_request_id="r10-after-prelude-failure",
        )
        self.assertTrue(follow_up.created)
        self.assertIs(RunStatus.CREATED, follow_up.run.status)
        # 且真的能重跑（起点：每次新 turn 都撞 409 而失败）。
        rerun = asyncio.run(
            executor.execute(
                follow_up.run.id, cancellation_token=CancellationToken()
            )
        )
        self.assertIs(RunStatus.COMPLETED, rerun.status)
        self.assertIs(RunStatus.COMPLETED, self.repository.get_run(follow_up.run.id).status)

    def test_get_run_failure_finalises_run_and_releases_conversation(self) -> None:
        """切片 10 残留 A1：`execute` 开头那次 `get_run` 失败也不许留活动态行。

        起点：`run = self._repository.get_run(run_id)` 在 `try` **之外**。它抛错时
        网关先建的 `created` 行（**也在** `ACTIVE_RUN_STATUSES` 里）无人收尾 →
        `storage/v2/transcript.py` 的活跃闸把落点会话永久占住（`POST /messages`
        恒 409），只能等下次启动清扫自愈。R10 把 `try` 上移到前奏之前，但没有覆盖
        到这一行；且此时 `run: RunRecord` 还不存在，只能靠**入参 `run_id`** 收尾。

        红检查：删掉 `execute` 里包住这次读取的 `try/except`（回到"读取失败直接
        外抛"）→ ① 的终态断言、③ 的事件断言、④ 的"能再发一轮"断言一起红。
        """
        conversation = self.chat_repository.create_conversation()
        submission = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="读取失败的一轮",
            client_request_id="a1-get-run-failure",
        )
        self.assertTrue(submission.created)
        self.assertIs(RunStatus.CREATED, submission.run.status)
        self.assertIn(submission.run.status, ACTIVE_RUN_STATUSES)
        self.assertIsNone(submission.run.started_at)

        executor = AgentRunExecutor(
            repository=self.repository,
            provider=ScriptedProvider(
                [
                    (
                        ProviderTextDelta("之后的一轮也能跑。"),
                        ProviderCompleted(
                            finish_reason="stop", input_tokens=4, output_tokens=2
                        ),
                    )
                ]
            ),
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )

        # 注入式故障：只让**开头那一次**读取抛错（其余调用照常），模拟"行已存在、
        # 数据库读不出来"。异常必须原样向上传播——调用方（网关 `_on_run_done`、
        # worker `_execute_with_guard`）本来就接住它，这里不新造正常返回。
        real_get_run = self.repository.get_run

        def exploding_get_run(run_id: str):
            if run_id == submission.run.id:
                raise NotFoundError(f"Run not found: {run_id}")
            return real_get_run(run_id)

        with mock.patch.object(
            self.repository, "get_run", side_effect=exploding_get_run
        ):
            with self.assertRaises(NotFoundError):
                asyncio.run(
                    executor.execute(
                        submission.run.id,
                        cancellation_token=CancellationToken(),
                    )
                )

        # ① 该 v2 行落终态（起点：永久停在 `created`，活跃闸占死会话）。
        final = self.repository.get_run(submission.run.id)
        self.assertIs(RunStatus.FAILED, final.status)
        self.assertNotIn(final.status, ACTIVE_RUN_STATUSES)
        self.assertEqual("runtime_internal_error", final.error_code)
        # ② 从未开始的行不许伪造 `started_at`（沿用 T2/R10 口径：保持 NULL）。
        self.assertIsNone(final.started_at)
        self.assertIsNotNone(final.finished_at)
        # ③ 运行时事件里**恰好一条**终态事件；`start_run` 没跑过就不该有 run_started。
        event_types = [
            event.event_type
            for event in self.repository.list_runtime_events(submission.run.id)
        ]
        self.assertEqual(["run_failed"], event_types)

        # ④ 落点会话不再 409：还能再发一轮（起点：恒 ConflictError）。
        follow_up = self.repository.create_message_submission(
            conversation_id=conversation.id,
            lane_id=None,
            content="之后的一轮",
            client_request_id="a1-after-get-run-failure",
        )
        self.assertTrue(follow_up.created)
        self.assertIs(RunStatus.CREATED, follow_up.run.status)
        # 且真的能重跑（起点：每次新 turn 都撞 409 而失败）。
        rerun = asyncio.run(
            executor.execute(
                follow_up.run.id, cancellation_token=CancellationToken()
            )
        )
        self.assertIs(RunStatus.COMPLETED, rerun.status)
        self.assertIs(
            RunStatus.COMPLETED, self.repository.get_run(follow_up.run.id).status
        )

    def test_get_run_failure_on_terminal_run_keeps_original_exception(self) -> None:
        """收尾兜底不许**替换**调用方原本要抛的异常（行已终态 → 吞掉 InvalidStateError）。

        红检查：把 `AgentRunExecutor.execute` 里读 run 那段的兜底收尾（`runner.py`
        内联的 `try: transition_run_status(...) except Exception:`）的
        `except Exception` 去掉 → 行已终态时它会抛 `InvalidStateError` 而不是调用方
        原本的 `NotFoundError`，用例红。
        """
        _, _, _, run = self._create_run()
        # 行先落终态：兜底迁移必然撞 `InvalidStateError`（终态不可再改）。
        self.repository.transition_run_status(
            run.id,
            RunStatus.CANCELLED,
            event_type="run_cancelled",
            cancelled_by="user",
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=ScriptedProvider([]),
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )
        real_get_run = self.repository.get_run

        def exploding_get_run(run_id: str):
            if run_id == run.id:
                raise NotFoundError(f"Run not found: {run_id}")
            return real_get_run(run_id)

        with mock.patch.object(
            self.repository, "get_run", side_effect=exploding_get_run
        ):
            with self.assertRaises(NotFoundError):
                asyncio.run(
                    executor.execute(run.id, cancellation_token=CancellationToken())
                )
        # 终态不被改写；也没有多出第二条终态事件。
        self.assertIs(RunStatus.CANCELLED, self.repository.get_run(run.id).status)
        self.assertEqual(
            ["run_cancelled"],
            [
                event.event_type
                for event in self.repository.list_runtime_events(run.id)
            ],
        )

    def test_normal_run_event_sequence_is_unchanged_after_prelude_finalisation(
        self,
    ) -> None:
        """正常路径逐字不变：单轮 run 仍走到 `COMPLETED`，事件序列与今天一致。"""
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("你好呀。"),
                    ProviderCompleted(
                        finish_reason="stop", input_tokens=5, output_tokens=3
                    ),
                )
            ]
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertIs(RunStatus.COMPLETED, result.status)
        self.assertEqual("你好呀。", result.content)
        self.assertEqual(5, result.input_tokens)
        self.assertEqual(3, result.output_tokens)
        persisted = self.repository.get_run(run.id)
        self.assertIs(RunStatus.COMPLETED, persisted.status)
        self.assertIsNotNone(persisted.started_at)
        self.assertEqual(
            (
                "run_started",
                "run_budget_declared",
                "model_turn_status_changed",
                "model_turn_status_changed",
                "model_turn_started",
                "model_text_delta",
                "model_turn_completed",
                "run_completed",
            ),
            tuple(
                event.event_type
                for event in self.repository.list_runtime_events(run.id)
            ),
        )

    def test_read_only_tool_without_confirmation_judge_completes(self) -> None:
        # 回归：只读工具（如 read_workspace_file / list_workspace_dir）不定义
        # requires_explicit_confirmation（继承自 RegisteredTool Protocol），v2 曾因
        # 无条件调用而抛 AttributeError，导致整个 run 变成 runtime_internal_error。
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("先列出目录。"),
                    ProviderToolCall(
                        id="call_1",
                        name="list_workspace_dir",
                        arguments={},
                    ),
                    ProviderCompleted(
                        finish_reason="tool_calls",
                        input_tokens=8,
                        output_tokens=3,
                    ),
                ),
                (
                    ProviderTextDelta("目录已列出。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=5,
                    ),
                ),
            ]
        )
        tool = ReadOnlyToolWithoutConfirmationJudge()
        self.assertFalse(hasattr(tool, "requires_explicit_confirmation"))
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

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("先列出目录。目录已列出。", result.content)
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.COMPLETED, tool_execution.status)
        self.assertIsNotNone(tool_execution.result_entry_id)
        self.assertEqual(1, len(tool.calls))

        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.COMPLETED, replay.derived_status)
        self.assertEqual((), replay.warnings)

    def test_read_only_exploration_continues_without_no_deliverable_stop(self) -> None:
        # 与 pi 对齐：v2 不再设「只读不交付」「连续空回」「重复调用」等计数式安全停止。
        # 模型连续只读探索即便远超旧的无交付阈值（旧实现默认 6 轮即停），也应由模型自行
        # 收敛、在产出最终回答时交付，而不是被安全策略截停。这里用 6 轮只读 + 1 轮最终文本。
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id=f"call_{index}",
                        name="read_text_file",
                        arguments={"path": f"{index}.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                )
                for index in range(1, 7)
            ]
            + [
                (
                    ProviderTextDelta("收集完毕，以下为结论。"),
                    ProviderCompleted(finish_reason="stop"),
                )
            ]
        )
        tool = FakeTool(name="read_text_file")
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

        # 未被旧的无交付阈值（6 轮）截停，模型收敛后正常完成。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(7, len(result.model_turn_ids))
        self.assertEqual(7, len(provider.requests))
        self.assertEqual("收集完毕，以下为结论。", result.content)
        self.assertIsNone(self.repository.get_run(run.id).error_code)

    def test_malformed_tool_arguments_are_recovered_not_fatal(self) -> None:
        # 健壮性：模型返回的工具参数不是有效 JSON 时，不再把整个 run 判为致命错误
        # （旧行为：回答失败 invalid_tool_arguments）。改为记录一次失败的工具调用并把
        # 错误反馈给模型，run 继续，模型在下一轮修正后可正常交付。
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_bad",
                        name="read_text_file",
                        arguments={},
                        parse_error="模型返回的工具参数不是有效 JSON。",
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("我修正了参数，以下是内容。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool(name="read_text_file")
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

        # run 没有被判失败，而是继续走到模型交付。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual("我修正了参数，以下是内容。", result.content)
        self.assertIsNone(self.repository.get_run(run.id).error_code)
        # 出错的那次工具调用被记录为 failed，并把错误反馈给了模型。
        tool_executions = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )
        self.assertEqual(1, len(tool_executions))
        self.assertEqual(ToolExecutionStatus.FAILED, tool_executions[0].status)
        self.assertEqual("invalid_tool_arguments", tool_executions[0].error_code)
        self.assertIn(
            "不是有效 JSON",
            (tool_executions[0].safe_message or ""),
        )

    def test_returned_and_raised_failures_share_persisted_contract(self) -> None:
        for return_failure in (False, True):
            with self.subTest(return_failure=return_failure):
                _, lane, _, run = self._create_run()
                provider = ScriptedProvider(
                    [
                        (
                            ProviderToolCall(
                                id=f"call_failure_{return_failure}",
                                name="normalized_failure",
                                arguments={"path": "a.txt"},
                            ),
                            ProviderCompleted(finish_reason="tool_calls"),
                        ),
                        (
                            ProviderTextDelta("已处理失败。"),
                            ProviderCompleted(finish_reason="stop"),
                        ),
                    ]
                )
                registry = ToolRegistry()
                registry.register(
                    NormalizedFailureTool(
                        "normalized_failure",
                        return_failure=return_failure,
                    )
                )
                executor = AgentRunExecutor(
                    repository=self.repository,
                    provider=provider,
                    tool_registry=registry,
                    model="scripted-model",
                )

                result = asyncio.run(
                    executor.execute(
                        run.id,
                        cancellation_token=CancellationToken(),
                    )
                )

                self.assertEqual(RunStatus.COMPLETED, result.status)
                execution = self.repository.list_tool_executions(
                    result.model_turn_ids[0]
                )[0]
                self.assertEqual(ToolExecutionStatus.FAILED, execution.status)
                self.assertEqual("temporary_unavailable", execution.error_code)
                self.assertEqual("工具暂时不可用。", execution.safe_message)
                self.assertTrue(execution.retryable)
                self.assertEqual("corr_tool_failure", execution.correlation_id)
                self.assertEqual(
                    {
                        "path": "$.path",
                        "keyword": "available",
                        "expected": True,
                    },
                    execution.error_details,
                )

                result_entry = next(
                    entry
                    for entry in self.repository.list_entries(lane.id)
                    if entry.id == execution.result_entry_id
                )
                self.assertEqual(True, result_entry.payload["retryable"])
                self.assertEqual(
                    "corr_tool_failure",
                    result_entry.payload["correlationId"],
                )
                self.assertEqual(execution.error_details, result_entry.payload["errorDetails"])

                failed_event = next(
                    event
                    for event in self.repository.list_runtime_events(run.id)
                    if event.event_type == "tool_execution_failed"
                )
                self.assertEqual(True, failed_event.payload["retryable"])
                self.assertEqual(
                    "corr_tool_failure",
                    failed_event.payload["correlationId"],
                )
                model_feedback = provider.requests[1].messages[-1].content
                self.assertIn("temporary_unavailable", model_feedback)
                self.assertIn("$.path", model_feedback)
                self.assertIn("调整参数后重试", model_feedback)
                self.assertGreater(len(model_feedback), 4)

    def test_schema_failure_preserves_actionable_validation_details(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_invalid_schema",
                        name="read_file",
                        arguments={"path": 42},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("已修正参数。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(FakeTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual("invalid_tool_arguments", execution.error_code)
        self.assertTrue(execution.retryable)
        self.assertEqual(
            {"path": "$.path", "keyword": "type", "expected": "string"},
            execution.error_details,
        )
        model_feedback = provider.requests[1].messages[-1].content
        self.assertIn("$.path", model_feedback)
        self.assertIn("规则：type", model_feedback)
        self.assertIn('期望："string"', model_feedback)

    def test_unexpected_tool_exception_is_safely_redacted(self) -> None:
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_crash",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("已安全处理。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(UnexpectedFailureTool())
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        with self.assertLogs("endless_task.runtime_v2.execution", level="ERROR"):
            result = asyncio.run(
                executor.execute(run.id, cancellation_token=CancellationToken())
            )

        execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual("tool_execution_failed", execution.error_code)
        self.assertEqual("工具执行出现内部错误。", execution.safe_message)
        self.assertFalse(execution.retryable)
        self.assertEqual(run.correlation_id, execution.correlation_id)
        persisted = str(self.repository.list_entries(lane.id))
        events = str(self.repository.list_runtime_events(run.id))
        model_feedback = provider.requests[1].messages[-1].content
        for exposed in (persisted, events, model_feedback):
            self.assertNotIn("do-not-leak", exposed)
            self.assertNotIn("database password", exposed)

    def test_agent_loop_runs_until_model_delivers_final_answer(self) -> None:
        # 连续有工具动作的轮次应继续执行，直到模型交付最终文本。
        _, lane, _, run = self._create_run()
        responses = [
            (
                ProviderToolCall(
                    id=f"call_{index}",
                    name="write_file",
                    arguments={"path": f"{index}.txt"},
                ),
                ProviderCompleted(finish_reason="tool_calls"),
            )
            for index in range(1, 5)
        ]
        # 最后一个模型回合：无工具调用 → 交付最终文本 → 完成。
        responses.append(
            (
                ProviderTextDelta("已完成。"),
                ProviderCompleted(finish_reason="stop"),
            )
        )
        provider = ScriptedProvider(responses)
        # 注意：FakeTool 的 schema 只接受 path 参数，故写调用用 path。
        tool = FakeTool(name="write_file", effect=ToolEffect.LOCAL_WRITE, approval_mode=ToolApprovalMode.REQUIRED)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        # 走完 5 个模型回合（4 个工具轮次 + 1 个最终文本轮次）。
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(5, len(result.model_turn_ids))
        self.assertEqual(5, len(provider.requests))
        self.assertEqual("已完成。", result.content)

    def test_parallel_tool_batch_preserves_source_order(self) -> None:
        _, lane, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_slow",
                        name="slow_read",
                        arguments={"path": "slow.txt"},
                    ),
                    ProviderToolCall(
                        id="call_fast",
                        name="fast_read",
                        arguments={"path": "fast.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("两个工具都完成了。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=5),
                ),
            ]
        )
        slow = FakeTool("slow_read", content="slow result", delay=0.05)
        fast = FakeTool("fast_read", content="fast result")
        registry = ToolRegistry()
        registry.register(slow)
        registry.register(fast)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(slow.calls))
        self.assertEqual(1, len(fast.calls))
        self.assertLessEqual(
            slow.started_at[0],
            fast.finished_at[0],
            "read-only tools should execute concurrently",
        )
        tool_messages = provider.requests[1].messages[2:4]
        self.assertEqual(("call_slow", "call_fast"), tuple(
            message.tool_call_id for message in tool_messages
        ))
        entries = self.repository.list_entries(lane.id)
        tool_results = [entry for entry in entries if entry.type is TranscriptEntryType.TOOL_RESULT]
        self.assertEqual(("slow result", "fast result"), tuple(
            entry.payload["content"] for entry in tool_results
        ))

    def test_tool_batch_at_call_limit_respects_concurrency_limit(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                tuple(
                    [
                        ProviderToolCall(
                            id=f"call_{index}",
                            name="limited_read",
                            arguments={"path": f"{index}.txt"},
                        )
                        for index in range(4)
                    ]
                    + [ProviderCompleted(finish_reason="tool_calls")]
                ),
                (
                    ProviderTextDelta("批次已完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool("limited_read", delay=0.02)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            tool_execution_limits=ToolExecutionLimits(
                max_calls_per_turn=4,
                max_concurrent_calls=2,
            ),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(4, len(tool.calls))
        self.assertEqual(2, tool.max_active_calls)

    def test_tool_batch_over_call_limit_stops_before_persistence(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                tuple(
                    [
                        ProviderToolCall(
                            id=f"call_{index}",
                            name="limited_read",
                            arguments={"path": f"{index}.txt"},
                        )
                        for index in range(3)
                    ]
                    + [ProviderCompleted(finish_reason="tool_calls")]
                )
            ]
        )
        tool = FakeTool("limited_read")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            tool_execution_limits=ToolExecutionLimits(max_calls_per_turn=2),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("tool_call_limit_exceeded", result.run.error_code)
        self.assertEqual(0, len(tool.calls))
        self.assertEqual(
            (),
            self.repository.list_tool_executions(result.model_turn_ids[0]),
        )

    def test_oversized_tool_arguments_stop_before_persistence(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_large",
                        name="limited_read",
                        arguments={"path": "x" * 100},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                )
            ]
        )
        tool = FakeTool("limited_read")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            tool_execution_limits=ToolExecutionLimits(
                max_argument_bytes=32,
                max_total_argument_bytes=32,
            ),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("tool_argument_limit_exceeded", result.run.error_code)
        self.assertEqual(
            (),
            self.repository.list_tool_executions(result.model_turn_ids[0]),
        )

    def test_deeply_nested_tool_arguments_stop_before_persistence(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_deep",
                        name="limited_read",
                        arguments={"path": {"a": {"b": "value"}}},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                )
            ]
        )
        tool = FakeTool("limited_read")
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            tool_execution_limits=ToolExecutionLimits(
                max_argument_bytes=1_000,
                max_total_argument_bytes=1_000,
                max_argument_depth=3,
            ),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("tool_argument_limit_exceeded", result.run.error_code)
        self.assertEqual(
            (),
            self.repository.list_tool_executions(result.model_turn_ids[0]),
        )

    def test_side_effect_tool_waits_for_approval(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_write",
                        name="write_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        tool = FakeTool(
            "write_file",
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )
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

        self.assertEqual(RunStatus.WAITING_APPROVAL, result.status)
        self.assertEqual(0, len(tool.calls))
        self.assertEqual(RunStatus.WAITING_APPROVAL, self.repository.get_run(run.id).status)
        tool_execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.WAITING_APPROVAL, tool_execution.status)

    def test_approved_side_effect_tool_executes(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_write",
                        name="write_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("已写入。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=3),
                ),
            ]
        )
        tool = FakeTool(
            "write_file",
            content="written",
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            approval_gate=StaticToolApprovalGate(ToolApprovalDecision.APPROVE),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(tool.calls))
        self.assertEqual(
            ToolExecutionStatus.COMPLETED,
            self.repository.list_tool_executions(result.model_turn_ids[0])[0].status,
        )

    def test_unattended_gate_denies_required_tool_without_executing_it(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_write",
                        name="write_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("该操作需要人工批准，未执行。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        tool = FakeTool(
            "write_file",
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.REQUIRED,
        )
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            approval_gate=UnattendedToolApprovalGate(),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], tool.calls)
        execution = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )[0]
        self.assertEqual(ToolExecutionStatus.REJECTED, execution.status)
        self.assertEqual("approval_denied", execution.error_code)
        self.assertIn("未授权", provider.requests[1].messages[-1].content)

    def test_steering_is_injected_at_next_model_turn(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
                (
                    ProviderTextDelta("已结合补充信息。"),
                    ProviderCompleted(finish_reason="stop", input_tokens=12, output_tokens=4),
                ),
            ]
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
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            while not provider.requests:
                await asyncio.sleep(0)
            self.assertTrue(await executor.enqueue_user_message("补充：只看摘要"))
            result = await task
            self.assertFalse(await executor.enqueue_user_message("后续问题"))
            follow_up_messages = await executor.drain_follow_up_messages()
            return result, follow_up_messages

        result, follow_up_messages = asyncio.run(scenario())
        steering_message = provider.requests[1].messages[-1]
        self.assertEqual("user", steering_message.role)
        self.assertEqual("补充：只看摘要", steering_message.content)
        self.assertEqual(RunStatus.COMPLETED, result.status)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("steer_injected", event_types)

        self.assertEqual(("后续问题",), follow_up_messages)

    def test_cancellation_persists_run_and_model_turn_state(self) -> None:
        _, _, _, run = self._create_run()
        provider = PausedProvider()
        registry = ToolRegistry()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="paused-model",
        )
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            await provider.paused.wait()
            await executor.request_cancel()
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(RunStatus.CANCELLED, result.status)
        self.assertEqual(RunStatus.CANCELLED, self.repository.get_run(run.id).status)
        self.assertEqual(
            ModelTurnStatus.CANCELLED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("run_cancel_requested", event_types)
        self.assertIn("run_cancelled", event_types)

    def test_cancellation_during_tool_execution_persists_terminal_tool_state(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_paused",
                        name="paused_read",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderToolCall(
                        id="call_queued",
                        name="paused_read",
                        arguments={"path": "b.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        tool = PausedTool()
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            tool_execution_limits=ToolExecutionLimits(max_concurrent_calls=1),
        )
        token = CancellationToken()

        async def scenario() -> None:
            task = asyncio.create_task(executor.execute(run.id, cancellation_token=token))
            await tool.paused.wait()
            await executor.request_cancel()
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(RunStatus.CANCELLED, result.status)
        self.assertEqual(
            ModelTurnStatus.CANCELLED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        tool_executions = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )
        self.assertEqual(2, len(tool_executions))
        self.assertEqual(
            (ToolExecutionStatus.CANCELLED, ToolExecutionStatus.CANCELLED),
            tuple(execution.status for execution in tool_executions),
        )
        self.assertTrue(
            all(execution.result_entry_id is not None for execution in tool_executions)
        )
        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.CANCELLED, replay.derived_status)
        self.assertEqual(ModelTurnStatus.CANCELLED, replay.model_turns[0].derived_status)
        self.assertEqual(
            (ToolExecutionStatus.CANCELLED, ToolExecutionStatus.CANCELLED),
            tuple(
                execution.derived_status
                for execution in replay.model_turns[0].tool_executions
            ),
        )
        self.assertEqual((), replay.warnings)

    def test_cancellation_during_tool_batch_preparation_persists_terminal_tool_state(self) -> None:
        _, _, _, run = self._create_run()
        token = CancellationToken()
        provider = ScriptedProvider(
            [
                (
                    ProviderToolCall(
                        id="call_first",
                        name="cancel_during_prepare",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderToolCall(
                        id="call_second",
                        name="cancel_during_prepare",
                        arguments={"path": "b.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
            ]
        )
        tool = CancellationDuringPreparationTool(token)
        registry = ToolRegistry()
        registry.register(tool)
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
        )

        result = asyncio.run(executor.execute(run.id, cancellation_token=token))

        self.assertEqual(RunStatus.CANCELLED, result.status)
        self.assertEqual(0, len(tool.calls))
        tool_executions = self.repository.list_tool_executions(
            result.model_turn_ids[0]
        )
        self.assertEqual(2, len(tool_executions))
        self.assertEqual(
            (ToolExecutionStatus.CANCELLED, ToolExecutionStatus.CANCELLED),
            tuple(execution.status for execution in tool_executions),
        )
        self.assertTrue(
            all(execution.result_entry_id is not None for execution in tool_executions)
        )

    def test_host_task_cancellation_finalises_run(self) -> None:
        """宿主取消（外层任务取消 / asyncio.timeout）也必须把运行落成终态。

        这类取消不是运行自己的 CancellationToken：抛的是 ``asyncio.CancelledError``
        （BaseException 子类），既不会被 ``except Exception`` 兜住，也不会走
        ``except RuntimeCancelled``。不落终态的话运行行会停在运行中，冻结该会话。
        """
        _, _, _, run = self._create_run()
        provider = HangingProvider()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="hanging-model",
        )

        async def scenario() -> None:
            task = asyncio.create_task(
                executor.execute(run.id, cancellation_token=CancellationToken())
            )
            await provider.entered.wait()
            # 宿主取消：取消正在 await execute() 的任务。
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        persisted = self.repository.get_run(run.id)
        # 取消不能被吞掉变成正常返回，但运行必须已经是终态。
        self.assertNotIn(persisted.status, ACTIVE_RUN_STATUSES)
        self.assertEqual(RunStatus.CANCELLED, persisted.status)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("run_cancelled", event_types)

    def test_host_cancellation_finalize_is_idempotent_on_terminal_run(self) -> None:
        """已终态的运行再做一次宿主取消收尾：不迁移、不抛异常（幂等兜底）。"""
        _, _, _, run = self._create_run()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=ScriptedProvider([]),
            tool_registry=ToolRegistry(),
            model="scripted-model",
        )
        self.repository.start_run(run.id)
        self.repository.transition_run_status(
            run.id,
            RunStatus.COMPLETED,
            event_type="run_completed",
        )

        executor._finalize_host_cancellation(run.id)

        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(run.id).status)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertNotIn("run_cancelled", event_types)

    def test_wallclock_watchdog_finalises_gracefully(self) -> None:
        """硬时长上限 → 优雅收尾（COMPLETED + 引导语），不再 FAILED。"""
        _, _, _, run = self._create_run()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=PausedProvider(),
            tool_registry=ToolRegistry(),
            model="paused-model",
            run_budget=RunBudget(soft_seconds=0.01, hard_seconds=0.02),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertIsNone(result.run.error_code)
        self.assertIn("时长上限", result.content)
        # 优雅收尾也必须把在飞轮次收口，不能留在 RUNNING，也不能从结果里丢失。
        persisted = self.repository.list_model_turns(run.id)
        self.assertEqual(
            ModelTurnStatus.CANCELLED,
            persisted[0].status,
        )
        self.assertEqual(
            [turn.id for turn in persisted],
            list(result.model_turn_ids),
        )

    def test_wallclock_watchdog_stops_hung_loop_gracefully(self) -> None:
        _, _, _, run = self._create_run()
        provider = RepeatingReadProvider()
        registry = ToolRegistry()
        registry.register(FakeTool(name="read_file"))
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            run_budget=RunBudget(soft_seconds=0.01, hard_seconds=0.05),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertGreater(len(provider.requests), 0)
        stops = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_awaiting_user"
            and event.payload["reason"] == RTIME
            and event.payload["willStop"] is True
        ]
        self.assertEqual(1, len(stops))
        entry = self.repository.get_entry(result.assistant_entry_id)
        self.assertEqual("time_budget_stop", entry.payload["finishReason"])

    def test_hung_single_turn_does_not_consume_the_whole_budget(self) -> None:
        """墙体变大后，一次挂死的模型调用不能吃掉整段预算。"""
        _, _, _, run = self._create_run()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=PausedProvider(),
            tool_registry=ToolRegistry(),
            model="paused-model",
            run_budget=RunBudget(
                soft_seconds=0.1,
                hard_seconds=30.0,
                per_turn_seconds=0.02,
            ),
        )

        started = time.monotonic()
        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )
        elapsed = time.monotonic() - started

        # 单轮上限把它截断在 0.02s 量级，而不是等满 30s 硬墙。
        self.assertLess(elapsed, 5.0)
        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("agent_timeout", result.run.error_code)

    def test_early_inner_timeout_is_not_reported_as_the_wall(self) -> None:
        """非墙钟来源的 TimeoutError 绝不能被说成"已到时长上限"。"""
        _, _, _, run = self._create_run()

        class EarlyTimeoutProvider:
            name = "early-timeout"

            def __init__(self) -> None:
                self.requests: list[object] = []

            async def stream(self, request, cancellation_token):
                self.requests.append(request)
                raise TimeoutError("inner component timed out")
                yield  # pragma: no cover - 让它是 async generator

        # 墙很远（30s），单轮上限 60s ⇒ 这个 TimeoutError 不可能来自墙。
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=EarlyTimeoutProvider(),
            tool_registry=ToolRegistry(),
            model="early-timeout-model",
            run_budget=RunBudget(soft_seconds=1.0, hard_seconds=30.0),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("agent_timeout", result.run.error_code)
        self.assertNotIn("时长上限", result.content)
        entries = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_awaiting_user"
        ]
        self.assertEqual([], entries)

    def test_early_inner_timeout_without_hard_wall_is_not_reported_as_the_wall(
        self,
    ) -> None:
        """根本没有硬墙时，内层 TimeoutError 更不该被说成"已到时长上限"。"""
        _, _, _, run = self._create_run()

        class EarlyTimeoutProvider:
            name = "early-timeout"

            def __init__(self) -> None:
                self.requests: list[object] = []

            async def stream(self, request, cancellation_token):
                self.requests.append(request)
                raise TimeoutError("inner component timed out")
                yield  # pragma: no cover - 让它是 async generator

        executor = AgentRunExecutor(
            repository=self.repository,
            provider=EarlyTimeoutProvider(),
            tool_registry=ToolRegistry(),
            model="early-timeout-model",
            run_budget=RunBudget(soft_seconds=1.0, hard_seconds=None),
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("agent_timeout", result.run.error_code)
        self.assertNotIn("时长上限", result.content)
        entries = [
            event
            for event in self.repository.list_runtime_events(run.id)
            if event.event_type == "run_awaiting_user"
        ]
        self.assertEqual([], entries)

    def test_provider_error_terminates_run_with_safe_error(self) -> None:
        _, _, _, run = self._create_run()
        provider = FakeProvider(
            chunks=("部分输出",),
            failure_after_chunks=1,
        )
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="fake-model",
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual("provider_unavailable", result.run.error_code)
        self.assertEqual("模拟模型服务暂不可用。", result.run.safe_message)
        self.assertEqual(
            ModelTurnStatus.FAILED,
            self.repository.get_model_turn(result.model_turn_ids[0]).status,
        )
        replay = RuntimeV2ReplayService(self.repository).replay_run(run.id)
        self.assertEqual(RunStatus.FAILED, replay.derived_status)
        self.assertEqual("部分输出", replay.partial_content)
        self.assertEqual(ModelTurnStatus.FAILED, replay.model_turns[0].derived_status)
        self.assertEqual((), replay.warnings)

    def test_context_compaction_hook_runs_between_model_turns(self) -> None:
        _, _, _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成"),
                    ProviderCompleted(finish_reason="stop", input_tokens=10, output_tokens=2),
                ),
            ]
        )
        compaction_hook = StaticCompactionHook()
        executor = AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=ToolRegistry(),
            model="scripted-model",
            compaction_hook=compaction_hook,
        )

        result = asyncio.run(
            executor.execute(run.id, cancellation_token=CancellationToken())
        )

        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(1, len(compaction_hook.calls))
        self.assertEqual("压缩摘要", provider.requests[0].messages[0].content)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(run.id)
        )
        self.assertIn("compaction_started", event_types)
        self.assertIn("compaction_completed", event_types)


class ContextProjectionTest(unittest.TestCase):
    def test_projects_only_policy_allowed_entries(self) -> None:
        from endless_task.runtime_v2 import TranscriptEntryRecord, TranscriptEntryStatus

        def entry(
            entry_id: str,
            entry_type: TranscriptEntryType,
            actor: Actor,
            payload: dict,
            policy: dict,
        ) -> TranscriptEntryRecord:
            return TranscriptEntryRecord(
                id=entry_id,
                conversation_id="conversation",
                parent_id=None,
                lane_id="lane",
                seq=1,
                type=entry_type,
                type_version=1,
                actor=actor,
                status=TranscriptEntryStatus.FINAL,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                payload=payload,
                context_policy=policy,
                display={},
            )

        result = ContextProjection().project(
            [
                entry(
                    "user",
                    TranscriptEntryType.USER_MESSAGE,
                    Actor.USER,
                    {"content": "问题"},
                    {"include_in_llm": True, "transform": "full"},
                ),
                entry(
                    "artifact",
                    TranscriptEntryType.ARTIFACT_REF,
                    Actor.SYSTEM,
                    {"artifactId": "artifact"},
                    {"include_in_llm": False, "transform": "none"},
                ),
                entry(
                    "tool_call",
                    TranscriptEntryType.TOOL_CALL,
                    Actor.TOOL,
                    {"callId": "call_1", "toolName": "read_file", "arguments": {}},
                    {"include_in_llm": False, "transform": "none"},
                ),
                entry(
                    "tool",
                    TranscriptEntryType.TOOL_RESULT,
                    Actor.TOOL,
                    {"callId": "call_1", "toolName": "read_file", "content": "结果"},
                    {
                        "include_in_llm": True,
                        "transform": "tool_result",
                        "trust_level": "untrusted",
                    },
                ),
            ]
        )

        # tool_call 条目本身被策略排除，但 tool 消息必须配对 → 投影补一条合成
        # assistant(tool_calls)，因此它也算"进入上下文"。
        self.assertEqual(
            ("user", "tool_call", "tool"), result.included_entry_ids
        )
        self.assertEqual(("artifact",), result.skipped_entry_ids)
        self.assertEqual(
            ("user", "assistant", "tool"),
            tuple(message.role for message in result.messages),
        )
        self.assertEqual("call_1", result.messages[1].tool_calls[0].id)
        self.assertEqual("call_1", result.messages[2].tool_call_id)
