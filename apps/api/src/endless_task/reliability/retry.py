"""Retry classification and budgets (M3A, doc 05 §4.1/§4.2).

Pure protocol layer for explainable, budgeted retry:

- a shared error vocabulary (``ErrorCategory``) and a default classifier over
  provider/tool error codes; authentication, quota, invalid-request and
  policy-denied errors are never auto-retried,
- ``RetryDecision`` carries scope, delay, budget consumption and idempotency
  requirements per doc 05,
- ``RetryBudget``/``Deadline`` enforce per-run limits with fail-closed
  consumption checks,
- tools are only auto-retried when the decision allows it AND the tool is
  ``safe`` (or ``key_required`` with an idempotency key); unsafe/unknown
  tools are never auto-retried (RS-3).

No provider adapter or runtime state is touched here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    require_identifier,
    require_protocol_version,
    require_text,
)
from endless_task.tool_platform import IdempotencyPolicy

RETRY_PROTOCOL_VERSION = 1

#: Fixed vocabulary from doc 05 §4.1.
RETRYABLE_CATEGORIES = frozenset(
    {
        "transient_network",
        "rate_limit",
        "provider_overloaded",
        "provider_timeout",
        "context_overflow",
        "tool_timeout",
        "tool_transient",
    }
)
NEVER_RETRY_CATEGORIES = frozenset(
    {
        "invalid_request",
        "authentication",
        "quota_exhausted",
        "tool_invalid_input",
        "policy_denied",
        "unknown_outcome",
    }
)


class ErrorCategory(str, Enum):
    TRANSIENT_NETWORK = "transient_network"
    RATE_LIMIT = "rate_limit"
    PROVIDER_OVERLOADED = "provider_overloaded"
    PROVIDER_TIMEOUT = "provider_timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_TRANSIENT = "tool_transient"
    TOOL_INVALID_INPUT = "tool_invalid_input"
    POLICY_DENIED = "policy_denied"
    UNKNOWN_OUTCOME = "unknown_outcome"


class RetryScope(str, Enum):
    REQUEST = "request"
    TURN = "turn"
    PROVIDER = "provider"
    TOOL = "tool"


#: Default code -> category mapping. Provider adapters may supply their own
#: map at wiring time; unknown codes classify as non-retryable (fail closed).
DEFAULT_ERROR_CODE_MAP: Mapping[str, ErrorCategory] = {
    # provider (openai-compatible adapter normalization)
    "network_error": ErrorCategory.TRANSIENT_NETWORK,
    "provider_unavailable": ErrorCategory.PROVIDER_OVERLOADED,
    "rate_limited": ErrorCategory.RATE_LIMIT,
    "request_timeout": ErrorCategory.PROVIDER_TIMEOUT,
    "authentication_failed": ErrorCategory.AUTHENTICATION,
    "permission_denied": ErrorCategory.POLICY_DENIED,
    "context_too_large": ErrorCategory.CONTEXT_OVERFLOW,
    "content_filtered": ErrorCategory.POLICY_DENIED,
    "unsupported_provider_event": ErrorCategory.INVALID_REQUEST,
    "invalid_provider_response": ErrorCategory.INVALID_REQUEST,
    "provider_not_configured": ErrorCategory.AUTHENTICATION,
    # tool (workspace/shell/mcp normalization)
    "tool_timeout": ErrorCategory.TOOL_TIMEOUT,
    "mcp_tool_timeout": ErrorCategory.TOOL_TIMEOUT,
    "mcp_tool_failed": ErrorCategory.TOOL_TRANSIENT,
    "mcp_server_disconnected": ErrorCategory.TOOL_TRANSIENT,
    "tool_invalid_input": ErrorCategory.TOOL_INVALID_INPUT,
    "path_not_found": ErrorCategory.TOOL_INVALID_INPUT,
    "binary_content": ErrorCategory.TOOL_INVALID_INPUT,
    "file_too_large": ErrorCategory.TOOL_INVALID_INPUT,
    "approval_denied": ErrorCategory.POLICY_DENIED,
    "denied_by_extension": ErrorCategory.POLICY_DENIED,
    "unknown_outcome": ErrorCategory.UNKNOWN_OUTCOME,
    "tool_execution_unknown": ErrorCategory.UNKNOWN_OUTCOME,
}

_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 30.0


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    scope: RetryScope
    delay_seconds: float
    consumes_budget: bool
    requires_idempotency_key: bool
    failover_allowed: bool
    reason: str
    schema_version: int = RETRY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=RETRY_PROTOCOL_VERSION,
            protocol="retry_decision",
        )
        if not isinstance(self.retryable, bool) or not isinstance(
            self.scope, RetryScope
        ):
            raise AgentPlatformError(
                "invalid_retry_decision",
                "Retry decision flags and scope must be protocol values",
            )
        if isinstance(self.delay_seconds, bool) or not isinstance(
            self.delay_seconds, (int, float)
        ):
            raise AgentPlatformError(
                "invalid_retry_decision",
                "Retry delay must be numeric",
            )
        delay = float(self.delay_seconds)
        if not math.isfinite(delay) or delay < 0:
            raise AgentPlatformError(
                "invalid_retry_decision",
                "Retry delay must be a finite non-negative number",
            )
        object.__setattr__(self, "delay_seconds", delay)
        for field_name in (
            "consumes_budget",
            "requires_idempotency_key",
            "failover_allowed",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise AgentPlatformError(
                    "invalid_retry_decision",
                    f"{field_name} must be boolean",
                )
        object.__setattr__(
            self,
            "reason",
            require_text(self.reason, field_name="reason", max_length=256),
        )


class RetryClassifier(Protocol):
    def classify(
        self,
        code: str,
        *,
        retry_after_ms: Optional[int] = None,
    ) -> RetryDecision: ...


class DefaultRetryClassifier:
    """Deterministic classifier over the default error-code map."""

    def __init__(
        self,
        *,
        code_map: Optional[Mapping[str, ErrorCategory]] = None,
    ) -> None:
        self._code_map = dict(code_map) if code_map is not None else dict(
            DEFAULT_ERROR_CODE_MAP
        )

    def classify(
        self,
        code: str,
        *,
        retry_after_ms: Optional[int] = None,
    ) -> RetryDecision:
        normalized = require_identifier(code, field_name="error_code", max_length=128)
        category = self._code_map.get(normalized)
        if category is None:
            return RetryDecision(
                retryable=False,
                scope=RetryScope.REQUEST,
                delay_seconds=0.0,
                consumes_budget=False,
                requires_idempotency_key=False,
                failover_allowed=False,
                reason="unknown_error_category",
            )
        category_name = category.value
        if category_name in NEVER_RETRY_CATEGORIES:
            scope = (
                RetryScope.TOOL
                if category_name in {
                    "tool_invalid_input",
                    "policy_denied",
                    "unknown_outcome",
                }
                else RetryScope.REQUEST
            )
            return RetryDecision(
                retryable=False,
                scope=scope,
                delay_seconds=0.0,
                consumes_budget=False,
                requires_idempotency_key=False,
                failover_allowed=False,
                reason=f"never_retry:{category_name}",
            )
        provider_scoped = category_name.startswith("provider_") or category_name in {
            "transient_network",
            "rate_limit",
        }
        scope = RetryScope.PROVIDER if provider_scoped else RetryScope.TOOL
        delay = self._delay(category_name, retry_after_ms)
        if category_name == "context_overflow":
            return RetryDecision(
                retryable=True,
                scope=RetryScope.TURN,
                delay_seconds=0.0,
                consumes_budget=True,
                requires_idempotency_key=False,
                failover_allowed=False,
                reason="compact_then_retry",
            )
        return RetryDecision(
            retryable=True,
            scope=scope,
            delay_seconds=delay,
            consumes_budget=True,
            requires_idempotency_key=category_name == "tool_timeout",
            failover_allowed=category_name in {
                "transient_network",
                "provider_overloaded",
                "provider_timeout",
            },
            reason=f"retryable:{category_name}",
        )

    def _delay(self, category_name: str, retry_after_ms: Optional[int]) -> float:
        if category_name == "rate_limit" and retry_after_ms is not None:
            if not isinstance(retry_after_ms, int) or retry_after_ms < 0:
                raise AgentPlatformError(
                    "invalid_retry_after",
                    "retry_after_ms must be a non-negative integer",
                )
            return min(max(retry_after_ms / 1000.0, 0.0), _MAX_DELAY_SECONDS)
        if category_name in {"provider_timeout", "transient_network"}:
            return _BASE_DELAY_SECONDS
        return _BASE_DELAY_SECONDS * 2.0


def tool_auto_retry_allowed(
    *,
    idempotency: IdempotencyPolicy,
    decision: RetryDecision,
    has_idempotency_key: bool = False,
) -> bool:
    """RS-3 gate: only safe/idempotent tools may auto-retry."""
    if not isinstance(idempotency, IdempotencyPolicy):
        raise AgentPlatformError(
            "invalid_retry_gate",
            "Tool auto-retry requires an IdempotencyPolicy",
        )
    if not isinstance(decision, RetryDecision):
        raise AgentPlatformError(
            "invalid_retry_gate",
            "Tool auto-retry requires a RetryDecision",
        )
    if not decision.retryable:
        return False
    if idempotency is IdempotencyPolicy.UNSAFE or idempotency is IdempotencyPolicy.UNKNOWN:
        return False
    if idempotency is IdempotencyPolicy.SAFE:
        # A safe tool needs no idempotency key; a decision that demands one
        # (tool timeout) is not auto-retried for key-less safe tools.
        return not decision.requires_idempotency_key
    if decision.requires_idempotency_key:
        return has_idempotency_key and idempotency is IdempotencyPolicy.KEY_REQUIRED
    return idempotency is IdempotencyPolicy.KEY_REQUIRED


@dataclass(frozen=True)
class Deadline:
    deadline_monotonic: float
    schema_version: int = RETRY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=RETRY_PROTOCOL_VERSION,
            protocol="retry_deadline",
        )
        if isinstance(self.deadline_monotonic, bool) or not isinstance(
            self.deadline_monotonic, (int, float)
        ):
            raise AgentPlatformError(
                "invalid_retry_deadline",
                "Deadline must be numeric",
            )
        if not math.isfinite(float(self.deadline_monotonic)):
            raise AgentPlatformError(
                "invalid_retry_deadline",
                "Deadline must be finite",
            )

    def allows(self, now: float, delay_seconds: float = 0.0) -> bool:
        return now + float(delay_seconds) <= float(self.deadline_monotonic)


@dataclass(frozen=True)
class RetryBudget:
    """Per-run retry budget counters; consumption is immutable and capped."""

    provider_request_attempts: int = 0
    provider_failovers: int = 0
    tool_attempts: int = 0
    total_retry_delay: float = 0.0
    context_overflow_compactions: int = 0
    schema_version: int = RETRY_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=RETRY_PROTOCOL_VERSION,
            protocol="retry_budget",
        )
        for field_name in (
            "provider_request_attempts",
            "provider_failovers",
            "tool_attempts",
            "context_overflow_compactions",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AgentPlatformError(
                    "invalid_retry_budget",
                    f"{field_name} must be a non-negative integer",
                )
        delay = self.total_retry_delay
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise AgentPlatformError(
                "invalid_retry_budget",
                "total_retry_delay must be numeric",
            )
        if not math.isfinite(float(delay)) or float(delay) < 0:
            raise AgentPlatformError(
                "invalid_retry_budget",
                "total_retry_delay must be a finite non-negative number",
            )
        object.__setattr__(self, "total_retry_delay", float(delay))


def consume_budget(
    budget: RetryBudget,
    decision: RetryDecision,
    *,
    max_provider_attempts: int,
    max_tool_attempts: int,
    max_failovers: int,
    max_retry_delay: float,
    deadline: Optional[Deadline] = None,
    now: Optional[float] = None,
) -> tuple[RetryBudget, bool]:
    """Immutable budget consumption; fails closed when any cap is exceeded."""
    if not isinstance(budget, RetryBudget) or not isinstance(decision, RetryDecision):
        raise AgentPlatformError(
            "invalid_retry_budget",
            "Budget consumption requires a RetryBudget and RetryDecision",
        )
    for name, value in (
        ("max_provider_attempts", max_provider_attempts),
        ("max_tool_attempts", max_tool_attempts),
        ("max_failovers", max_failovers),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AgentPlatformError(
                "invalid_retry_budget",
                f"{name} must be a non-negative integer",
            )
    if (
        isinstance(max_retry_delay, bool)
        or not isinstance(max_retry_delay, (int, float))
        or float(max_retry_delay) < 0
    ):
        raise AgentPlatformError(
            "invalid_retry_budget",
            "max_retry_delay must be a non-negative number",
        )
    if not decision.retryable:
        return budget, False
    if deadline is not None and now is not None:
        if not deadline.allows(now, decision.delay_seconds):
            return budget, False
    scope = decision.scope
    if scope is RetryScope.PROVIDER or scope is RetryScope.REQUEST:
        if budget.provider_request_attempts >= max_provider_attempts:
            return budget, False
        attempts = budget.provider_request_attempts + 1
        failovers = (
            budget.provider_failovers + 1
            if decision.failover_allowed and budget.provider_failovers < max_failovers
            else budget.provider_failovers
        )
    elif scope is RetryScope.TOOL:
        if budget.tool_attempts >= max_tool_attempts:
            return budget, False
        attempts = budget.tool_attempts + 1
        failovers = budget.provider_failovers
    else:  # TURN-level (context overflow): count compaction, no delay cap issue
        if decision.reason != "compact_then_retry":
            return budget, False
        return RetryBudget(
            provider_request_attempts=budget.provider_request_attempts,
            provider_failovers=budget.provider_failovers,
            tool_attempts=budget.tool_attempts,
            total_retry_delay=budget.total_retry_delay,
            context_overflow_compactions=budget.context_overflow_compactions + 1,
        ), True
    new_delay = budget.total_retry_delay + decision.delay_seconds
    if new_delay > float(max_retry_delay):
        return budget, False
    return RetryBudget(
        provider_request_attempts=attempts,
        provider_failovers=failovers,
        tool_attempts=budget.tool_attempts,
        total_retry_delay=new_delay,
        context_overflow_compactions=budget.context_overflow_compactions,
    ), True


__all__ = [
    "DEFAULT_ERROR_CODE_MAP",
    "Deadline",
    "DefaultRetryClassifier",
    "ErrorCategory",
    "NEVER_RETRY_CATEGORIES",
    "RETRY_PROTOCOL_VERSION",
    "RETRYABLE_CATEGORIES",
    "RetryBudget",
    "RetryClassifier",
    "RetryDecision",
    "RetryScope",
    "consume_budget",
    "tool_auto_retry_allowed",
]
