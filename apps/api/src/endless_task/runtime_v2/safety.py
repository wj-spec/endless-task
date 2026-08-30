from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class SafetyStopReason(str, Enum):
    DUPLICATE_TOOL_SIGNATURE = "duplicate_tool_signature"
    CONSECUTIVE_TOOL_FAILURES = "consecutive_tool_failures"
    NO_PROGRESS = "no_progress"
    MAX_MODEL_TURNS = "max_model_turns"


class SafetyStopError(RuntimeError):
    def __init__(self, reason: SafetyStopReason, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason = reason
        self.code = reason.value
        self.safe_message = safe_message


@dataclass
class SafetyStopState:
    seen_tool_signatures: set[tuple[str, str]] = field(default_factory=set)
    consecutive_failed_tools: int = 0
    consecutive_empty_model_turns: int = 0


@dataclass(frozen=True)
class SafetyStopPolicy:
    max_consecutive_tool_failures: int = 3
    max_consecutive_empty_model_turns: int = 2

    def register_tool_signature(
        self,
        state: SafetyStopState,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> None:
        canonical_arguments = json.dumps(
            dict(arguments),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        signature = (tool_name, canonical_arguments)
        if signature in state.seen_tool_signatures:
            raise SafetyStopError(
                SafetyStopReason.DUPLICATE_TOOL_SIGNATURE,
                "模型重复请求了相同操作，已安全停止。",
            )
        state.seen_tool_signatures.add(signature)

    def register_tool_result(
        self,
        state: SafetyStopState,
        *,
        succeeded: bool,
    ) -> None:
        if succeeded:
            state.consecutive_failed_tools = 0
            return
        state.consecutive_failed_tools += 1
        if state.consecutive_failed_tools >= self.max_consecutive_tool_failures:
            raise SafetyStopError(
                SafetyStopReason.CONSECUTIVE_TOOL_FAILURES,
                "工具连续失败且没有有效进展，已安全停止。",
            )

    def register_model_turn(
        self,
        state: SafetyStopState,
        *,
        content: str,
        tool_call_count: int,
    ) -> None:
        if content.strip() or tool_call_count:
            state.consecutive_empty_model_turns = 0
            return
        state.consecutive_empty_model_turns += 1
        if state.consecutive_empty_model_turns >= self.max_consecutive_empty_model_turns:
            raise SafetyStopError(
                SafetyStopReason.NO_PROGRESS,
                "模型连续返回空结果，已安全停止。",
            )
