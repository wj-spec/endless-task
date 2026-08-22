from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Awaitable, Callable, Optional

from endless_task.domain.repositories import InvalidStateError
from endless_task.tooling import (
    ApprovalStatus,
    RegisteredTool,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolActivityCopy,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
    ToolResult,
    ToolValidationError,
)

from .cancellation import CancellationToken, RuntimeCancelled
from .events import RuntimeEvent
from .repository import RuntimeRepository


class ApprovalCoordinator:
    """Connects persisted approval decisions to the in-process Agent Loop."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[ApprovalStatus]] = {}
        self._lock = asyncio.Lock()

    async def register(self, approval_id: str) -> asyncio.Future[ApprovalStatus]:
        async with self._lock:
            existing = self._waiters.get(approval_id)
            if existing is not None:
                return existing
            future = asyncio.get_running_loop().create_future()
            self._waiters[approval_id] = future
            return future

    async def resolve(self, approval_id: str, status: ApprovalStatus) -> None:
        async with self._lock:
            future = self._waiters.get(approval_id)
            if future is not None and not future.done():
                future.set_result(status)

    async def discard(self, approval_id: str) -> None:
        async with self._lock:
            self._waiters.pop(approval_id, None)

    async def wait(
        self,
        approval_id: str,
        future: asyncio.Future[ApprovalStatus],
        cancellation_token: CancellationToken,
        *,
        timeout_seconds: float,
    ) -> ApprovalStatus:
        cancellation = asyncio.create_task(cancellation_token.wait())
        try:
            done, _ = await asyncio.wait(
                (future, cancellation),
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                raise RuntimeCancelled()
            if future not in done:
                return ApprovalStatus.EXPIRED
            return future.result()
        finally:
            if not cancellation.done():
                cancellation.cancel()
            await asyncio.gather(cancellation, return_exceptions=True)


class RuntimeToolExecutionObserver:
    def __init__(
        self,
        *,
        repository: RuntimeRepository,
        coordinator: ApprovalCoordinator,
        publish: Callable[[RuntimeEvent], Awaitable[None]],
        approval_timeout_seconds: float,
    ) -> None:
        self._repository = repository
        self._coordinator = coordinator
        self._publish = publish
        self._approval_timeout_seconds = approval_timeout_seconds
        self._activities: dict[str, ToolActivityCopy] = {}

    async def prepare(
        self,
        tool: RegisteredTool,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> Optional[ToolCall]:
        prompt = None
        activity = self._activity_copy(tool, call)
        if tool.definition.approval_mode is ToolApprovalMode.REQUIRED:
            prompt = self._approval_prompt(tool, call)
        approval, event = self._repository.prepare_tool_call(
            call=call,
            definition=tool.definition,
            approval_prompt=prompt,
        )
        if approval is None:
            running = replace(call, status=ToolCallStatus.RUNNING)
            self._activities[call.id] = activity
            await self._publish(self._repository.start_tool_call(running, activity))
            return running

        future = await self._coordinator.register(approval.id)
        if event is not None:
            await self._publish(event)
        try:
            status = await self._coordinator.wait(
                approval.id,
                future,
                cancellation_token,
                timeout_seconds=self._approval_timeout_seconds,
            )
            if status is ApprovalStatus.EXPIRED:
                _, resolved_event = self._repository.resolve_approval(
                    approval.id,
                    ApprovalStatus.EXPIRED,
                )
                if resolved_event is not None:
                    await self._publish(resolved_event)
            if status is ApprovalStatus.APPROVED:
                running = replace(call, status=ToolCallStatus.RUNNING)
                self._activities[call.id] = activity
                await self._publish(self._repository.start_tool_call(running, activity))
                return running
            return None
        except (RuntimeCancelled, asyncio.CancelledError):
            try:
                _, resolved_event = self._repository.resolve_approval(
                    approval.id,
                    ApprovalStatus.CANCELLED,
                )
            except InvalidStateError:
                resolved_event = None
            if resolved_event is not None:
                await self._publish(resolved_event)
            raise
        finally:
            await self._coordinator.discard(approval.id)

    async def completed(self, call: ToolCall, result: ToolResult) -> None:
        activity = self._activities.pop(call.id, self._default_activity(call))
        event = self._repository.complete_tool_call(
            call,
            result_truncated=result.is_truncated,
            activity=activity,
        )
        await self._publish(event)

    async def failed(self, call: ToolCall, error_code: str) -> None:
        activity = self._activities.pop(call.id, self._default_activity(call))
        event = self._repository.fail_tool_call(
            call,
            error_code=error_code,
            activity=activity,
        )
        await self._publish(event)

    async def cancelled(self, call: ToolCall) -> None:
        activity = self._activities.pop(call.id, self._default_activity(call))
        event = self._repository.cancel_tool_call(call, activity=activity)
        await self._publish(event)

    @staticmethod
    def _approval_prompt(
        tool: RegisteredTool,
        call: ToolCall,
    ) -> ToolApprovalPrompt:
        factory = getattr(tool, "approval_prompt", None)
        if callable(factory):
            prompt = factory(call)
            if not isinstance(prompt, ToolApprovalPrompt):
                raise ToolValidationError(
                    "invalid_approval_prompt",
                    "Tool approval prompt must use ToolApprovalPrompt",
                )
            return prompt

        impact = (
            "这会修改本机数据。"
            if tool.definition.effect is ToolEffect.LOCAL_WRITE
            else "这会向本机之外的服务发起操作。"
        )
        return ToolApprovalPrompt(
            summary=f"允许 Endless 执行“{tool.definition.name}”吗？",
            reason=f"{tool.definition.description} {impact}",
            metadata={
                "toolName": tool.definition.name,
                "effect": tool.definition.effect.value,
            },
        )

    @classmethod
    def _activity_copy(
        cls,
        tool: RegisteredTool,
        call: ToolCall,
    ) -> ToolActivityCopy:
        factory = getattr(tool, "activity_copy", None)
        if callable(factory):
            activity = factory(call)
            if not isinstance(activity, ToolActivityCopy):
                raise ToolValidationError(
                    "invalid_activity_copy",
                    "Tool activity copy must use ToolActivityCopy",
                )
            return activity
        return cls._default_activity(call)

    @staticmethod
    def _default_activity(call: ToolCall) -> ToolActivityCopy:
        del call
        return ToolActivityCopy(
            running="正在处理所需信息",
            completed="已完成所需处理",
            failed="处理失败，可以重试",
            cancelled="已停止处理",
        )
