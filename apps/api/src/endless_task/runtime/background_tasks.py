from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")
DoneCallback = Callable[[asyncio.Task[Any]], None]


class BackgroundTaskSupervisor:
    """Owns detached asyncio tasks for one component.

    Short-lived structured concurrency should still use TaskGroup/timeout at the
    call site. This class is only for tasks whose lifetime outlives the current
    async stack frame.
    """

    def __init__(
        self,
        *,
        name: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._name = name
        self._logger = logger or logging.getLogger(__name__)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def tasks(self) -> tuple[asyncio.Task[Any], ...]:
        return tuple(self._tasks)

    def running_count(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    def spawn(
        self,
        awaitable: Awaitable[T],
        *,
        name: str | None = None,
        on_done: DoneCallback | None = None,
    ) -> asyncio.Task[T]:
        if self._closed:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise RuntimeError(
                f"Background task supervisor {self._name!r} is closed"
            )
        task = asyncio.create_task(
            awaitable,
            name=name,
            context=contextvars.copy_context(),
        )
        self._tasks.add(task)
        task.add_done_callback(
            lambda finished: self._on_task_done(finished, on_done)
        )
        return task

    async def drain(self) -> None:
        while True:
            current = asyncio.current_task()
            pending = tuple(task for task in self._tasks if task is not current)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)

    async def shutdown(self, *, cancel: bool = True) -> None:
        self._closed = True
        if cancel:
            for task in tuple(self._tasks):
                if not task.done():
                    task.cancel()
        await self.drain()

    def _on_task_done(
        self,
        task: asyncio.Task[Any],
        on_done: DoneCallback | None,
    ) -> None:
        self._tasks.discard(task)
        if on_done is not None:
            try:
                on_done(task)
            except Exception:
                self._logger.exception(
                    "Background task done callback failed",
                    extra={
                        "supervisor": self._name,
                        "task_name": task.get_name(),
                    },
                )
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._logger.error(
            "Background task failed",
            exc_info=(type(error), error, error.__traceback__),
            extra={"supervisor": self._name, "task_name": task.get_name()},
        )