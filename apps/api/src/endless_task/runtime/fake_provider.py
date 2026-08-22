from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional, Sequence

from .cancellation import CancellationToken, RuntimeCancelled
from .provider import (
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
)


class FakeProvider:
    """Deterministic provider used to develop and test the Runtime."""

    name = "fake"

    def __init__(
        self,
        chunks: Sequence[str] = ("你好", "，我是 Endless Task。"),
        *,
        finish_reason: str = "stop",
        input_tokens: int = 10,
        output_tokens: int = 8,
        failure_after_chunks: Optional[int] = None,
        failure: Optional[ProviderError] = None,
        pause_after_chunks: Optional[int] = None,
    ) -> None:
        self._chunks = tuple(chunks)
        self._finish_reason = finish_reason
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._failure_after_chunks = failure_after_chunks
        self._failure = failure or ProviderError(
            "provider_unavailable",
            "模拟模型服务暂不可用。",
            retryable=True,
        )
        self._pause_after_chunks = pause_after_chunks
        self.paused = asyncio.Event()
        self._resume = asyncio.Event()
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        self.requests.append(request)
        emitted = 0

        if self._failure_after_chunks == 0:
            raise self._failure

        for chunk in self._chunks:
            cancellation_token.raise_if_cancelled()
            if self._pause_after_chunks is not None and emitted == self._pause_after_chunks:
                self.paused.set()
                await self._wait_until_resumed_or_cancelled(cancellation_token)

            if chunk:
                yield ProviderTextDelta(chunk)
                emitted += 1

            if self._failure_after_chunks == emitted:
                raise self._failure

            await asyncio.sleep(0)

        cancellation_token.raise_if_cancelled()
        yield ProviderCompleted(
            finish_reason=self._finish_reason,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )

    def resume(self) -> None:
        self._resume.set()

    async def _wait_until_resumed_or_cancelled(
        self,
        cancellation_token: CancellationToken,
    ) -> None:
        resume_task = asyncio.create_task(self._resume.wait())
        cancel_task = asyncio.create_task(cancellation_token.wait())
        done, pending = await asyncio.wait(
            (resume_task, cancel_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done:
            raise RuntimeCancelled()
