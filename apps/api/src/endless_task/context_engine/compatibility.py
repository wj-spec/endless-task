from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence

from endless_task.runtime.provider import ProviderMessage

from .protocol import (
    CommitResult,
    CompactionRequest,
    CompactionResult,
    ContextEngine,
    ContextInput,
    ContextRequest,
    ContextSnapshot,
    MaintenanceRequest,
    MaintenanceResult,
    BootstrapRequest,
    TurnOutcome,
)

LegacyMessageAssembler = Callable[[ContextRequest], Sequence[ProviderMessage]]
LegacyTokenEstimator = Callable[[Sequence[ProviderMessage]], int]


class LegacyContextEngineAdapter(ContextEngine):
    """Projects legacy context messages into the v1 ContextEngine boundary."""

    def __init__(
        self,
        assemble_messages: LegacyMessageAssembler,
        *,
        estimate_tokens: LegacyTokenEstimator,
    ) -> None:
        self._assemble_messages = assemble_messages
        self._estimate_tokens = estimate_tokens
        self.inputs: list[ContextInput] = []

    async def bootstrap(self, request: BootstrapRequest) -> None:
        del request

    async def ingest(self, event: ContextInput) -> None:
        self.inputs.append(event)

    async def assemble(self, request: ContextRequest) -> ContextSnapshot:
        messages = tuple(self._assemble_messages(request))
        estimated_tokens = self._estimate_tokens(messages)
        return ContextSnapshot(
            messages=messages,
            segments=(),
            estimated_tokens=estimated_tokens,
            reserved_output_tokens=request.budget.reserved_output_tokens,
            budget=request.budget,
            fingerprint=_message_fingerprint(messages),
        )

    async def maintain(self, request: MaintenanceRequest) -> MaintenanceResult:
        del request
        return MaintenanceResult(changed=False)

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        del request
        return CompactionResult(checkpoint_id=None, released_tokens=0)

    async def commit_turn(self, outcome: TurnOutcome) -> CommitResult:
        del outcome
        return CommitResult(committed=False)


def _message_fingerprint(messages: Sequence[ProviderMessage]) -> str:
    payload = [
        {
            "role": message.role,
            "content": message.content,
            "toolCallId": message.tool_call_id,
            "name": message.name,
            "toolCalls": [
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                    "parseError": call.parse_error,
                }
                for call in message.tool_calls
            ],
        }
        for message in messages
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()