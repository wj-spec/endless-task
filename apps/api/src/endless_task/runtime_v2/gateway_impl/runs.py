"""`RuntimeV2SessionGateway` 的运行族：发送/变体/恢复/审批/取消与运行管道。

从 `gateway.py` 拆出（行为零改动）；只调用底座 `_GatewayBase` 的校验/查找方法。
"""

from __future__ import annotations

import asyncio

import uuid

from endless_task.domain.repositories import ConflictError
from endless_task.runtime.cancellation import CancellationToken

from ..domain import RunRecord, RunStatus
from ..execution import AgentRunExecutor, RunExecutionResult
from .base import _GatewayBase
from .models import _ActiveRun, logger
from .views import _prefix_fingerprint
from .models import (
    PendingApproval,
    RuntimeV2RecoveryResult,
    RuntimeV2RegenerateResult,
    RuntimeV2SendResult,
)


class _GatewayRunsMixin:
    async def send(
        self,
        conversation_id: str,
        content: str,
        *,
        lane_id: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> RuntimeV2SendResult:
        content = content.strip()
        if not content:
            raise ConflictError("Message content cannot be empty")
        request_id = (client_request_id or f"runtime-v2-{uuid.uuid4().hex}").strip()
        if not request_id:
            raise ConflictError("Idempotency key cannot be empty")
        if len(request_id) > 200:
            raise ConflictError("Idempotency key is too long")
        self.validate_session(conversation_id)
        async with self._lock:
            submission = self._repository.create_message_submission(
                conversation_id=conversation_id,
                lane_id=lane_id,
                content=content,
                client_request_id=request_id,
            )
            run = submission.run
            if submission.created:
                run = await self._launch_run(run)
            return RuntimeV2SendResult(
                conversation_id=conversation_id,
                lane_id=submission.lane.id,
                run_id=run.id,
                user_message_id=submission.user_entry.id,
            )

    async def resolve_recovery(
        self,
        run_id: str,
        *,
        retry: bool,
    ) -> RuntimeV2RecoveryResult:
        self._chat_repository.get_conversation(
            self._repository.get_run(run_id).conversation_id
        )
        async with self._lock:
            run = self._repository.get_run(run_id)
            if self._find_active_run(run_id) is not None:
                raise ConflictError("Interrupted run is still active in this process")
            recovery = self._replay_service.classify_crash_recovery(run_id)
            if retry and not recovery.can_auto_resume:
                raise ConflictError("Interrupted run cannot be safely retried")
            self._repository.finalize_interrupted_run(
                run_id,
                deactivate_variant=retry,
            )
            if not retry:
                return RuntimeV2RecoveryResult(
                    run_id=run_id,
                    action="mark_failed",
                    lane_id=run.lane_id,
                )

            new_run = await self._start_run(
                conversation_id=run.conversation_id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )
            return RuntimeV2RecoveryResult(
                run_id=run_id,
                action="retry",
                lane_id=run.lane_id,
                new_run_id=new_run.id,
            )

    def list_run_variants(self, run_id: str) -> tuple[RunRecord, ...]:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        return self._repository.list_run_variants(run_id)

    async def regenerate_run(self, run_id: str) -> RuntimeV2RegenerateResult:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        async with self._lock:
            self._require_no_active_runs(run.conversation_id)
            if run.status not in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                raise ConflictError("Only a terminal run can be regenerated")
            new_run = await self._start_run(
                conversation_id=run.conversation_id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )
            return RuntimeV2RegenerateResult(
                old_run_id=run.id,
                new_run_id=new_run.id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
            )

    async def resend_run(self, run_id: str, content: str) -> RuntimeV2RegenerateResult:
        """编辑消息重跑（P0-2b）：触发条目不变，仅覆盖该轮用户文案，
        在相同 sibling group 下新建 run（与 regenerate 同链语义）。"""
        content = content.strip()
        if not content:
            raise ConflictError("Resend content cannot be empty")
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        async with self._lock:
            self._require_no_active_runs(run.conversation_id)
            if run.status not in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                raise ConflictError("Only a terminal run can be resent")
            new_run = await self._start_run(
                conversation_id=run.conversation_id,
                lane_id=run.lane_id,
                trigger_entry_id=run.trigger_entry_id,
                sibling_group_id=run.sibling_group_id,
                user_content_override=content,
            )
        return RuntimeV2RegenerateResult(
            old_run_id=run.id,
            new_run_id=new_run.id,
            lane_id=run.lane_id,
            trigger_entry_id=run.trigger_entry_id,
            sibling_group_id=run.sibling_group_id,
        )

    async def select_run_variant(self, run_id: str) -> RunRecord:
        run = self._repository.get_run(run_id)
        self.validate_session(run.conversation_id)
        async with self._lock:
            self._require_no_active_runs(run.conversation_id)
            if run.status is not RunStatus.COMPLETED:
                raise ConflictError("Only a completed run variant can be selected")
            if run.assistant_entry_id is None:
                raise ConflictError("Run variant has no assistant entry")
            selected = self._repository.set_active_run_variant(run_id)
            self._repository.append_runtime_event(
                run_id=selected.id,
                event_type="run_variant_selected",
                payload={
                    "assistantEntryId": selected.assistant_entry_id,
                    "siblingGroupId": selected.sibling_group_id,
                },
            )
            return selected

    async def steer(self, run_id: str, content: str) -> bool:
        content = content.strip()
        if not content:
            raise ConflictError("Steering content cannot be empty")
        active = self._find_active_run(run_id)
        if active is None:
            return False
        return await active.executor.enqueue_user_message(content)

    async def cancel(self, run_id: str) -> bool:
        active = self._find_active_run(run_id)
        if active is None:
            return False
        await active.executor.request_cancel()
        return True

    async def resolve_approval(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
        *,
        modified_arguments: Optional[Mapping[str, object]] = None,
    ) -> bool:
        return await self._approval_gate.resolve(
            approval_id,
            decision,
            modified_arguments=modified_arguments,
        )

    def has_active_run(self, conversation_id: str) -> bool:
        active = self._active_runs.get(conversation_id)
        return active is not None and not active.task.done()

    def pending_approvals(self, conversation_id: str) -> tuple[PendingApproval, ...]:
        return tuple(
            approval
            for approval in self._pending_approvals.values()
            if self._repository.get_run(approval.run_id).conversation_id
            == conversation_id
        )

    async def _start_run(
        self,
        *,
        conversation_id: str,
        lane_id: str,
        trigger_entry_id: str,
        sibling_group_id: Optional[str] = None,
        user_content_override: Optional[str] = None,
    ) -> RunRecord:
        run = self._repository.create_run(
            conversation_id=conversation_id,
            lane_id=lane_id,
            trigger_entry_id=trigger_entry_id,
            sibling_group_id=sibling_group_id,
            is_active_variant=True,
            user_content_override=user_content_override,
        )
        return await self._launch_run(run)

    async def _launch_run(self, run: RunRecord) -> RunRecord:
        conversation_id = run.conversation_id
        lane_id = run.lane_id
        trigger_entry = self._repository.get_entry(run.trigger_entry_id)
        trigger_content = trigger_entry.payload.get("content", "")
        product_context_messages = (
            await self._context_prefix_builder(
                conversation_id,
                lane_id,
                run.id,
                trigger_content if isinstance(trigger_content, str) else "",
            )
            if self._context_prefix_builder is not None
            else ()
        )
        token = CancellationToken()
        memory_context_messages = (
            self._memory_service.context_messages(
                conversation_id=conversation_id,
                lane_id=lane_id,
                run_id=run.id,
            )
            if self._memory_service is not None
            else ()
        )
        context_prefix_messages = (
            *self._context_prefix_messages,
            *product_context_messages,
            *memory_context_messages,
        )
        self._metrics.record_prefix_fingerprint(
            conversation_id=conversation_id,
            fingerprint=_prefix_fingerprint(context_prefix_messages),
        )
        selected_provider = self._provider
        selected_model = self._model
        if self._provider_resolver is not None:
            selection = self._provider_resolver(
                self._chat_repository.get_conversation(conversation_id)
            )
            selected_provider = selection.provider
            selected_model = selection.model
        executor = AgentRunExecutor(
            repository=self._repository,
            no_progress_enforcement_enabled=self._no_progress_enforcement_enabled,
            escalation_budget_ratio=self._escalation_budget_ratio,
            verifier_mode=self._verifier_mode,
            verifier_model=self._verifier_model,
            user_profile_provider=self._user_profile_provider,
            cost_cap_usd=self._cost_cap_usd,
            pricing_catalog=self._pricing_catalog,
            provider=selected_provider,
            tool_registry=self._tool_registry,
            model=selected_model,
            max_output_tokens=self._max_output_tokens,
            temperature=self._temperature,
            approval_gate=self._approval_gate,
            provider_slot=self._provider_slot,
            context_prefix_messages=context_prefix_messages,
            tool_filter_provider=self._tool_filter_provider,
            tool_definitions_provider=self._tool_definitions_provider,
            v2_pipeline_enabled=self._v2_pipeline_enabled,
            context_engine_v2_enabled=self._context_engine_v2_enabled,
            context_window_tokens=self._context_window_tokens,
            compaction_hook=self._compaction_hook,
            metrics=self._metrics,
            agent_timeout_seconds=self._agent_timeout_seconds,
            tool_execution_limits=self._tool_execution_limits,
            trace_observer=self._trace_observer,
            span_recorder=self._span_recorder,
            provider_retry_evaluator=self._provider_retry_evaluator,
            provider_retry_observer=(
                self._metrics.provider_retry_observer(run.id)
                if self._provider_retry_evaluator is not None
                else None
            ),
            tool_trust_policy=self._tool_trust_policy,
        )
        task = self._active_run_supervisor.spawn(
            executor.execute(
                run.id,
                cancellation_token=token,
            ),
            name=f"runtime-v2-run:{run.id}",
            on_done=(
                lambda finished, key=conversation_id: self._on_run_done(
                    key,
                    finished,
                )
            ),
        )
        self._active_runs[conversation_id] = _ActiveRun(
            run_id=run.id,
            task=task,
            executor=executor,
            cancellation_token=token,
        )
        return run

    def _on_run_done(self, conversation_id: str, task: asyncio.Task[object]) -> None:
        active = self._active_runs.get(conversation_id)
        if active is not None and active.task is task:
            self._active_runs.pop(conversation_id, None)
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            return
        if (
            isinstance(result, RunExecutionResult)
            and result.status is RunStatus.COMPLETED
            and self._run_completion_callback is not None
        ):
            self._post_run_supervisor.spawn(
                self._post_run_completed(result.run.id),
                name=f"runtime-v2-post-run:{result.run.id}",
            )

    async def _post_run_completed(self, run_id: str) -> None:
        callback = self._run_completion_callback
        if callback is None:
            return
        try:
            await callback(run_id)
        except Exception:
            logger.exception(
                "Runtime v2 post-run processing failed",
                extra={"run_id": run_id},
            )
