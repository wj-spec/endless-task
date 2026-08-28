from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from endless_task.domain.models import PermissionMode, TurnSnapshot
from endless_task.domain.repositories import ChatRepository
from endless_task.tooling import ApprovalRequest, ApprovalStatus, ToolError, ToolRegistry

from .agent_loop import AgentLoop
from .approval import ApprovalCoordinator, RuntimeToolExecutionObserver
from .cancellation import CancellationManager, RuntimeCancelled
from .context import ContextBuildError, P0ContextBuilder
from .events import EventPublisher, NullEventPublisher, RuntimeEvent
from .provider import ModelProvider, ProviderError
from .repository import RuntimeRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeConfiguration:
    model: str
    max_output_tokens: int = 2048
    temperature: Optional[float] = None
    max_concurrent_model_calls: int = 2
    max_agent_iterations: int = 4
    max_tool_calls_per_turn: int = 8
    agent_timeout_seconds: float = 120.0
    approval_timeout_seconds: float = 1800.0

    def __post_init__(self) -> None:
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.max_concurrent_model_calls <= 0:
            raise ValueError("max_concurrent_model_calls must be positive")
        if self.max_agent_iterations <= 0:
            raise ValueError("max_agent_iterations must be positive")
        if self.max_tool_calls_per_turn <= 0:
            raise ValueError("max_tool_calls_per_turn must be positive")
        if self.agent_timeout_seconds <= 0:
            raise ValueError("agent_timeout_seconds must be positive")
        if self.approval_timeout_seconds <= 0:
            raise ValueError("approval_timeout_seconds must be positive")


