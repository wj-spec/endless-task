from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping, Optional, Protocol, Tuple, Union

from .cancellation import CancellationToken


@dataclass(frozen=True)
class ProviderMessage:
    role: str
    content: str
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Tuple["ProviderToolCall", ...] = ()


@dataclass(frozen=True)
class ProviderToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ProviderToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]
    # 模型返回的工具参数无法解析为 JSON 对象时置为错误说明（可恢复）。
    # 此时 arguments 为占位空 dict，运行时将其作为一次失败的工具调用反馈给模型，
    # 而不是把整个 run 判为致命错误。
    parse_error: Optional[str] = None


@dataclass(frozen=True)
class ProviderRequest:
    request_id: str
    model: str
    messages: Tuple[ProviderMessage, ...]
    max_output_tokens: int
    temperature: Optional[float] = None
    tools: Tuple[ProviderToolDefinition, ...] = ()


@dataclass(frozen=True)
class ProviderTextDelta:
    text: str


@dataclass(frozen=True)
class ProviderCompleted:
    finish_reason: str = "stop"
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


ProviderStreamEvent = Union[ProviderTextDelta, ProviderToolCall, ProviderCompleted]


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        safe_message: str,
        *,
        retryable: bool,
        retry_after_ms: Optional[int] = None,
        provider_request_id: Optional[str] = None,
    ) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.provider_request_id = provider_request_id


class ModelProvider(Protocol):
    name: str

    def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        ...
