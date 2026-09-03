"""Around-tool middleware chain for the version 2 tool platform (AP-104).

A middleware receives the around event and a ``next`` callable; it may run
code before/after invoking ``next`` and may replace the outcome. Middlewares
compose left-to-right: the first middleware is the outermost wrapper, the
last wraps the terminal executor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from endless_task.agent_platform import AgentPlatformError

from .protocol import ToolOutcome

ToolNext = Callable[[], Awaitable[ToolOutcome]]


class ToolMiddleware(Protocol):
    async def __call__(self, event: object, next: ToolNext) -> ToolOutcome: ...


def chain_tool_middleware(
    middlewares: Iterable[ToolMiddleware],
    terminal: ToolNext,
) -> ToolNext:
    """Compose ``middlewares`` around ``terminal`` into a single callable."""
    if isinstance(middlewares, (str, bytes)):
        raise AgentPlatformError(
            "invalid_middleware_chain",
            "Middlewares must be a collection of ToolMiddleware values",
        )
    ordered = tuple(middlewares)
    if any(not callable(middleware) for middleware in ordered):
        raise AgentPlatformError(
            "invalid_middleware_chain",
            "Middlewares must be callable",
        )
    if not callable(terminal):
        raise AgentPlatformError(
            "invalid_middleware_chain",
            "Terminal tool executor must be callable",
        )

    def build(event: object, position: int) -> ToolNext:
        if position >= len(ordered):
            return terminal
        middleware = ordered[position]

        async def step() -> ToolOutcome:
            return await middleware(event, build(event, position + 1))

        return step

    def run(event: object) -> Awaitable[ToolOutcome]:
        return build(event, 0)()

    return run


__all__ = ["ToolMiddleware", "ToolNext", "chain_tool_middleware"]
