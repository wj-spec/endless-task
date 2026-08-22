from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from endless_task.domain.models import (
    ResponseVariantOperation,
    TurnSnapshot,
    TurnStatus,
)
from endless_task.domain.repositories import ChatRepository, InvalidStateError

from .assistant import AssistantRuntime


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnHandle:
    conversation_id: str
    turn_id: str
    response_variant_id: str


class TurnController:
    """Owns in-process execution tasks; HTTP transport is added in R0.3."""

    def __init__(
        self,
        *,
        chat_repository: ChatRepository,
        runtime: AssistantRuntime,
    ) -> None:
        self._chat_repository = chat_repository
        self._runtime = runtime
        self._tasks: dict[tuple[str, str], asyncio.Task[TurnSnapshot]] = {}
        self._lock = asyncio.Lock()

    async def submit(
        self,
        *,
        conversation_id: str,
        client_request_id: str,
        content: str,
    ) -> TurnHandle:
        snapshot = self._chat_repository.create_turn(
            conversation_id=conversation_id,
            client_request_id=client_request_id,
            content=content,
        )
        initial_variant = next(
            item.variant
            for item in snapshot.response_variants
            if item.variant.index == 1
            and item.variant.operation is ResponseVariantOperation.CREATE
        )
        return await self._schedule_created(
            snapshot,
            expected_variant_id=initial_variant.id,
        )

    async def retry(
        self,
        *,
        turn_id: str,
        command_request_id: str,
    ) -> TurnHandle:
        result = self._chat_repository.create_response_variant(
            turn_id=turn_id,
            command_request_id=command_request_id,
            operation=ResponseVariantOperation.RETRY,
        )
        return await self._schedule_created(
            result.turn_snapshot,
            expected_variant_id=result.response_variant_id,
        )

    async def regenerate(
        self,
        *,
        turn_id: str,
        command_request_id: str,
    ) -> TurnHandle:
        result = self._chat_repository.create_response_variant(
            turn_id=turn_id,
            command_request_id=command_request_id,
            operation=ResponseVariantOperation.REGENERATE,
        )
        return await self._schedule_created(
            result.turn_snapshot,
            expected_variant_id=result.response_variant_id,
        )

    async def cancel(self, *, turn_id: str) -> bool:
        snapshot = self._chat_repository.get_turn(turn_id)
        if snapshot.turn.status not in (TurnStatus.CREATED, TurnStatus.RUNNING):
            return False
        variant_id = snapshot.turn.active_response_variant_id
        if variant_id is None:
            raise InvalidStateError("Active turn has no response variant")
        return await self._runtime.request_cancel(turn_id=turn_id, variant_id=variant_id)

    async def wait(self, handle: TurnHandle) -> TurnSnapshot:
        key = (handle.turn_id, handle.response_variant_id)
        async with self._lock:
            task = self._tasks.get(key)
        if task is None:
            return self._chat_repository.get_turn(handle.turn_id)
        return await task

    async def shutdown(self) -> None:
        async with self._lock:
            items = tuple(self._tasks.items())
        for (turn_id, variant_id), task in items:
            if not task.done():
                await self._runtime.request_cancel(
                    turn_id=turn_id,
                    variant_id=variant_id,
                )
        if items:
            await asyncio.gather(*(task for _, task in items), return_exceptions=True)

    async def _schedule_created(
        self,
        snapshot: TurnSnapshot,
        *,
        expected_variant_id: Optional[str] = None,
    ) -> TurnHandle:
        variant_id = expected_variant_id or snapshot.turn.active_response_variant_id
        if variant_id is None:
            raise InvalidStateError("Turn has no active response variant")
        handle = TurnHandle(
            conversation_id=snapshot.turn.conversation_id,
            turn_id=snapshot.turn.id,
            response_variant_id=variant_id,
        )
        key = (handle.turn_id, handle.response_variant_id)

        async with self._lock:
            existing = self._tasks.get(key)
            if existing is not None and not existing.done():
                return handle

            current = self._chat_repository.get_turn(handle.turn_id)
            if current.turn.active_response_variant_id != variant_id:
                return handle
            if current.turn.status is not TurnStatus.CREATED:
                return handle

            task = asyncio.create_task(
                self._runtime.execute(
                    turn_id=handle.turn_id,
                    variant_id=handle.response_variant_id,
                )
            )
            self._tasks[key] = task
            task.add_done_callback(
                lambda finished, task_key=key: self._on_task_done(task_key, finished)
            )
        return handle

    def _on_task_done(
        self,
        key: tuple[str, str],
        task: asyncio.Task[TurnSnapshot],
    ) -> None:
        self._tasks.pop(key, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Turn execution task failed before state convergence",
                exc_info=(type(error), error, error.__traceback__),
                extra={"turn_id": key[0], "variant_id": key[1]},
            )
