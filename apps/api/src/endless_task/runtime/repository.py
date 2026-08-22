from __future__ import annotations

from typing import Optional, Protocol, Sequence, Tuple

from .events import RuntimeEvent


class RuntimeRepository(Protocol):
    def start_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        provider: str,
        model: str,
    ) -> RuntimeEvent:
        ...

    def start_message(self, *, turn_id: str, variant_id: str) -> RuntimeEvent:
        ...

    def append_text_delta(
        self,
        *,
        turn_id: str,
        variant_id: str,
        delta: str,
        accumulated_content: str,
    ) -> RuntimeEvent:
        ...

    def complete_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        content: str,
        finish_reason: str,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
    ) -> Tuple[RuntimeEvent, RuntimeEvent]:
        ...

    def fail_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        error_code: str,
        safe_message: str,
        retryable: bool,
        correlation_id: str,
        retry_after_ms: Optional[int] = None,
    ) -> RuntimeEvent:
        ...

    def cancel_response(
        self,
        *,
        turn_id: str,
        variant_id: str,
        partial_content: str,
        cancelled_by: str = "user",
    ) -> Optional[RuntimeEvent]:
        ...

    def list_events(
        self,
        turn_id: str,
        *,
        after_sequence: int = 0,
    ) -> Sequence[RuntimeEvent]:
        ...

    def recover_interrupted(self) -> Sequence[RuntimeEvent]:
        ...
