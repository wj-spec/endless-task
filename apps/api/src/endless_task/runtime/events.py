from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Mapping, Optional, Protocol


@dataclass(frozen=True)
class RuntimeEvent:
    version: int
    event_id: str
    sequence: int
    type: str
    conversation_id: str
    turn_id: str
    occurred_at: str
    data: Mapping[str, object]
    response_variant_id: Optional[str] = None
    message_id: Optional[str] = None


class EventPublisher(Protocol):
    async def publish(self, event: RuntimeEvent) -> None:
        ...


class NullEventPublisher:
    async def publish(self, event: RuntimeEvent) -> None:
        del event


class RecordingEventPublisher:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def publish(self, event: RuntimeEvent) -> None:
        self.events.append(event)


class RuntimeEventBroker:
    """Best-effort live notification; the persisted journal remains authoritative."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[RuntimeEvent]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: RuntimeEvent) -> None:
        async with self._lock:
            queues = tuple(self._subscribers.get(event.turn_id, ()))
        for queue in queues:
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(
        self,
        turn_id: str,
    ) -> AsyncIterator[asyncio.Queue[RuntimeEvent]]:
        queue: asyncio.Queue[RuntimeEvent] = asyncio.Queue()
        async with self._lock:
            self._subscribers.setdefault(turn_id, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(turn_id)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(turn_id, None)
