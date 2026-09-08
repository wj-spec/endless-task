"""C1 制造者—检查者分离：独立验证的运行时集成。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
)
from endless_task.storage import Database, SqliteChatRepository, SqliteRuntimeV2Repository
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
    ToolResult,
)

from tests.test_runtime_v2_execution import FakeTool, ScriptedProvider


class WriteTool(FakeTool):
    """有副作用的工具（用于 side_effects 模式的判据）。"""

    def __init__(self, name: str = "write_file") -> None:
        super().__init__(name, effect=ToolEffect.LOCAL_WRITE)


def _verdict_text(verdict: str, *, reasons=(), missing=()) -> str:
    return json.dumps(
        {"verdict": verdict, "reasons": list(reasons), "missing": list(missing)},
        ensure_ascii=False,
    )


class _VerificationTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create_run(self):
        conversation = self.chat_repository.create_conversation()
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "把结论写进 report.md"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )
        return conversation, run

    def _executor(self, provider, tool, **kwargs) -> AgentRunExecutor:
        registry = ToolRegistry()
        registry.register(tool)
        return AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="maker-model",
            **kwargs,
        )

    def _events(self, run_id: str, event_type: str) -> list:
        return [
            event
            for event in self.repository.list_runtime_events(run_id)
            if event.event_type == event_type
        ]


class VerificationRunTest(_VerificationTestCase):
    async def test_off_by_default_does_not_verify(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [(ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop"))]
        )
        executor = self._executor(provider, FakeTool())
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_verified"))
        self.assertEqual(1, len(provider.requests))

    async def test_verifier_runs_independently_and_records_pass(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("已把结论写入 report.md。"),
                    ProviderToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={"path": "report.md"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (
                    ProviderTextDelta("完成：report.md 已写好。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
                (
                    ProviderTextDelta(_verdict_text("pass", reasons=("目标已达成",))),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=11,
                        output_tokens=7,
                    ),
                ),
            ]
        )
        executor = self._executor(
            provider,
            WriteTool(),
            verifier_mode="side_effects",
            verifier_model="verifier-model",
        )
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)

        verified = self._events(run.id, "run_verified")
        self.assertEqual(1, len(verified))
        payload = verified[0].payload
        self.assertEqual("pass", payload["verdict"])
        self.assertEqual("verifier-model", payload["model"])
        self.assertEqual(("目标已达成",), tuple(payload["reasons"]))
        self.assertEqual(11, payload["inputTokens"])
        self.assertIsInstance(payload["latencyMs"], int)
        # 先有"验证中"，再有结论。
        self.assertEqual(1, len(self._events(run.id, "run_verifying")))

        # 隔离性：验证请求只有 system+user，无工具，且不含制造者的工具历史。
        verify_request = provider.requests[-1]
        self.assertEqual((), tuple(verify_request.tools))
        self.assertEqual("verifier-model", verify_request.model)
        self.assertEqual(2, len(verify_request.messages))
        self.assertEqual(["system", "user"], [m.role for m in verify_request.messages])
        self.assertIn("把结论写进 report.md", verify_request.messages[1].content)
        self.assertIn("report.md 已写好", verify_request.messages[1].content)
        self.assertIn("write_file", verify_request.messages[1].content)
        self.assertNotIn("tool", [m.role for m in verify_request.messages])

    async def test_failed_verification_escalates_with_verdict(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("已完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
                (
                    ProviderTextDelta(
                        _verdict_text(
                            "fail",
                            reasons=("报告未写入",),
                            missing=("report.md",),
                        )
                    ),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        executor = self._executor(provider, WriteTool(), verifier_mode="1")
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        escalated = self._events(run.id, "run_awaiting_user")
        self.assertEqual(1, len(escalated))
        payload = escalated[0].payload
        self.assertEqual("verification_failed", payload["reason"])
        self.assertIn("独立验证未通过", payload["summary"])
        self.assertIn("报告未写入", payload["summary"])
        self.assertEqual("fail", payload["verdict"]["verdict"])
        self.assertEqual(("报告未写入",), tuple(payload["verdict"]["reasons"]))

    async def test_unparsable_verifier_output_is_uncertain_without_escalation(
        self,
    ) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (ProviderTextDelta("已完成。"), ProviderCompleted(finish_reason="stop")),
                (ProviderTextDelta("我觉得还行吧。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        executor = self._executor(provider, WriteTool(), verifier_mode="1")
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        verified = self._events(run.id, "run_verified")
        self.assertEqual(1, len(verified))
        self.assertEqual("uncertain", verified[0].payload["verdict"])
        self.assertEqual([], self._events(run.id, "run_awaiting_user"))

    async def test_side_effects_mode_skips_read_only_runs(self) -> None:
        _, run = self._create_run()
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("读完了。"),
                    ProviderToolCall(
                        id="call_1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    ),
                    ProviderCompleted(finish_reason="tool_calls"),
                ),
                (ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop")),
            ]
        )
        executor = self._executor(provider, FakeTool(), verifier_mode="side_effects")
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual([], self._events(run.id, "run_verified"))
        self.assertEqual(2, len(provider.requests))

    async def test_verifier_failure_does_not_change_run_result(self) -> None:
        _, run = self._create_run()

        class ExplodingVerifierProvider(ScriptedProvider):
            async def stream(self, request, cancellation_token):
                if request.request_id.endswith(":verify"):
                    raise RuntimeError("verifier transport down")
                async for event in super().stream(request, cancellation_token):
                    yield event

        provider = ExplodingVerifierProvider(
            [(ProviderTextDelta("完成。"), ProviderCompleted(finish_reason="stop"))]
        )
        executor = self._executor(provider, WriteTool(), verifier_mode="1")
        result = await executor.execute(run.id, cancellation_token=CancellationToken())
        self.assertEqual(RunStatus.COMPLETED, result.status)
        verified = self._events(run.id, "run_verified")
        self.assertEqual(1, len(verified))
        self.assertEqual("uncertain", verified[0].payload["verdict"])
        self.assertIn("调用失败", verified[0].payload["reasons"][0])


class VerificationSnapshotTest(_VerificationTestCase):
    async def test_snapshot_exposes_verification(self) -> None:
        provider = ScriptedProvider(
            [
                (ProviderTextDelta("已完成。"), ProviderCompleted(finish_reason="stop")),
                (
                    ProviderTextDelta(_verdict_text("pass")),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        registry = ToolRegistry()
        registry.register(WriteTool())
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="maker-model",
            max_output_tokens=128,
            verifier_mode="1",
            verifier_model="verifier-model",
        )
        conversation = self.chat_repository.create_conversation()
        handle = await gateway.send(conversation.id, "任务")
        await gateway.shutdown()
        snapshot = gateway.snapshot(conversation.id)
        verification = snapshot.get("verification")
        self.assertIsNotNone(verification)
        assert isinstance(verification, dict)
        self.assertEqual("verified", verification["status"])
        self.assertEqual("pass", verification["verdict"])
        self.assertEqual("verifier-model", verification["model"])
        self.assertEqual(handle.run_id, verification["runId"])


if __name__ == "__main__":
    unittest.main()
