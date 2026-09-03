"""Provider retry orchestration policy (M3A RS-1).

Pure, deterministic evaluation used by the runtime around provider stream
consumption (05 RS-1): classify provider error codes, respect budgets and
deadlines, and only ever retry **before** any stream event has been emitted
(retrying after partial output would duplicate model text/tool calls).

Default configuration is ``max_attempts=0``: the runtime records a shadow
decision (``ProviderRetryRecord``) for every provider failure and never
retries, exactly as doc 05 requires ("保持默认 retry=0，先只记录 shadow
decision").
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    require_protocol_version,
)

from .retry import (
    Deadline,
    DefaultRetryClassifier,
    RetryClassifier,
    RetryDecision,
    RetryScope,
)

RETRY_RUNTIME_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ProviderRetryConfig:
    max_attempts: int = 0
    max_total_retry_delay: float = 30.0
    deadline_seconds: Optional[float] = None
    schema_version: int = RETRY_RUNTIME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_protocol_version(
            self.schema_version,
            expected=RETRY_RUNTIME_SCHEMA_VERSION,
            protocol="provider_retry_config",
        )
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 0
        ):
            raise AgentPlatformError(
                "invalid_retry_config",
                "provider retry max_attempts must be a non-negative integer",
            )
        delay = self.max_total_retry_delay
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise AgentPlatformError(
                "invalid_retry_config",
                "max_total_retry_delay must be numeric",
            )
        if not math.isfinite(float(delay)) or float(delay) < 0:
            raise AgentPlatformError(
                "invalid_retry_config",
                "max_total_retry_delay must be finite and non-negative",
            )
        if self.deadline_seconds is not None:
            if isinstance(self.deadline_seconds, bool) or not isinstance(
                self.deadline_seconds, (int, float)
            ):
                raise AgentPlatformError(
                    "invalid_retry_config",
                    "deadline_seconds must be numeric",
                )
            if not math.isfinite(float(self.deadline_seconds)) or float(
                self.deadline_seconds
            ) <= 0:
                raise AgentPlatformError(
                    "invalid_retry_config",
                    "deadline_seconds must be finite and positive",
                )


@dataclass(frozen=True)
class ProviderRetryRecord:
    """One evaluation result; also the shadow record when retry is off."""

    error_code: str
    retryable: bool
    would_retry: bool
    delay_seconds: float
    attempts_used: int
    reason: str
    schema_version: int = RETRY_RUNTIME_SCHEMA_VERSION


class ProviderRetryObserver(Protocol):
    def __call__(self, record: ProviderRetryRecord) -> None: ...


class ProviderRetryEvaluator:
    """Classify + budget + deadline evaluation for one provider failure."""

    def __init__(
        self,
        config: ProviderRetryConfig,
        *,
        classifier: Optional[RetryClassifier] = None,
    ) -> None:
        if not isinstance(config, ProviderRetryConfig):
            raise AgentPlatformError(
                "invalid_retry_config",
                "Evaluator requires a ProviderRetryConfig",
            )
        self._config = config
        self._classifier = classifier if classifier is not None else DefaultRetryClassifier()

    def evaluate(
        self,
        *,
        error_code: str,
        attempts_used: int,
        emitted_events: bool,
        retry_after_ms: Optional[int] = None,
        now: Optional[float] = None,
    ) -> ProviderRetryRecord:
        if not isinstance(attempts_used, int) or attempts_used < 0:
            raise AgentPlatformError(
                "invalid_retry_config",
                "attempts_used must be a non-negative integer",
            )
        if not isinstance(emitted_events, bool):
            raise AgentPlatformError(
                "invalid_retry_config",
                "emitted_events must be boolean",
            )
        decision = self._classifier.classify(error_code, retry_after_ms=retry_after_ms)
        would_retry = self._would_retry(
            decision=decision,
            attempts_used=attempts_used,
            emitted_events=emitted_events,
            now=now,
        )
        return ProviderRetryRecord(
            error_code=error_code,
            retryable=decision.retryable,
            would_retry=would_retry,
            delay_seconds=decision.delay_seconds if would_retry else 0.0,
            attempts_used=attempts_used,
            reason=decision.reason,
        )

    def _would_retry(
        self,
        *,
        decision: RetryDecision,
        attempts_used: int,
        emitted_events: bool,
        now: Optional[float],
    ) -> bool:
        if not decision.retryable:
            return False
        if decision.scope is not RetryScope.PROVIDER and decision.scope is not RetryScope.REQUEST:
            # context_overflow (TURN) and tool scopes are not handled by the
            # provider stream layer.
            return False
        if emitted_events:
            return False
        if attempts_used >= self._config.max_attempts:
            return False
        if self._config.max_total_retry_delay < decision.delay_seconds:
            return False
        if (
            self._config.deadline_seconds is not None
            and now is not None
            and now + decision.delay_seconds
            > self._config.deadline_seconds
        ):
            return False
        return True


__all__ = [
    "ProviderRetryConfig",
    "ProviderRetryEvaluator",
    "ProviderRetryObserver",
    "ProviderRetryRecord",
    "RETRY_RUNTIME_SCHEMA_VERSION",
]