class AssistantRuntime:
    def __init__(
        self,
        *,
        chat_repository: ChatRepository,
        runtime_repository: RuntimeRepository,
        context_builder: P0ContextBuilder,
        provider: ModelProvider,
        configuration: RuntimeConfiguration,
        tool_registry: Optional[ToolRegistry] = None,
        cancellation_manager: Optional[CancellationManager] = None,
        event_publisher: Optional[EventPublisher] = None,
        approval_coordinator: Optional[ApprovalCoordinator] = None,
        permission_mode_provider: Optional[Callable[[], PermissionMode]] = None,
        knowledge_query_rewriter=None,
        knowledge_reranker=None,
        knowledge_repository=None,
        workspace_resolver=None,
        provider_resolver=None,
    ) -> None:
        self._chat_repository = chat_repository
        self._runtime_repository = runtime_repository
        self._context_builder = context_builder
        self._provider = provider
        self._configuration = configuration
        self._tool_registry = tool_registry or ToolRegistry()
        self._provider_slots = asyncio.Semaphore(
            configuration.max_concurrent_model_calls
        )
        self._cancellation_manager = cancellation_manager or CancellationManager()
        self._event_publisher = event_publisher or NullEventPublisher()
        self._approval_coordinator = approval_coordinator or ApprovalCoordinator()
        self._permission_mode_provider = permission_mode_provider
        self._knowledge_query_rewriter = knowledge_query_rewriter
        self._knowledge_reranker = knowledge_reranker
        self._knowledge_repository = knowledge_repository
        self._workspace_resolver = workspace_resolver
        self._provider_resolver = provider_resolver

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

            conversation_id = self._chat_repository.get_turn(
                turn_id
            ).turn.conversation_id
            if self._provider_resolver is None:
                selected_provider = self._provider
                selected_model = self._configuration.model
            else:
                selection = self._provider_resolver(
                    self._chat_repository.get_conversation(conversation_id)
                )
                selected_provider = selection.provider
                selected_model = selection.model

            started_event = self._runtime_repository.start_response(
                turn_id=turn_id,
                variant_id=variant_id,
                provider=selected_provider.name,
                model=selected_model,
            )
            started = True
            await self._publish(started_event)

            context = self._context_builder.build(
                turn_id,
                response_variant_id=variant_id,
                reserved_output_tokens=self._configuration.max_output_tokens,
                knowledge_query=await self._rewrite_knowledge_query(turn_id),
                knowledge_ranking=await self._rerank_knowledge(turn_id),
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

            async def append_text(delta: str) -> None:
                nonlocal accumulated_content
                accumulated_content += delta
                event = self._runtime_repository.append_text_delta(
                    turn_id=turn_id,
                    variant_id=variant_id,
                    delta=delta,
                    accumulated_content=accumulated_content,
                )
                await self._publish(event)

            tool_filter = self._tool_filter_for_conversation(conversation_id)
            agent_loop = AgentLoop(
                provider=selected_provider,
                tool_registry=self._tool_registry,
                model=selected_model,
                max_output_tokens=self._configuration.max_output_tokens,
                temperature=self._configuration.temperature,
                max_iterations=self._configuration.max_agent_iterations,
                max_tool_calls=self._configuration.max_tool_calls_per_turn,
                tool_observer=RuntimeToolExecutionObserver(
                    repository=self._runtime_repository,
                    coordinator=self._approval_coordinator,
                    publish=self._publish,
                    approval_timeout_seconds=(
                        self._configuration.approval_timeout_seconds
                    ),
                    permission_mode_provider=self._permission_mode_provider,
                ),
                active_timeout_seconds=self._configuration.agent_timeout_seconds,
                tool_filter=tool_filter,
            )
            result = await agent_loop.run(
                request_id=variant_id,
                conversation_id=self._chat_repository.get_turn(
                    turn_id
                ).turn.conversation_id,
                turn_id=turn_id,
                response_variant_id=variant_id,
                messages=context.messages,
                cancellation_token=token,
                on_text_delta=append_text,
            )

            token.raise_if_cancelled()
            completed_events = self._runtime_repository.complete_response(
                turn_id=turn_id,
                variant_id=variant_id,
                content=result.content,
                finish_reason=result.finish_reason,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
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
        except ToolError as error:
            if not started:
                raise
            event = self._runtime_repository.fail_response(
                turn_id=turn_id,
                variant_id=variant_id,
                partial_content=accumulated_content,
                error_code=error.code,
                safe_message=error.safe_message,
                retryable=error.retryable,
                correlation_id=error.correlation_id or self._correlation_id(),
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

    async def _rewrite_knowledge_query(self, turn_id: str):
        if self._knowledge_query_rewriter is None:
            return None
        try:
            snapshot = self._chat_repository.get_turn(turn_id)
            user_content = snapshot.user_message.content
        except Exception:  # noqa: BLE001 改写是增强项，任何失败都静默回退
            return None
        return await self._knowledge_query_rewriter.rewrite(user_content)

    async def _rerank_knowledge(self, turn_id: str):
        """R5.9：注入前对检索候选做 LLM 重排；失败返回 None（保持原排序）。"""
        if self._knowledge_reranker is None or self._knowledge_repository is None:
            return None
        try:
            snapshot = self._chat_repository.get_turn(turn_id)
            user_content = snapshot.user_message.content
            if len(user_content.strip()) < 4:
                return None
            workspace_id = self._chat_repository.get_conversation(
                snapshot.turn.conversation_id
            ).workspace_id
            from endless_task.domain.models import KnowledgeScope
            from endless_task.storage.sqlite_knowledge_repository import scope_tier

            grouped = self._knowledge_repository.search(
                user_content,
                [
                    KnowledgeScope.SOURCE,
                    KnowledgeScope.MEMORY,
                    KnowledgeScope.ARTIFACT,
                    KnowledgeScope.CONVERSATION,
                ],
                self._knowledge_reranker.candidate_limit,
                workspace_id=workspace_id,
            )
            candidates = [hit for hits in grouped.values() for hit in hits]
            candidates.sort(key=lambda hit: (scope_tier(hit.scope), -hit.score))
        except Exception:  # noqa: BLE001 重排是增强项，任何失败都静默回退
            return None
        return await self._knowledge_reranker.rerank(user_content, candidates)

    async def request_cancel(self, *, turn_id: str, variant_id: str) -> bool:
        return await self._cancellation_manager.cancel(turn_id, variant_id)

    _WORKSPACE_TOOLS = frozenset(
        {
            "read_workspace_file",
            "write_workspace_file",
            "list_workspace_dir",
            "delete_workspace_file",
            "run_shell",
        }
    )

    def _tool_filter_for_conversation(self, conversation_id: str):
        """工作区运行时工具仅在「已绑定本地目录的工作区会话」可见。"""
        if self._workspace_resolver is None:
            return None
        binding = self._workspace_resolver.resolve_binding(conversation_id)
        if binding is None:
            def filter_out(name: str) -> bool:
                return name not in self._WORKSPACE_TOOLS

            return filter_out
        return None

    async def resolve_approval(
        self,
        *,
        approval_id: str,
        status: ApprovalStatus,
    ) -> ApprovalRequest:
        approval, event = self._runtime_repository.resolve_approval(
            approval_id,
            status,
        )
        if event is not None:
            await self._publish(event)
        await self._approval_coordinator.resolve(approval.id, approval.status)
        return approval

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
