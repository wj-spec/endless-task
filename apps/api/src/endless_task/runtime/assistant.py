from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from endless_task.domain.models import TurnSnapshot
from endless_task.domain.repositories import ChatRepository

from .cancellation import CancellationManager, RuntimeCancelled
from .context import ContextBuildError, P0ContextBuilder
from .events import EventPublisher, NullEventPublisher, RuntimeEvent
from .provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderRequest,
    ProviderTextDelta,
)
from .repository import RuntimeRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfiguration:
    model: str
    max_output_tokens: int = 2048
    temperature: Optional[float] = None
    max_concurrent_model_calls: int = 2

    def __post_init__(self) -> None:
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.max_concurrent_model_calls <= 0:
            raise ValueError("max_concurrent_model_calls must be positive")


class AssistantRuntime:
    def __init__(
        self,
        *,
        chat_repository: ChatRepository,
        runtime_repository: RuntimeRepository,
        context_builder: P0ContextBuilder,
        provider: ModelProvider,
        configuration: RuntimeConfiguration,
        cancellation_manager: Optional[CancellationManager] = None,
        event_publisher: Optional[EventPublisher] = None,
    ) -> None:
        self._chat_repository = chat_repository
        self._runtime_repository = runtime_repository
        self._context_builder = context_builder
        self._provider = provider
        self._configuration = configuration
        self._provider_slots = asyncio.Semaphore(
            configuration.max_concurrent_model_calls
        )
        self._cancellation_manager = cancellation_manager or CancellationManager()
        self._event_publisher = event_publisher or NullEventPublisher()

    async def execute(self, *, turn_id: str, variant_id: str) -> TurnSnapshot:
        token = await self._cancellation_manager.acquire(turn_id, variant_id)
        accumulated_content = ""
        started = False
        provider_slot_acquired = False

        try:
            if token.is_cancelled:
                event = self._runtime_repository.cancel_response(
                    turn_id=turn_id,
                    variant_id=variant_id,
                    partial_content="",
                )
                if event is not None:
                    await self._publish(event)
                return self._chat_repository.get_turn(turn_id)

            started_event = self._runtime_repository.start_response(
                turn_id=turn_id,
                variant_id=variant_id,
                provider=self._provider.name,
                model=self._configuration.model,
            )
            started = True
            await self._publish(started_event)

            context = self._context_builder.build(
                turn_id,
                response_variant_id=variant_id,
                reserved_output_tokens=self._configuration.max_output_tokens,
            )
            token.raise_if_cancelled()

            await self._acquire_provider_slot(token)
            provider_slot_acquired = True
            token.raise_if_cancelled()

            message_event = self._runtime_repository.start_message(
                turn_id=turn_id,
                variant_id=variant_id,
            )
            await self._publish(message_event)

            completion: Optional[ProviderCompleted] = None
            request = ProviderRequest(
                request_id=variant_id,
                model=self._configuration.model,
                messages=context.messages,
                max_output_tokens=self._configuration.max_output_tokens,
                temperature=self._configuration.temperature,
            )

            async for provider_event in self._provider.stream(request, token):
                token.raise_if_cancelled()
                if isinstance(provider_event, ProviderTextDelta):
                    if not provider_event.text:
                        continue
                    accumulated_content += provider_event.text
                    event = self._runtime_repository.append_text_delta(
                        turn_id=turn_id,
                        variant_id=variant_id,
                        delta=provider_event.text,
                        accumulated_content=accumulated_content,
                    )
                    await self._publish(event)
                    continue

                if isinstance(provider_event, ProviderCompleted):
                    completion = provider_event
                    break

                raise ProviderError(
                    "unsupported_provider_event",
                    "模型返回了当前版本无法处理的响应。",
                    retryable=False,
                )

            token.raise_if_cancelled()
            if completion is None:
                raise ProviderError(
                    "provider_error",
                    "模型响应意外结束，可以重试。",
                    retryable=True,
                )

            completed_events = self._runtime_repository.complete_response(
                turn_id=turn_id,
                variant_id=variant_id,
                content=accumulated_content,
                finish_reason=completion.finish_reason,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            for event in completed_events:
                await self._publish(event)

        except RuntimeCancelled:
            event = self._runtime_repository.cancel_response(
                turn_id=turn_id,
                variant_id=variant_id,
                partial_content=accumulated_content,
            )
            if event is not None:
                await self._publish(event)
        except ContextBuildError as error:
            if not started:
                raise
            event = self._runtime_repository.fail_response(
                turn_id=turn_id,
                variant_id=variant_id,
                partial_content="",
                error_code=error.code,
                safe_message=error.safe_message,
                retryable=False,
                correlation_id=self._correlation_id(),
            )
            await self._publish(event)
        except ProviderError as error:
            if not started:
                raise
            event = self._runtime_repository.fail_response(
                turn_id=turn_id,
                variant_id=variant_id,
                partial_content=accumulated_content,
                error_code=error.code,
                safe_message=error.safe_message,
                retryable=error.retryable,
                retry_after_ms=error.retry_after_ms,
                correlation_id=self._correlation_id(),
            )
            await self._publish(event)
        except asyncio.CancelledError:
            if started:
                event = self._runtime_repository.cancel_response(
                    turn_id=turn_id,
                    variant_id=variant_id,
                    partial_content=accumulated_content,
                    cancelled_by="system",
                )
                if event is not None:
                    await self._publish(event)
            raise
        except Exception:
            if not started:
                raise
            logger.exception(
                "Assistant runtime failed",
                extra={"turn_id": turn_id, "variant_id": variant_id},
            )
            event = self._runtime_repository.fail_response(
                turn_id=turn_id,
                variant_id=variant_id,
                partial_content=accumulated_content,
                error_code="internal_error",
                safe_message="本地运行时发生错误，可以重试。",
                retryable=True,
                correlation_id=self._correlation_id(),
            )
            await self._publish(event)
        finally:
            if provider_slot_acquired:
                self._provider_slots.release()
            await self._cancellation_manager.release(turn_id, variant_id)

        return self._chat_repository.get_turn(turn_id)

    async def request_cancel(self, *, turn_id: str, variant_id: str) -> bool:
        return await self._cancellation_manager.cancel(turn_id, variant_id)

    async def recover_interrupted(self) -> tuple[RuntimeEvent, ...]:
        events = tuple(self._runtime_repository.recover_interrupted())
        for event in events:
            await self._publish(event)
        return events

    async def _publish(self, event: RuntimeEvent) -> None:
        try:
            await self._event_publisher.publish(event)
        except Exception:
            logger.exception(
                "Runtime event publication failed",
                extra={"event_id": event.event_id, "turn_id": event.turn_id},
            )

    async def _acquire_provider_slot(self, token) -> None:
        acquire_task = asyncio.create_task(self._provider_slots.acquire())
        cancel_task = asyncio.create_task(token.wait())
        try:
            done, _ = await asyncio.wait(
                (acquire_task, cancel_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done:
                if acquire_task in done and acquire_task.result():
                    self._provider_slots.release()
                raise RuntimeCancelled()
            await acquire_task
        finally:
            for task in (acquire_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(acquire_task, cancel_task, return_exceptions=True)

    @staticmethod
    def _correlation_id() -> str:
        return f"corr_{uuid.uuid4().hex}"
