from __future__ import annotations

import asyncio


class RuntimeCancelled(Exception):
    """Raised when the user has cancelled the active response."""


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise RuntimeCancelled()

    async def wait(self) -> None:
        await self._event.wait()


class CancellationManager:
    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], CancellationToken] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, turn_id: str, variant_id: str) -> CancellationToken:
        key = (turn_id, variant_id)
        async with self._lock:
            token = self._tokens.get(key)
            if token is None:
                token = CancellationToken()
                self._tokens[key] = token
            return token

    async def cancel(self, turn_id: str, variant_id: str) -> bool:
        key = (turn_id, variant_id)
        async with self._lock:
            token = self._tokens.get(key)
            if token is None:
                token = CancellationToken()
                self._tokens[key] = token
            was_cancelled = token.is_cancelled
            token.cancel()
            return not was_cancelled

    async def release(self, turn_id: str, variant_id: str) -> None:
        key = (turn_id, variant_id)
        async with self._lock:
            self._tokens.pop(key, None)
