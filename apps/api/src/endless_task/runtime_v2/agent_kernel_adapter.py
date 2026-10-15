"""Production AgentKernel adapter over the runtime v2 executor (M4A DR-1).

This adapter is the missing production binding between the frozen
:mod:`agent_kernel` protocol and the real run path (02 3.1 / 11.3 gap):

- ``agent_kernel`` stays a pure protocol package (import boundary test),
- this module lives in ``runtime_v2`` and implements the protocol by
  driving :class:`AgentRunExecutor` against the real repository,

A :class:`RunCommand` is executed as an **internal child run**: an
isolated conversation/lane/trigger entry is created, the run is created
with ``run_id=command.run_id`` (protocol requires outcome run id ==
command run id), the executor runs to a terminal state, and the
:class:`RunExecutionResult` is projected back to a :class:`RunOutcome`.

Composition root supplies the two seams this module cannot and must not
assemble itself:

- ``conversation_factory`` — how a child conversation is created
  (workspace binding, ephemeral kind, product-visibility filtering),
- ``executor_builder`` — a fresh :class:`AgentRunExecutor` per run,
  built from the :class:`RunCommand` so the composition root can derive
  the child's own wall clock from ``command.deadline_monotonic`` (the
  real provider/tool/model dependencies stay at composition root).

Cancellation is real, not stubbed: an active run keeps its cancellation
token in a registry, and :meth:`cancel` releases it so the executor
finishes with ``CANCELLED``.

This module *is* integrated: the composition root builds it in
``_delegation_kernel_provider`` for every parent run that can delegate.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from endless_task.agent_kernel import (
    AgentKernel,
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
    RunUsage,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.domain.models import Conversation
from endless_task.runtime.cancellation import CancellationToken

from .domain import Actor, RunStatus, TranscriptEntryType
from .execution import AgentRunExecutor, RunExecutionResult

ConversationFactory = Callable[..., Awaitable[Conversation]]
ExecutorBuilder = Callable[[RunCommand], AgentRunExecutor]

#: 适配器最多留存多少条已结束运行的用量快照（超出丢最老的）。
#: 内核实例活在整个父运行期间、会派很多子运行，所以留存必须有界；
#: 先例是协调器的 ``MAX_RETAINED_TERMINAL_RECORDS = 32``。
MAX_RETAINED_USAGE_SNAPSHOTS = 32


class RuntimeV2AgentKernel(AgentKernel):
    """AgentKernel protocol implementation over ``AgentRunExecutor``."""

    def __init__(
        self,
        *,
        repository,
        conversation_factory: ConversationFactory,
        executor_builder: ExecutorBuilder,
    ) -> None:
        if not callable(getattr(repository, "create_run", None)):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "RuntimeV2AgentKernel requires a runtime v2 repository",
            )
        if not callable(conversation_factory):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "conversation_factory must be callable",
            )
        if not callable(executor_builder):
            raise AgentPlatformError(
                "invalid_agent_kernel_adapter",
                "executor_builder must be callable",
            )
        self._repository = repository
        self._conversation_factory = conversation_factory
        self._executor_builder = executor_builder
        self._active: dict[str, tuple[AgentRunExecutor, CancellationToken]] = {}
        #: 已结束运行的用量快照，按 run_id 留存（有界，插入序丢最老）。
        #: 收尾路径（取消/内核异常/契约违规）在协调器动手时 `_active` 里已经没有
        #: 这个运行了——`run` 的 finally 先于每一个协调器收尾函数——所以"实时查
        #: 活动表"这条缝在需要它的时候必然返回 None。快照只能在摘牌那一刻留存。
        self._last_usage: dict[str, RunUsage] = {}

    async def run(self, command: RunCommand) -> RunOutcome:
        """Execute one child :class:`RunCommand` to a terminal outcome."""
        if not isinstance(command, RunCommand):
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "RuntimeV2AgentKernel.run requires a RunCommand",
            )
        if command.run_id in self._active:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "A child run with this run id is already active",
                details={"run_id": command.run_id},
            )
        workspace_id = command.metadata.get("child_workspace_id")
        conversation = await self._conversation_factory(
            command.run_id, workspace_id=workspace_id
        )
        lane = self._repository.create_lane(conversation_id=conversation.id)
        trigger = self._repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": command.message.content},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        run = self._repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
            run_id=command.run_id,
            correlation_id=command.trace.correlation_id,
        )
        executor = self._executor_builder(command)
        token = CancellationToken()
        self._active[command.run_id] = (executor, token)
        result: Optional[RunExecutionResult] = None
        try:
            result = await executor.execute(command.run_id, cancellation_token=token)
        finally:
            # 与摘牌**同一刻**留存快照：协调器的收尾函数在这一行之后才跑，那时
            # `_active` 已经空了。正常返回用 result 构造最终用量；异常路径（宿主
            # 取消、内核在半路抛出）用执行器的镜像构造部分用量——**那段构造必须
            # 有自己的 try/except**，否则一段坏快照会顶掉原始异常（取消被吞掉、
            # 或真实故障变成假故障）。
            self._active.pop(command.run_id, None)
            self._retain_usage(command.run_id, result, executor)
        assert result is not None  # 上面的 except 路径已经重抛，这里必然有结果
        return _project_run_result(
            result=result,
            command=command,
            repository=self._repository,
        )

    def usage_snapshot(self, run_id: str) -> Optional[RunUsage]:
        """本次运行的用量快照：在飞时取执行器镜像，已结束取运行时留存的那一份。

        协调器按鸭子类型取用它（``getattr(kernel, "usage_snapshot", None)``），
        所以 ``AgentKernel`` Protocol 不必新增成员。
        """
        live = self._active.get(run_id)
        if live is not None:
            return _snapshot_from_executor(live[0])
        return self._last_usage.get(run_id)

    def _retain_usage(
        self,
        run_id: str,
        result: Optional[RunExecutionResult],
        executor: AgentRunExecutor,
    ) -> None:
        """把这次运行的用量快照留存进有界表；只为收尾路径服务，失败绝不外抛。"""
        try:
            if result is not None:
                snapshot = RunUsage(
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    model_turns=len(result.model_turn_ids),
                    tool_calls=result.tool_call_count,
                )
            else:
                snapshot = _snapshot_from_executor(executor)
        except Exception:  # noqa: BLE001 - 留存是旁路，绝不能掩盖原始异常
            return
        self._last_usage[run_id] = snapshot
        while len(self._last_usage) > MAX_RETAINED_USAGE_SNAPSHOTS:
            self._last_usage.pop(next(iter(self._last_usage)), None)

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        entry = self._active.get(run_id)
        if entry is None:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot steer an inactive child run",
                details={"run_id": run_id},
            )
        executor, _ = entry
        await executor.enqueue_user_message(message.content)

    async def cancel(self, run_id: str, reason: str) -> None:
        entry = self._active.get(run_id)
        if entry is None:
            raise AgentPlatformError(
                "invalid_agent_kernel_value",
                "Cannot cancel an inactive child run",
                details={"run_id": run_id},
            )
        executor, token = entry
        token.cancel()
        await executor.request_cancel(cancelled_by="delegation")


def _project_run_result(
    *,
    result: RunExecutionResult,
    command: RunCommand,
    repository,
) -> RunOutcome:
    """Map a :class:`RunExecutionResult` onto the kernel protocol types."""
    status = _map_run_status(result.status)
    diagnostics: list[SafeDiagnostic] = []
    if result.status is RunStatus.WAITING_APPROVAL:
        # Read-only children must never block on approvals; a waiting
        # approval means the child surface leaked a gated tool. Fail
        # closed instead of reporting a non-terminal state as success.
        diagnostics.append(
            SafeDiagnostic(
                code="child_waiting_approval",
                safe_message=(
                    "Child run requested approval, which read-only delegation "
                    "does not allow."
                ),
            )
        )
    elif result.status is RunStatus.FAILED:
        run = repository.get_run(command.run_id)
        diagnostics.append(
            SafeDiagnostic(
                code=run.error_code or "child_run_failed",
                safe_message=run.safe_message or "Child run failed.",
            )
        )
    content = result.content or ""
    assistant_message: Optional[AgentMessage] = None
    if status is RunOutcomeStatus.COMPLETED:
        if not content.strip():
            # A completed kernel run must carry a final assistant message,
            # and empty results cannot be forged into a valid one.
            status = RunOutcomeStatus.FAILED
            diagnostics.append(
                SafeDiagnostic(
                    code="child_empty_result",
                    safe_message="Child run completed without a result message.",
                )
            )
        else:
            assistant_message = AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content=content,
            )
    return RunOutcome(
        run_id=command.run_id,
        status=status,
        trace=command.trace,
        assistant_message=assistant_message,
        diagnostics=tuple(diagnostics),
        # 用量从执行结果直接带出：token 与轮次本来就是执行器算好的，
        # 工具调用数由执行循环自己数（``tool_call_count``），这里 O(1) 映射，
        # 不需要回查工具执行表。
        usage=RunUsage(
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_turns=len(result.model_turn_ids),
            tool_calls=result.tool_call_count,
        ),
    )


def _map_run_status(status: RunStatus) -> RunOutcomeStatus:
    if status is RunStatus.COMPLETED:
        return RunOutcomeStatus.COMPLETED
    if status is RunStatus.CANCELLED:
        return RunOutcomeStatus.CANCELLED
    return RunOutcomeStatus.FAILED


def _snapshot_from_executor(executor: AgentRunExecutor) -> RunUsage:
    """从执行器的实例镜像构造**部分**用量快照（最后一个已完成模型轮为止）。

    执行循环是唯一知道累计量的地方，它把三个计数与工具计数一起镜像在实例上；
    这里只做 O(1) 读取，不回查库、不下钻私有帧。用 ``getattr`` 兜底是因为
    运行时可能拿到替身执行器（测试注入），缺镜像时诚实落 0，绝不编造。
    """
    return RunUsage(
        input_tokens=int(getattr(executor, "_input_tokens", 0) or 0),
        output_tokens=int(getattr(executor, "_output_tokens", 0) or 0),
        model_turns=int(getattr(executor, "_model_turns", 0) or 0),
        tool_calls=int(getattr(executor, "_tool_call_count", 0) or 0),
    )


__all__ = [
    "ConversationFactory",
    "ExecutorBuilder",
    "RuntimeV2AgentKernel",
]
