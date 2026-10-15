"""`RuntimeV2SessionGateway` 的运行族：发送/变体/恢复/审批/取消与运行管道。

从 `gateway.py` 拆出（行为零改动）；只调用底座 `_GatewayBase` 的校验/查找方法。
"""

from __future__ import annotations

import asyncio

import uuid

from endless_task.domain.repositories import ConflictError, RunNotRevivableError
from endless_task.runtime.cancellation import CancellationToken

from ..domain import RunRecord, RunStatus
from ..execution import AgentRunExecutor, RunExecutionResult
from ..revive import collect_revive_decision
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
        """崩溃/中断 run 的恢复入口（**并**：E3/B 的硬停 run 原地复活）。

        两种形态共用同一个入口，但只有**一个**分派判据——run 行自己的状态：

        * 行**活动**（= 崩溃/重启留下来的中断 run）⇒ 今天的行为，逐字不变：
          `retry=False` 落 ``FAILED``（mark_failed）；`retry=True` 先收尾成 ``FAILED``
          再在**同一 trigger / 同一 sibling group** 下开一条新 run（`action="retry"`）。
        * 行是**终态**（`COMPLETED` / `FAILED` / `CANCELLED`）且 `retry=True` ⇒ 只可能是
          **E3/B：同一条 run 原地复活**（`action="resumed"`，见 `storage/v2/runs.py` 的
          `revive_terminal_run`）；**不可复活**的终态一律**指名拒绝**
          （`RunNotRevivableError`，409 + `code=run_not_revivable`）。
          为什么"接着跑"要落在**这个**入口而不是新开一个网关方法/路由：
          这是"让一条停下来的 run 再跑起来"的既有语义位；新增网关方法会再动一次
          `storage_method_inventory.json`（ADR 只授权了那一条仓储方法），新增路由会动
          `api_routes.json`（本批**两份**面清单都要零改动）。
          ⚠️ 两个如实登记的后果：① `retry` 与 `resume` 在终态上**合流**为同一件事
          —— 既有代码里对终态的 retry **必然失败**（`classify_crash_recovery` 之后
          `finalize_interrupted_run` 直接抛，`InvalidStateError`），所以这里没有改掉任何
          "以前能成"的路径，只是把一条必然失败的路径接成了有意义的动作、并把错误码
          从笼统的 `invalid_state` 换成可判别的 `run_not_revivable`；② 崩溃恢复
          （**活动** run）那条路径一个字没动。
        """
        self._chat_repository.get_conversation(
            self._repository.get_run(run_id).conversation_id
        )
        async with self._lock:
            run = self._repository.get_run(run_id)
            if self._find_active_run(run_id) is not None:
                raise ConflictError("Interrupted run is still active in this process")
            if retry and run.status in (
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            ):
                # E3/B：四条前提逐条**指名**确认（`runtime_v2/revive.py` 是唯一真相源：
                # COMPLETED + 最后一条 awaiting_user 是 willStop=true + 没有"结果无法确认"
                # 的工具体 + 该 lane 没有别的活动 run）。不满足 ⇒
                # `RunNotRevivableError`（HTTP 409，code=`run_not_revivable`），
                # 绝不靠 `idx_v2_runs_one_active_per_lane` 报错兜底。
                decision = collect_revive_decision(
                    repository=self._repository,
                    run_id=run_id,
                )
                if not decision.revivable:
                    raise RunNotRevivableError(decision.refusal_message)
                revived = self._repository.revive_terminal_run(run_id)
                try:
                    launched = await self._launch_run(revived)
                except Exception as error:
                    # ⚠️ 复活已经把行改成了 `running`：启动接线再失败时**绝不能**留下一条
                    # 没有任何任务在跑的 `running`（那正是本批要消灭的"假 running"）。
                    # 按既有的中断收尾路径落终态（行此刻是活动态，`finalize_interrupted_run`
                    # 收得下），让用户看到一条如实的失败，而不是一条永远不动的 running。
                    self._repository.finalize_interrupted_run(
                        run_id,
                        error_code="revive_launch_failed",
                        safe_message="复活失败：这次运行没能启动起来，已如实标记为失败。",
                    )
                    raise ConflictError(
                        "revive_launch_failed：复活失败，这次运行没能启动起来"
                    ) from error
                return RuntimeV2RecoveryResult(
                    run_id=launched.id,
                    action="resumed",
                    lane_id=launched.lane_id,
                )
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
            run_budget=self._run_budget,
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
        active = _ActiveRun(
            run_id=run.id,
            task=task,
            executor=executor,
            cancellation_token=token,
        )
        # 两个索引必须同步登记：会话槽位（`has_active_run` / `_require_no_active_runs`
        # 的语义靠它）+ run id 精确索引（旧 run 被更新的 run 顶掉槽位后仍可取消/纠偏）。
        self._active_runs[conversation_id] = active
        self._active_runs_by_id[run.id] = active
        return run

    def _on_run_done(self, conversation_id: str, task: asyncio.Task[object]) -> None:
        active = self._active_runs.get(conversation_id)
        if active is None or active.task is not task:
            # 会话槽位已被更新的 run 顶掉：按 task 反查这一份并**只**释放它自己
            # （绝不能顺手把更新的 run 的句柄删掉）。
            active = next(
                (
                    candidate
                    for candidate in self._active_runs_by_id.values()
                    if candidate.task is task
                ),
                None,
            )
        if active is not None:
            if self._active_runs.get(conversation_id) is active:
                self._active_runs.pop(conversation_id, None)
            if self._active_runs_by_id.get(active.run_id) is active:
                self._active_runs_by_id.pop(active.run_id, None)
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
