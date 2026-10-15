"""M4A DR-1 slice 4: coordinator/scheduler over the production adapter E2E.

First end-to-end proof that the delegation boundary drives the **real**
runtime-v2 execution surface (not a fake kernel): an
:class:`InProcessChildCoordinator` receives a :class:`RuntimeV2AgentKernel`
and a SpawnSpec, the child runs as a genuine repository run through
:class:`AgentRunExecutor`, and the projected ChildOutcome is returned.

This integration deliberately does NOT add an app-level flag or
composition-root wiring yet: the only real consumer of delegation today
would be the spawn_agent tool (DR-2), so a flag would be dead
configuration (02 11.4 discipline). Child conversations are created by
the injected conversation factory; product-visibility filtering belongs
to the composition-root slice.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Sequence

from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildRunStatus,
    InProcessChildCoordinator,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    RuntimeV2AgentKernel,
    ToolExecutionLimits,
    TranscriptEntryType,
)
from endless_task.runtime_ledger import TraceContext
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteDelegationChildRunRepository,
    SqliteRuntimeV2Repository,
)
from endless_task.storage.v2.base import ACTIVE_RUN_STATUSES
from endless_task.tooling import ToolRegistry

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "session.query",
        "memory.read",
    }
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


class HangingProvider:
    """永不返回、也不理会取消令牌的 Provider：模拟子运行被就地取消。"""

    name = "hanging"

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request, cancellation_token
        yield ProviderTextDelta("开始")
        self.entered.set()
        # 挂住，直到宿主（父运行单轮超时 / 任务取消）取消它。
        await asyncio.Event().wait()


class TwoTurnCancelProvider:
    """两轮 provider：第一轮真花掉 token，第二轮挂住等宿主取消。

    用于证明"取消时已经花过 token"这条终态路径：第一轮留下的用量必须在取消
    收尾时回填进库行，而不是被静默写成 0（0 是一句假话，会让 M3 成本账低估）。
    """

    name = "two_turn_cancel"

    def __init__(self) -> None:
        self.requests: list[ProviderRequest] = []
        self.second_turn_entered = asyncio.Event()

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderTextDelta("先查一遍。")
            yield ProviderToolCall(
                id="call_1",
                name="read_file",
                arguments={"path": "a.txt"},
            )
            yield ProviderCompleted(
                finish_reason="tool_calls",
                input_tokens=11,
                output_tokens=7,
            )
            return
        # 第二轮：等测试下达取消。取消令牌一置位就抛出，执行器按取消收尾——
        # 这一轮从未完成，所以它不该被计进用量（部分快照的边界）。
        self.second_turn_entered.set()
        await cancellation_token.wait()
        cancellation_token.raise_if_cancelled()


class FailingProvider:
    name = "failing"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        raise ProviderError(
            "provider_unavailable",
            "模型服务不可用。",
            retryable=True,
        )
        yield  # pragma: no cover - makes this an async generator


def child_spec(tool_call_id: str = "call_1") -> SpawnSpec:
    return SpawnSpec(
        task="调查并总结",
        expected_output=ChildOutputSchema(
            name="research_output",
            schema={"type": "object"},
        ),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read", "session.query"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=120.0,
        parent_run_id="run_parent_1",
        parent_tool_call_id=tool_call_id,
    )


class CoordinatorOverProductionKernelTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.store = SqliteDelegationChildRunRepository(self.database)
        self.executors_created = 0

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _conversation_factory(self):
        async def factory(child_run_id: str, *, workspace_id=None):
            # Composition-root contract: child conversations are tagged so
            # product enumeration excludes them (06 §2.4).
            return self.chat_repository.create_delegation_conversation(
                child_run_id, workspace_id=workspace_id
            )

        return factory

    def _executor_builder(self, provider):
        def build(command) -> AgentRunExecutor:
            del command  # 这些用例只关心执行器本身，不校验它拿到的预算
            self.executors_created += 1
            return AgentRunExecutor(
                repository=self.repository,
                provider=provider,
                tool_registry=ToolRegistry(),
                model="scripted-model",
                tool_execution_limits=ToolExecutionLimits(
                    max_concurrent_calls=1,
                    max_calls_per_turn=4,
                ),
            )

        return build

    def _kernel(self, provider) -> RuntimeV2AgentKernel:
        return RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=self._conversation_factory(),
            executor_builder=self._executor_builder(provider),
        )

    def _coordinator(self, kernel, **overrides) -> InProcessChildCoordinator:
        payload = {
            "parent_run_id": "run_parent_1",
            "parent_capabilities": PARENT_CAPABILITIES,
            "parent_trace": TraceContext(
                trace_id="trace_parent_1",
                run_id="run_parent_1",
                correlation_id="corr_1",
            ),
        }
        payload.update(overrides)
        return InProcessChildCoordinator(kernel, **payload)

    def test_completed_child_runs_on_real_executor(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("调查完成，结论如下。"),
                    ProviderCompleted(
                        finish_reason="stop",
                        input_tokens=10,
                        output_tokens=4,
                    ),
                ),
            ]
        )
        coordinator = self._coordinator(
            self._kernel(provider), child_run_store=self.store
        )

        async def scenario():
            child_run_id = await coordinator.spawn(child_spec())
            outcome = await coordinator.query(child_run_id)
            return child_run_id, outcome

        child_run_id, outcome = asyncio.run(scenario())
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual(child_run_id, outcome.child_run_id)
        self.assertIn("调查完成", outcome.summary)
        # 落库的四个用量列必须是真值——M3 成本闸按它们算账。
        row = self.store.get_child_run(child_run_id)
        self.assertEqual(10, row.input_tokens)
        self.assertEqual(4, row.output_tokens)
        self.assertEqual(1, row.model_turns)
        self.assertEqual(0, row.tool_calls)
        self.assertEqual(10, outcome.usage.input_tokens)
        self.assertEqual(4, outcome.usage.output_tokens)
        self.assertEqual(1, outcome.usage.model_turns)
        # The child really ran on the production surface.
        run = self.repository.get_run(child_run_id)
        from endless_task.runtime_v2 import RunStatus

        self.assertEqual(RunStatus.COMPLETED, run.status)
        self.assertEqual(1, self.executors_created)
        record = coordinator.children[0]
        self.assertEqual(ChildRunStatus.COMPLETED, record.status)
        self.assertTrue(record.decision.read_only)
        # 06 §2.4: the child conversation is tagged and excluded from the
        # product-facing conversation enumeration by default.
        visible_ids = [
            item.id for item in self.chat_repository.list_conversations()
        ]
        self.assertNotIn(run.conversation_id, visible_ids)
        tagged_ids = [
            item.id
            for item in self.chat_repository.list_conversations(
                include_delegation=True
            )
        ]
        self.assertIn(run.conversation_id, tagged_ids)

    def test_cancelled_child_backfills_tokens_from_the_completed_turn(self) -> None:
        """取消也算花过钱：已完成的模型轮的用量必须回填，不能写 0。

        运行行里的四个用量列被 M3 成本闸当作**实际花费**读数。取消路径旧实现写
        0，等于断言"这个子运行一个 token 都没花"——是假话。取消时那颗活动执行器
        已经从内核活动表里摘掉（`run` 的 finally 先于协调器收尾），所以用量只能由
        适配器在摘牌那一刻**留存**的部分快照给出：11/7 是第一轮真值，第二轮从未
        完成，不计入。
        """
        provider = TwoTurnCancelProvider()
        coordinator = self._coordinator(
            self._kernel(provider), child_run_store=self.store
        )

        async def scenario():
            # 阻塞版 spawn 会跑完整条子运行（而第二轮在等取消），所以必须放进
            # 后台任务——这也正是生产里"父运行自己那一轮 await 子运行"的形状。
            spawn_task = asyncio.create_task(coordinator.spawn(child_spec()))
            # 第二轮由取消令牌唤醒，所以"进入第二轮"就是"第一轮已经完成"的确切
            # 信号：此刻执行器的镜像已经是 11/7/1/1。等待加上限，绝不无界挂住。
            await asyncio.wait_for(provider.second_turn_entered.wait(), timeout=10)
            rows = self.store.list_child_runs("run_parent_1")
            self.assertEqual(1, len(rows))
            spawn_task.cancel()
            result = await asyncio.wait_for(
                asyncio.gather(spawn_task, return_exceptions=True), timeout=10
            )
            self.assertIsInstance(result[0], asyncio.CancelledError)
            return rows[0].child_run_id

        child_run_id = asyncio.run(scenario())
        row = self.store.get_child_run(child_run_id)
        self.assertEqual("cancelled", row.state.value)
        # 取消路径的用量是**部分**快照（最后一个已完成模型轮为止），不是 0。
        self.assertEqual(11, row.input_tokens)
        self.assertEqual(7, row.output_tokens)
        self.assertEqual(1, row.model_turns)
        self.assertEqual(1, row.tool_calls)

    def test_kernel_raising_after_a_successful_run_backfills_final_usage(self) -> None:
        """内核成功跑完、之后才抛异常：用量是**最终**值，靠留存快照回填。

        这条路径上 `RunOutcome`（连它的 usage）根本回不到协调器，所以只有适配器
        在 pop 时留存的那份快照能修好它。旧实现写 `failed 0 0 0 0`，而 v2 运行行
        是 `completed`——库行的用量与运行自己的记录互相矛盾。
        """
        class KernelRaisingAfterSuccess(RuntimeV2AgentKernel):
            """父类 `run` 跑完之后再抛：这是"成功跑完却抛异常"的精确复现。"""

            async def run(self, command):
                await super().run(command)
                raise AgentPlatformError(
                    "child_kernel_error",
                    "子运行成功之后拿到结果时炸了。",
                )

        coordinator = self._coordinator(
            KernelRaisingAfterSuccess(
                repository=self.repository,
                conversation_factory=self._conversation_factory(),
                executor_builder=self._executor_builder(
                    ScriptedProvider(
                        [
                            (
                                ProviderTextDelta("调查完成，结论如下。"),
                                ProviderCompleted(
                                    finish_reason="stop",
                                    input_tokens=11,
                                    output_tokens=7,
                                ),
                            ),
                        ]
                    )
                ),
            ),
            child_run_store=self.store,
        )

        async def scenario():
            return await coordinator.spawn(child_spec())

        child_run_id = asyncio.run(scenario())
        row = self.store.get_child_run(child_run_id)
        self.assertEqual("failed", row.state.value)
        self.assertEqual(11, row.input_tokens)
        self.assertEqual(7, row.output_tokens)
        self.assertEqual(1, row.model_turns)
        self.assertEqual(0, row.tool_calls)
        # 底层运行确实成功跑完了：库行的用量才是"真值被丢掉了"的证据。
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(child_run_id).status)

    def test_failed_child_returns_failed_outcome_with_diagnostic(self) -> None:
        coordinator = self._coordinator(self._kernel(FailingProvider()))

        async def scenario():
            child_run_id = await coordinator.spawn(child_spec())
            return await coordinator.query(child_run_id)

        outcome = asyncio.run(scenario())
        self.assertEqual("failed", outcome.status.value)
        self.assertTrue(
            any(d.code == "provider_unavailable" for d in outcome.diagnostics)
        )
        self.assertEqual(1, len(coordinator.children))

    def test_child_prompt_lands_as_user_entry_in_real_conversation(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("完成。"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            return await coordinator.spawn(child_spec())

        child_run_id = asyncio.run(scenario())
        run = self.repository.get_run(child_run_id)
        entries = self.repository.list_entries(run.lane_id)
        self.assertEqual(TranscriptEntryType.USER_MESSAGE, entries[0].type)
        self.assertEqual(Actor.USER, entries[0].actor)
        self.assertIn("调查并总结", entries[0].payload["content"])
        self.assertEqual(TranscriptEntryType.ASSISTANT_MESSAGE, entries[-1].type)

    def test_spawn_with_duplicate_parent_tool_call_rejected(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("一"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            await coordinator.spawn(child_spec(tool_call_id="call_1"))
            with self.assertRaises(Exception) as caught:
                await coordinator.spawn(child_spec(tool_call_id="call_1"))
            return caught.exception

        error = asyncio.run(scenario())
        self.assertEqual("delegation_duplicate_spawn", error.code)
        self.assertEqual(1, len(coordinator.tracked_child_run_ids))

    def test_two_distinct_children_run_on_real_executor(self) -> None:
        provider = ScriptedProvider(
            [
                (
                    ProviderTextDelta("第一份"),
                    ProviderCompleted(finish_reason="stop"),
                ),
                (
                    ProviderTextDelta("第二份"),
                    ProviderCompleted(finish_reason="stop"),
                ),
            ]
        )
        coordinator = self._coordinator(self._kernel(provider))

        async def scenario():
            first = await coordinator.spawn(child_spec(tool_call_id="call_a"))
            second = await coordinator.spawn(child_spec(tool_call_id="call_b"))
            return first, second

        first, second = asyncio.run(scenario())
        self.assertNotEqual(first, second)
        self.assertEqual(2, self.executors_created)
        self.assertEqual(2, len(coordinator.children))
        from endless_task.runtime_v2 import RunStatus

        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(first).status)
        self.assertEqual(RunStatus.COMPLETED, self.repository.get_run(second).status)

    def test_child_run_is_terminal_when_its_task_is_cancelled(self) -> None:
        """父运行的单轮超时/任务取消会就地把子运行取消，子运行必须落终态。

        委派工具（spawn_agent）在**父运行自己的那一轮**里 await 子运行，所以
        父运行超时触发的 ``asyncio.CancelledError`` 直接打在子运行的
        ``execute()`` 上（同一任务）。子运行必须自己收尾成终态，否则它的会话会
        被冻结、显示为需要人工恢复的中断运行。
        """
        provider = HangingProvider()
        child_run_ids: list[str] = []
        base_factory = self._conversation_factory()

        async def recording_factory(child_run_id: str, *, workspace_id=None):
            child_run_ids.append(child_run_id)
            return await base_factory(child_run_id, workspace_id=workspace_id)

        kernel = RuntimeV2AgentKernel(
            repository=self.repository,
            conversation_factory=recording_factory,
            executor_builder=self._executor_builder(provider),
        )
        coordinator = self._coordinator(kernel)

        async def scenario() -> None:
            task = asyncio.create_task(coordinator.spawn(child_spec()))
            await provider.entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())

        self.assertEqual(1, len(child_run_ids))
        child_run = self.repository.get_run(child_run_ids[0])
        self.assertNotIn(child_run.status, ACTIVE_RUN_STATUSES)
        self.assertEqual(RunStatus.CANCELLED, child_run.status)
        event_types = tuple(
            event.event_type
            for event in self.repository.list_runtime_events(child_run.id)
        )
        self.assertIn("run_cancelled", event_types)
        # 协调器没有为这条被取消的子运行留下内存记录（CancelledError 直接穿过
        # `except Exception` 处理器）；这是已知的委派层缺口，不在本次修复范围内。
        self.assertEqual(0, len(coordinator.children))


if __name__ == "__main__":
    unittest.main()
