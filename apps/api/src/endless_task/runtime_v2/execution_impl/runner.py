from __future__ import annotations

from endless_task.tool_platform.protocol import ToolOutcome
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from endless_task.tooling import (
    ContextualTool,
    JsonValue,
    ProgressReportingTool,
    RegisteredTool,
    ToolCall,
    ToolCallError,
    ToolCallStatus,
    ToolError,
    ToolExecutionContext,
    ToolProgressEvent,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
)
from endless_task.tooling.schema import ToolSchemaError, validate_tool_arguments
from ._base import _iso_now, logger
from .coordinator import ToolExecutionCoordinator
from .progress import (
    _evaluate_no_progress_shadow,
    _no_progress_evaluation_for_run,
)
from .run_progress import (
    collect_run_progress,
    rebuild_provider_messages,
    run_message_entries,
)
from .turns import ModelTurnRunner
from ..projection import ContextProjection
from ..domain import ModelTurnRecord, ModelTurnStatus, RunRecord, RunStatus, ToolExecutionRecord, ToolExecutionStatus
# 时长/成本维度的升级原因与收尾引导语：到硬上限/成本上限时用后者把"为什么停下"
# 如实写进可见正文（两种上限共用同一条优雅收尾路径）。
from ..escalation import BUDGET_REASON, COST_BUDGET_GUIDANCE, COST_REASON, DEFAULT_BUDGET_RATIO, NO_PROGRESS_REASON, TIME_BUDGET_GUIDANCE, TIME_REASON, UNCERTAIN_SIDE_EFFECTS_GUIDANCE, UNCERTAIN_SIDE_EFFECTS_REASON, VERIFICATION_REASON, EscalationBudget, EscalationProgress, build_escalation_report, budget_exhausted
from ..failure_memory import FailureMemoryAccumulator
from ..run_budget import INTERACTIVE_BUDGET, RunBudget
from ..usage_cost import cost_cap_exceeded, estimate_cost_usd
from ..user_profile import UserProfileBlock
from ..verification import VERIFICATION_PROTOCOL_VERSION, VerificationVerdict, build_verifier_messages, parse_verdict, should_verify
from ..safety import SafetyStopError, SafetyStopReason
from ..metrics import ModelTurnMetric, RunMetric, RuntimeV2MetricsCollector
from ..trace_observer import MIRROR_TIMEOUT_SECONDS, RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from endless_task.runtime_ledger.protocol import (
    SpanKind,
    SpanSpec,
    SpanStatus,
    TraceContext,
)


@dataclass(frozen=True)
class RunExecutionResult:
    status: RunStatus
    run: RunRecord
    content: str
    input_tokens: int
    output_tokens: int
    model_turn_ids: tuple[str, ...]
    pending_approval_execution_ids: tuple[str, ...]
    follow_up_messages: tuple[str, ...] = ()
    assistant_entry_id: Optional[str] = None
    # 本次运行实际派发出去的工具调用数：执行循环本来就在数（升级报告用），
    # 顺手带进终态，上层算子运行用量时无需回查工具执行表。
    tool_call_count: int = 0


class AgentRunExecutor:
    def __init__(
        self,
        *,
        repository: "SqliteRuntimeV2Repository",
        provider: ModelProvider,
        tool_registry: ToolRegistry,
        model: str,
        max_output_tokens: int = 8192,
        temperature: Optional[float] = None,
        approval_gate: Optional[ToolApprovalGate] = None,
        provider_slot: Optional[asyncio.Semaphore] = None,
        compaction_hook: Optional[ContextCompactionHook] = None,
        context_prefix_messages: Sequence[ProviderMessage] = (),
        tool_filter_provider: Optional[
            Callable[[str], Optional[Callable[[str], bool]]]
        ] = None,
        tool_definitions_provider: Optional[
            Callable[[str], tuple[ProviderToolDefinition, ...]]
        ] = None,
        v2_pipeline_enabled: bool = False,
        context_engine_v2_enabled: bool = False,
        tool_execution_limits: Optional[ToolExecutionLimits] = None,
        metrics: Optional["RuntimeV2MetricsCollector"] = None,
        provider_retry_evaluator: Optional[object] = None,
        provider_retry_observer: Optional[object] = None,
        no_progress_observer: Optional[object] = None,
        no_progress_enforcement_enabled: bool = False,
        escalation_budget_ratio: float = DEFAULT_BUDGET_RATIO,
        verifier_mode: str = "0",
        verifier_model: Optional[str] = None,
        user_profile_provider: Optional[Callable[[str], Optional[UserProfileBlock]]] = None,
        cost_cap_usd: float = 0.0,
        pricing_catalog: Optional[PricingCatalog] = None,
        context_shadow_observer: Optional[object] = None,
        context_window_tokens: Optional[int] = None,
        run_budget: Optional[RunBudget] = None,
        trace_observer: Optional[RunTraceObserver] = None,
        span_recorder: Optional[object] = None,
        tool_trust_policy: Optional[ToolTrustPolicy] = None,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._tool_registry = tool_registry
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature
        self._provider_slot = provider_slot
        self._compaction_hook = compaction_hook
        # 时长/成本预算按运行类别由接线层给入；缺省走交互式档，保证"一定有墙"。
        self._run_budget = run_budget or INTERACTIVE_BUDGET
        self._context_prefix_messages = tuple(context_prefix_messages)
        self._metrics = metrics
        self._run_compacted = False
        self._provider_retry_evaluator = provider_retry_evaluator
        self._provider_retry_observer = provider_retry_observer
        self._no_progress_observer = no_progress_observer
        self._no_progress_enforcement_enabled = bool(no_progress_enforcement_enabled)
        # C2 失败记忆：本次运行的失败尝试（外部状态），随工具执行增量累积。
        self._failure_memory = FailureMemoryAccumulator()
        # 已经就"哪一组重复失败"提示过模型 / 发过 run.stuck 事件。
        self._stuck_signature: Optional[tuple[tuple[str, str, str], ...]] = None
        self._stuck_emitted = False
        # 已注入过失败记忆的（工具，错误码）组合，避免每轮重复注入。
        self._injected_failure_keys: set[tuple[str, str]] = set()
        # 已通过 no-progress 评估发出的卡住级别（避免每轮重复发事件）。
        self._no_progress_stuck_level: Optional[str] = None
        # 本次运行已派发的工具调用数（每次 execute() 开头会再重置一次）。这里给
        # 一个初值，是为了让收尾路径在没经过 execute() 时也能读到干净的 0，而不是
        # AttributeError——它只是实例属性，不进被冻结的方法签名。
        self._tool_call_count = 0
        # 本次运行的用量镜像（输入/输出 token、已完成的模型轮数）。与
        # `_tool_call_count` 同一个理由放在实例上：终态收尾路径（取消/异常/兜底）
        # 需要在内核摘牌之后读到"已花掉多少"，而累计量是 execute() 的局部变量，
        # 随帧一起消失。同样只是实例属性——不新增任何方法/成员，冻结签名不动。
        self._input_tokens = 0
        self._output_tokens = 0
        self._model_turns = 0
        # C4 升级：预算阈值与已升级原因（同一原因只提请注意一次）。
        self._escalation_budget_ratio = float(escalation_budget_ratio)
        self._escalated_reasons: set[str] = set()
        # C1 制造者—检查者分离：独立验证模式（0 关 / 1 全部 / side_effects 仅关键运行）。
        self._verifier_mode = verifier_mode
        self._verifier_model = verifier_model
        # B5 用户画像：每次运行开始时读一次已存画像（只读，不重算），
        # 放在静态前缀之后 → 同一版本字节一致，provider 前缀缓存可复用。
        self._user_profile_provider = user_profile_provider
        # C5 成本可见：累计成本估算（按定价表）与可选的成本上限。
        # 成本上限以运行预算为权威来源（按类别 $1/$3/$10）；旧的构造参数只在
        # 它非零时优先——这样既保住既有直接构造执行器的测试（cost_cap_usd=0.1
        # 且不给预算），又让接线层给的按类上限真正生效。
        # **没有显式给预算**时按 R8 语义表保持"派生预算 cap=0.0"（关闭）：兜底用的
        # INTERACTIVE_BUDGET 自带 $1，但直接构造执行器的既有用例（如
        # test_no_cap_means_no_escalation）预期"没给上限 = 不升级"，不能凭空开闸。
        self._cost_cap_usd = float(cost_cap_usd) or (
            float(self._run_budget.cost_cap_usd) if run_budget is not None else 0.0
        )
        self._pricing_catalog = pricing_catalog or make_default_catalog()
        self._context_shadow_observer = context_shadow_observer
        self._context_window_tokens = context_window_tokens
        self._context_engine_v2_enabled = context_engine_v2_enabled
        self._trace_observer = trace_observer
        self._span_recorder = span_recorder
        protocol_receipt_sink = None
        if self._span_recorder is not None and callable(
            getattr(self._span_recorder, "record_effect", None)
        ):
            # RS-6 slice 2b: completed tool side effects land in the
            # runtime ledger as safety-critical records (only when trace
            # wiring is on; default off = current behavior).
            from .effect_sink import build_record_effect_sink

            protocol_receipt_sink = build_record_effect_sink(self._span_recorder)
        self._tool_coordinator = ToolExecutionCoordinator(
            repository=repository,
            tool_registry=tool_registry,
            approval_gate=approval_gate,
            tool_filter_provider=tool_filter_provider,
            tool_definitions_provider=tool_definitions_provider,
            v2_pipeline_enabled=v2_pipeline_enabled,
            limits=tool_execution_limits,
            protocol_receipt_sink=protocol_receipt_sink,
            failure_memory=self._failure_memory,
            trust_policy=tool_trust_policy,
        )
        self._model_turn_runner = ModelTurnRunner(
            repository=repository,
            provider=provider,
            tool_coordinator=self._tool_coordinator,
            model=model,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            provider_slot=provider_slot,
            metrics=metrics,
            provider_retry_evaluator=self._provider_retry_evaluator,
            provider_retry_observer=self._provider_retry_observer,
            span_recorder=self._span_recorder,
        )
        self._context_projection = ContextProjection()
        self._steering_messages: list[str] = []
        self._follow_up_messages: list[str] = []
        self._queue_lock = asyncio.Lock()
        self._active_run_id: Optional[str] = None
        self._cancellation_token: Optional[CancellationToken] = None

    def _open_run_span(self, run: RunRecord, *, monotonic_started: float):
        """Open a run-level INTERNAL span when a recorder is wired."""
        recorder = self._span_recorder
        if recorder is None or not callable(getattr(recorder, "start_span", None)):
            return None
        trace_id = run.correlation_id or run.id
        try:
            return recorder.start_span(
                SpanSpec(
                    trace=TraceContext(
                        trace_id=trace_id,
                        run_id=run.id,
                        correlation_id=trace_id,
                        span_id=run.id,
                    ),
                    kind=SpanKind.INTERNAL,
                    name=f"run:{run.conversation_id}",
                    started_at=run.started_at or _iso_now(),
                    monotonic_started=monotonic_started,
                    attributes={},
                )
            )
        except Exception:
            logger.exception(
                "Failed to open run trace span",
                extra={"run_id": run.id},
            )
            return None

    async def execute(
        self,
        run_id: str,
        *,
        cancellation_token: CancellationToken,
        on_text_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> RunExecutionResult:
        # 切片 10 残留 A1：这一次读取曾在 `try` **之外**，而 run 行是网关先建的
        # （状态 `created`，**也在** `ACTIVE_RUN_STATUSES` 里）——读取抛错时行就停在
        # 活动态、无人收尾，落点会话被 `storage/v2/transcript.py` 的活跃闸永久占住
        # （`POST /messages` 恒 409），直到下次启动清扫才自愈。R10 把主 `try` 上移到
        # **前奏之前**，但没有覆盖到这一行。
        #
        # 这里只补一件事：读取失败时用**入参 `run_id`**（不依赖这次失败的读取）把可能
        # 仍停在活动态的行落成终态，然后**原样重抛**——异常面与今天逐字相同（网关
        # `_on_run_done` / worker `_execute_with_guard` 的调用方本来就会吞/接住它）。
        # 其后到主 `try` 之间的语句全是纯内存赋值（无 I/O、无用户代码），构造不出
        # 异常出口，所以这一层的边界就是"主 `try` 之前唯一会抛的那一步"。
        #
        # 兜底收尾**内联**在这里、不新增方法：`AgentRunExecutor` 的方法面被
        # `tests/fixtures/storage_method_inventory.json` 冻结着，而本任务要求
        # `apps/api/tests/fixtures/` **零改动**。
        try:
            run = self._repository.get_run(run_id)
        except Exception:
            # 只按**入参 `run_id`** 落终态（此时 `run` 尚未绑定，绝不依赖那次失败的
            # 读取）：
            #   * 行已是终态 → `transition_run_status` 抛 `InvalidStateError`
            #     （已是目标态时早返回），一律吞掉：**不改写**调用方原本要抛的异常；
            #   * 行不存在 → `NotFoundError`，同样吞掉（没有活动行可留）；
            #   * 收尾自身失败 → 吞掉；随后原样 `raise`，异常面与今天逐字相同。
            # 绝不伪造 `started_at`：目标态 `FAILED` 不在 `ACTIVE_RUN_STATUSES` 里，
            # `transition_run_status` 对**从未开始**的行保持 `started_at = NULL`
            # （沿用 T2/R10 口径）。告警文案与既有 `except Exception` 收尾同源。
            try:
                self._repository.transition_run_status(
                    run_id,
                    RunStatus.FAILED,
                    event_type="run_failed",
                    payload={
                        "errorCode": "runtime_internal_error",
                        "safeMessage": "执行过程出现内部错误。",
                    },
                    error_code="runtime_internal_error",
                    safe_message="执行过程出现内部错误。",
                )
            except Exception:
                logger.exception(
                    "Failed to finalize run after run read failure",
                    extra={"run_id": run_id},
                )
            raise
        # 逐运行状态必须**每次运行开头**重置：任务/提醒路径上的执行器是进程级单例
        # （wiring 只构建一次，任务 worker 每个定时/提醒运行都复用同一个实例），
        # 上一轮累积的状态若不清空就会静默跨运行生效——最典型的是"同一原因只提请注意
        # 一次"的去重集合：第二次长任务运行将再也发不出软时长提醒。只重置"属于本次
        # 运行"的状态；模型、预算、超时、注册表、观察器、审批闸门等配置属性一律不动。
        self._active_run_id = run.id
        self._cancellation_token = cancellation_token
        self._run_compacted = False
        self._escalated_reasons = set()
        # 失败记忆累加器由工具协调器持有**同一个引用**（失败记录只由协调器 record
        # 进去），所以这里换新对象时必须同步换掉协调器手里的引用，否则新的失败会被
        # 记进上一轮那个旧累加器，本次运行永远看不到自己的失败。
        self._failure_memory = FailureMemoryAccumulator()
        self._tool_coordinator._failure_memory = self._failure_memory
        self._stuck_signature = None
        self._stuck_emitted = False
        self._injected_failure_keys = set()
        self._no_progress_stuck_level = None
        # 本次运行已派发的工具调用数：终态把它带进 RunExecutionResult，供上层
        # （子运行用量）取用。放在实例上而不是穿参数，是为了不动 _result /
        # _finalize_time_budget 的签名——它们的签名被
        # tests/fixtures/storage_method_inventory.json 冻结着。
        self._tool_call_count = 0
        # 用量镜像同样逐运行清零：执行器在任务/提醒路径上是进程级单例，上一轮
        # 的累计量若不清空，收尾路径会把上一次运行的 token 记到这一次头上。
        self._input_tokens = 0
        self._output_tokens = 0
        self._model_turns = 0
        # 待投递的引导/追问消息属于某一轮运行：运行开始时空队列就是它的初值。
        self._steering_messages = []
        self._follow_up_messages = []
        # 切片 10 R10：run 行是**网关先建**的（状态 `created`），而 `created` 也在
        # `ACTIVE_RUN_STATUSES` 里——所以"行已存在之后的任何早退"都会把落点会话
        # 永久占住（`POST /messages` 恒 409）。终态收尾（`except` 与 `finally`）
        # 会读下面这些累计量，因此它们必须在 `try` **之外**先拿到合法初值，
        # `try` 才能从**前奏之前**开始。正常路径逐字不变：这些初值与今天在
        # `try` 内的赋值逐字相同，只是位置提前。
        content_parts: list[str] = []
        model_turn_ids: list[str] = []
        input_tokens = 0
        output_tokens = 0
        pending_approval_ids: tuple[str, ...] = ()
        # C4 升级报告所需的运行进展计数（局部累计，无需回查数据库）。
        tool_calls_total = 0
        produced_characters = 0
        # C1 验证所需：本次运行实际执行过的"有副作用"工具（关键运行的判据）。
        side_effect_evidence: list[str] = []
        run_span = None
        run_started = time.monotonic()

        # 切片 10 终审修复波 W-A / R10：`try` 的起点必须在"run 行已存在之后的
        # **每一步**"之前，否则早退会留下活动态行、把落点会话占死。
        #
        # W-A（旧 :380）：`cancellation_token.raise_if_cancelled()` 曾放在 `try`
        # **之外**，而 `RuntimeCancelled` 的唯一终态收尾在 `try` 的
        # `except RuntimeCancelled` 里。取消一个**排队中**的 run（还堵在
        # `max_concurrent_task_runs` 信号量后面，拿到名额后才建 v2 行）会让 v2 行
        # 永久停在 `running`。
        # R10：**前奏**（`list_entry_context_entries` → `build_plan_shadow` →
        # `context_projection.project`）同样在 `try` 之外，它抛错时行停在
        # `created`（**也在**活跃集合里），交互路径无人收尾（网关 `_on_run_done`
        # 吞异常、任务路径只收 `task_run`），落点会话被
        # `storage/v2/transcript.py` 的活跃闸永久占住（`POST /messages` 恒 409），
        # 直到下次启动清扫才自愈。
        # 修法：把 `try` 起点上移到**前奏之前**。前奏、`start_run` 自身失败（行早已
        # 终态 / 库错）、两条 `append_runtime_event`、这一次取消检查、
        # `_open_run_span` 抛错，全部走既有终态收尾路径（CANCELLED / FAILED /
        # SafetyStopError），**不新增**任何终止语义、不改任何默认值。
        try:
            entries = self._repository.list_entry_context_entries(run.trigger_entry_id)
            if run.trigger_content_override is not None:
                # P0-2b：编辑重跑只覆盖本轮的触发用户文案，不新增/改写 transcript 条目
                # （事实源保持 append-only，链语义与 regenerate 一致）。
                from dataclasses import replace as replace_entry

                entries = tuple(
                    replace_entry(
                        entry,
                        payload={**entry.payload, "content": run.trigger_content_override},
                    )
                    if entry.id == run.trigger_entry_id
                    else entry
                    for entry in entries
                )
            from endless_task.context_engine import ContextBudget

            from .context_segments import build_plan_shadow

            budget = None
            if (
                self._context_window_tokens is not None
                and self._context_window_tokens > self._max_output_tokens
            ):
                budget = ContextBudget(
                    window_tokens=self._context_window_tokens,
                    reserved_output_tokens=self._max_output_tokens,
                    safety_margin_tokens=0,
                )
            plan_shadow = build_plan_shadow(entries, budget=budget)
            projection_entries = entries
            if self._context_engine_v2_enabled and budget is not None:
                excluded = set(plan_shadow.excluded_source_ids)
                if excluded:
                    projection_entries = tuple(
                        entry for entry in entries if entry.id not in excluded
                    )
            projection = self._context_projection.project(projection_entries)
            if self._context_shadow_observer is not None:
                self._context_shadow_observer(plan_shadow)
            provider_messages: list[ProviderMessage] = list(
                self._context_prefix_messages
            )
            profile = self._load_user_profile(run.conversation_id)
            if profile is not None and not profile.empty:
                provider_messages.append(
                    ProviderMessage(role="system", content=profile.content)
                )
            provider_messages.extend(projection.messages)
            # 批次 E / E1：**进度感知**（续跑的前提，先于 `start_run`）。
            #
            # 今天对同一个 run 再 `execute` 一次就是**从头重做**：上下文只来自触发器的
            # **祖先链**（`list_entry_context_entries` 是 `WITH RECURSIVE` 往**上**走），
            # 而运行期自己写下的 TOOL_CALL / TOOL_RESULT / 压缩摘要全是它的**后代**，
            # 一律不在内；`v2_model_turns` / `v2_tool_executions` 在 `execution_impl/`
            # 里又**从不用于跳过**（只用于收尾报表）⇒ 循环不知道"哪些轮已经做完了"。
            #
            # 这里在进入循环之前读一次进度，把**已完成的轮**接上：消息序列按
            # `ContextProjection` 的既有语义重建（含 `context_summary.coveredEntryIds`
            # 的跳过、TOOL_CALL ↔ TOOL_RESULT 配对），局部计数器按已完成的轮续上。
            # 两条都不动任何冻结签名：进度读取器是模块级纯函数（`run_progress.py`），
            # 而 `AgentRunExecutor` 的方法面被 `storage_method_inventory.json` 冻结着。
            #
            # 没有已完成轮（=今天绝大多数路径）时**什么都不做**：行为逐字不变。
            run_progress = collect_run_progress(
                repository=self._repository, run_id=run.id
            )
            if run_progress.resumable:
                resumed_entries = run_message_entries(
                    self._repository.list_entries(
                        run.lane_id,
                        include_variants=True,
                    ),
                    run_id=run.id,
                )
                head_messages = provider_messages[
                    : len(provider_messages) - len(projection.messages)
                ]
                provider_messages = list(
                    rebuild_provider_messages(
                        entries=(*projection_entries, *resumed_entries),
                        progress=run_progress,
                        prefix_messages=head_messages,
                        projection=self._context_projection,
                    )
                )
                model_turn_ids.extend(run_progress.completed_turn_ids)
                content_parts.extend(
                    turn.assistant_text
                    for turn in run_progress.completed_turns
                    if turn.assistant_text
                )
                input_tokens += run_progress.input_tokens
                output_tokens += run_progress.output_tokens
                tool_calls_total += run_progress.tool_call_count
                produced_characters += run_progress.produced_characters
                # 前任已**确认**的副作用仍算这次运行的副作用：验证闸门（`_maybe_verify`）
                # 看的正是这份证据，续跑把它清零等于假装没动过手。
                side_effect_evidence.extend(
                    self._side_effect_evidence(
                        tuple(
                            ProviderToolCall(
                                id=tool.call_id,
                                name=tool.tool_name,
                                arguments={},
                            )
                            for tool in run_progress.confirmed_tools
                        )
                    )
                )
                # 与循环内"每完成一轮就镜像用量"保持同一口径：镜像必须先接上前任。
                self._tool_call_count = tool_calls_total
                self._input_tokens = input_tokens
                self._output_tokens = output_tokens
                self._model_turns = len(model_turn_ids)
            # 批次 E / E2：**不确定副作用**的用户决定通道。
            #
            # 事实（ADR §3.2 / §4.0，本批复核过常量）：`coordinator` 在**批量执行之前**
            # 就把整批 tool 一律置 `RUNNING`（`tool_executions.py` 的活动集合 =
            # {created, validating, waiting_approval, running}），而崩溃收尾
            # `finalize_interrupted_run` 又把这**四个**状态压成同一个 `FAILED` +
            # `retryable=0` + 同一句话 ⇒ 行层面"**未调用**"与"**已调用但结果未知**"
            # 此后不可区分。既有 `run_auto_restored` 只还原**工作区文件**，对非文件
            # 副作用（发了一条飞书消息、调了一次外部写接口）什么也撤不回——这正是
            # "不能靠既有机制糊过去"的原因。
            #
            # 裁定（ADR 落死）：默认按"可能已产生副作用"处理——**不重放**这些工具，
            # 改为要求用户决定，并在既有 `run_awaiting_user` / 升级面上**如实**呈现。
            # 不接受"默认重放"，也不接受"默认跳过且不告知"。
            #
            # 收尾形态对齐 `_finalize_time_budget`（will_stop=True + 引导语进正文 +
            # COMPLETED），收尾代码**内联**在此、不新增方法：`AgentRunExecutor` 的方法面
            # 被 `tests/fixtures/storage_method_inventory.json` 冻结着。
            if run_progress.uncertain_tools:
                uncertain_report = build_escalation_report(
                    reason=UNCERTAIN_SIDE_EFFECTS_REASON,
                    progress=EscalationProgress(
                        model_turns=len(model_turn_ids),
                        tool_calls=tool_calls_total,
                        produced_characters=produced_characters,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    ),
                    budget=EscalationBudget(
                        used_tokens=input_tokens + output_tokens,
                        limit_tokens=self._escalation_limit_tokens(),
                    ),
                    will_stop=True,
                    uncertain_tools=tuple(
                        {
                            "toolName": tool.tool_name,
                            "callId": tool.call_id,
                            "status": tool.status.value,
                        }
                        for tool in run_progress.uncertain_tools
                    ),
                )
                self._repository.append_runtime_event(
                    run_id=run.id,
                    event_type="run_awaiting_user",
                    payload=uncertain_report.as_json(),
                )
                if content_parts:
                    content_parts.append("\n\n" + UNCERTAIN_SIDE_EFFECTS_GUIDANCE)
                else:
                    content_parts.append(UNCERTAIN_SIDE_EFFECTS_GUIDANCE)
                uncertain_content = "".join(content_parts)
                # 为什么这里**不**调 `_maybe_verify`（M-1：本路径是唯一一条不验证就收尾的
                # 终态路径；对照 `:757/790/849` 与 `_finalize_time_budget` 里的 `:1369`）：
                #
                # 1. E2 已经在升级面（`run_awaiting_user` + `uncertain_side_effects`）上
                #    把"要用户决定"这件事说清了，`will_stop=True`。再走一次验证器只会在
                #    同一停点上叠加第二个 `run_awaiting_user`（`verification_failed`），
                #    让用户看到两份互相竞争的决策请求（**双升级**）。
                # 2. 验证器判的是"制造者产出是否满足目标"（`build_verifier_messages` 只看
                #    目标/产出/证据），它**回答不了**"那次工具调用到底发出去没有"——而这
                #    正是本次停机的唯一理由（结果无法确认）。花钱买一个答非所问的结论不
                #    是稳健性。
                # 3. 因此这条路径故意"跳过验证、直接定案"：可见性由升级报告承载
                #    （`tests/test_runtime_v2_run_resume.py` 里有断言钉住"没有验证器调用"）。
                #
                # 收尾形态与 I-2 的口径（b）：在飞的那个 model turn（`PROJECTING_CONTEXT`）
                # 与"结果无法确认"的 tool 行（`RUNNING`）**故意留在活跃态**，不在这里压平：
                #
                # * 它们是"可能已产生副作用"这份信息的**唯一载体**（行级别一旦被
                #   `finalize_interrupted_run` 那样压成 `FAILED` + `retryable=0` + 同一句话，
                #   "未调用"与"已调用但结果未知"就不可区分；`is_uncertain_tool_execution`
                #   两种形态都认，正是为了这个）。
                # * 保留是**安全的**：`run_sweep` 只挑活动 run，而 `finalize_interrupted_run`
                #   对终态 run 直接 `InvalidStateError`（`storage/v2/runs.py:249-252`）⇒
                #   没有任何路径会去"顺手收尾"它们，状态是**定格**的，不是悬空的。
                # * "终态 run 不可续"这条判据不在行上、而在读取器上：`RunProgress.resumable`
                #   对终态 run 一律返回 False（`execution_impl/run_progress.py`），所以这
                #   两行活跃子行不会被误读成"还有活可干"。
                # * E3 拿到的是"结果未定案 + 不确定工具清单"，可以据此决定怎么续，
                #   不必再去猜行状态。
                final_run, assistant_entry = self._repository.finalize_run(
                    run.id,
                    content=uncertain_content,
                    finish_reason="uncertain_side_effects_stop",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
                return self._result(
                    RunStatus.COMPLETED,
                    final_run,
                    uncertain_content,
                    input_tokens,
                    output_tokens,
                    tuple(model_turn_ids),
                    (),
                    assistant_entry_id=assistant_entry.id,
                )
            run = self._repository.start_run(run.id)
            # 切片 10 T2 Step 1：把**运行预算墙**落痕，让快照能如实给出
            # 「上限/已用/还剩多少」。为什么必须由执行器发：这三条墙只有持有
            # `RunBudget` 的执行器知道——交互 run 由网关按交互档构造执行器，任务/
            # 提醒 run 走的是另一个执行器（`resolve_run_budget("task")`），子运行又
            # 是 `child_run_budget(...)`；网关按 run 行**猜不出**档位，猜就是伪造。
            # 只发一条（墙在整次运行里恒定），不按轮刷屏；elapsed/当前步骤由 run 行
            # 与既有计数器在快照侧投影成活值，不需要事件。
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="run_budget_declared",
                payload={
                    "softSeconds": self._run_budget.soft_seconds,
                    "hardSeconds": self._run_budget.hard_seconds,
                    "perTurnSeconds": self._run_budget.per_turn_seconds,
                    "costCapUsd": self._cost_cap_usd,
                    "stopOnCost": self._run_budget.stop_on_cost,
                },
            )
            if profile is not None and not profile.empty:
                # 缓存/成本可观测：把版本与签名记进事件，便于观察前缀变动频率。
                self._repository.append_runtime_event(
                    run_id=run.id,
                    event_type="user_profile_injected",
                    payload={
                        "version": profile.version,
                        "signature": profile.signature,
                        "characters": profile.characters,
                        "lines": len(profile.lines),
                        "manual": profile.manual,
                    },
                )
            cancellation_token.raise_if_cancelled()
            run_span = self._open_run_span(run, monotonic_started=run_started)
            while True:
                elapsed = time.monotonic() - run_started
                remaining_timeout: Optional[float] = self._run_budget.remaining_hard(
                    elapsed
                )
                reaches_time_wall = (
                    remaining_timeout is not None and remaining_timeout <= 0
                )
                progress_now = EscalationProgress(
                    model_turns=len(model_turn_ids),
                    tool_calls=tool_calls_total,
                    tool_failures=self._failure_memory.snapshot.total,
                    produced_characters=produced_characters,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    elapsed_seconds=elapsed,
                )
                # M3-3：成本到顶也是"墙"。子运行档（`stop_on_cost=True`）到这里就
                # **真停**，父档的这条判断被策略位短路，行为逐字节不变。
                reaches_cost_cap = (
                    not reaches_time_wall
                    and self._run_budget.stop_on_cost
                    and cost_cap_exceeded(
                        cost_usd=self._cumulative_cost(progress_now),
                        cap_usd=self._cost_cap_usd,
                    )
                )
                if reaches_time_wall or reaches_cost_cap:
                    # 到上限不是故障：落阶段性结论 + 引导语，运行记 COMPLETED。
                    # 两条上限共用**这一条**收尾路径（同一套 will_stop/引导语/
                    # COMPLETED 语义），不另造终止机制。
                    return await self._finalize_time_budget(
                        run,
                        content_parts=content_parts,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        model_turn_ids=model_turn_ids,
                        side_effects=side_effect_evidence,
                        progress=progress_now,
                        cancellation_token=cancellation_token,
                    )

                async def run_next_model_turn() -> ModelTurnOutcome:
                    provider_messages.extend(await self._drain_steering_messages(run))
                    await self._maybe_compact_context(run, provider_messages)
                    return await self._model_turn_runner.run(
                        run=run,
                        messages=provider_messages,
                        cancellation_token=cancellation_token,
                        on_text_delta=on_text_delta,
                    )

                # 单轮上限：min(剩余硬预算, per_turn_seconds)。墙体变大（15-60 分钟）后，
                # 一次挂死的模型调用会吃掉整段预算，所以每轮必须另有活性上限。
                turn_timeout = self._run_budget.turn_timeout(elapsed)
                try:
                    async with asyncio.timeout(turn_timeout):
                        outcome = await run_next_model_turn()
                except TimeoutError:
                    elapsed_after = time.monotonic() - run_started
                    hard_seconds = self._run_budget.hard_seconds
                    # 判据是"硬墙是否真的过了"，不是异常类型，也不是两个超时谁更小
                    # （墙恰好是约束方时，一个提前抛出的内建 TimeoutError 也会满足
                    # remaining_hard <= turn_timeout）。只有墙体确实已经过去才走优雅
                    # 收尾；否则这个 TimeoutError 来自单轮卡死或更内层的组件
                    # （工具执行就在 run_next_model_turn 内部），必须保留故障信号。
                    # 我们自己那个 asyncio.timeout(remaining) 到期后才会进到这里，
                    # 此时 monotonic 必然已经越过 elapsed_before + remaining = 墙。
                    if hard_seconds is not None and elapsed_after >= hard_seconds:
                        self._append_current_model_turn_ids(run.id, model_turn_ids)
                        return await self._finalize_time_budget(
                            run,
                            content_parts=content_parts,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            model_turn_ids=model_turn_ids,
                            side_effects=side_effect_evidence,
                            progress=EscalationProgress(
                                model_turns=len(model_turn_ids),
                                tool_calls=tool_calls_total,
                                tool_failures=self._failure_memory.snapshot.total,
                                produced_characters=produced_characters,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                elapsed_seconds=elapsed_after,
                            ),
                            cancellation_token=cancellation_token,
                        )
                    raise SafetyStopError(
                        SafetyStopReason.AGENT_TIMEOUT,
                        "单次模型调用超时，已安全停止。",
                    )
                model_turn_ids.append(outcome.model_turn_id)
                content_parts.append(outcome.content)
                input_tokens += outcome.input_tokens or 0
                output_tokens += outcome.output_tokens or 0
                tool_calls_total += len(outcome.tool_calls)
                self._tool_call_count = tool_calls_total
                # 与工具计数同一处镜像用量：每个**已完成**的模型轮更新一次，
                # 上限是"最后一个完成的轮"，在飞的那一轮不计（它从未完成）。
                self._input_tokens = input_tokens
                self._output_tokens = output_tokens
                self._model_turns = len(model_turn_ids)
                produced_characters += len(outcome.content)
                side_effect_evidence.extend(
                    self._side_effect_evidence(outcome.tool_calls)
                )
                pending_approval_ids = outcome.pending_approval_execution_ids
                if pending_approval_ids:
                    return self._result(
                        RunStatus.WAITING_APPROVAL,
                        run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        pending_approval_ids,
                    )

                provider_messages.extend(outcome.messages)
                self._observe_failure_memory(run)
                # C4：无进展（连续同因失败）或预算将尽 → 提请人工决策。
                self._maybe_escalate(
                    run,
                    progress=EscalationProgress(
                        model_turns=len(model_turn_ids),
                        tool_calls=tool_calls_total,
                        tool_failures=self._failure_memory.snapshot.total,
                        produced_characters=produced_characters,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        elapsed_seconds=elapsed,
                    ),
                    used_tokens=(outcome.input_tokens or 0)
                    + (outcome.output_tokens or 0),
                )
                delivered = not outcome.tool_calls and bool(outcome.content.strip())
                # 工具级「提前终止」：本批所有工具结果都请求 terminate → 停循环。
                if outcome.tool_calls and outcome.terminate:
                    await self._maybe_verify(
                        run,
                        candidate="".join(content_parts),
                        side_effects=side_effect_evidence,
                        progress=EscalationProgress(
                            model_turns=len(model_turn_ids),
                            tool_calls=tool_calls_total,
                            tool_failures=self._failure_memory.snapshot.total,
                            produced_characters=produced_characters,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            elapsed_seconds=elapsed,
                        ),
                        cancellation_token=cancellation_token,
                    )
                    final_run, assistant_entry = self._repository.finalize_run(
                        run.id,
                        content="".join(content_parts),
                        finish_reason=outcome.finish_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return self._result(
                        RunStatus.COMPLETED,
                        final_run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        (),
                        assistant_entry_id=assistant_entry.id,
                    )
                if delivered:
                    await self._maybe_verify(
                        run,
                        candidate="".join(content_parts),
                        side_effects=side_effect_evidence,
                        progress=EscalationProgress(
                            model_turns=len(model_turn_ids),
                            tool_calls=tool_calls_total,
                            tool_failures=self._failure_memory.snapshot.total,
                            produced_characters=produced_characters,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            elapsed_seconds=elapsed,
                        ),
                        cancellation_token=cancellation_token,
                    )
                    final_run, assistant_entry = self._repository.finalize_run(
                        run.id,
                        content="".join(content_parts),
                        finish_reason=outcome.finish_reason,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
                    return self._result(
                        RunStatus.COMPLETED,
                        final_run,
                        "".join(content_parts),
                        input_tokens,
                        output_tokens,
                        tuple(model_turn_ids),
                        (),
                        assistant_entry_id=assistant_entry.id,
                    )
                if self._no_progress_enforcement_enabled:
                    from endless_task.reliability.stop import StopLevel
                    from endless_task.reliability.stop_runtime import no_progress_guidance

                    evaluation = _no_progress_evaluation_for_run(
                        repository=self._repository,
                        run_id=run.id,
                    )
                    self._observe_no_progress_stuck(run, evaluation)
                    if evaluation is not None and evaluation.level is StopLevel.STOP:
                        self._maybe_escalate(
                            run,
                            progress=EscalationProgress(
                                model_turns=len(model_turn_ids),
                                tool_calls=tool_calls_total,
                                tool_failures=self._failure_memory.snapshot.total,
                                produced_characters=produced_characters,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                elapsed_seconds=elapsed,
                            ),
                            used_tokens=input_tokens + output_tokens,
                            reason=NO_PROGRESS_REASON,
                            will_stop=True,
                        )
                        guidance = no_progress_guidance(evaluation.level, evaluation.reasons)
                        content_parts.append("\n\n" + guidance)
                        await self._maybe_verify(
                            run,
                            candidate="".join(content_parts),
                            side_effects=side_effect_evidence,
                            progress=EscalationProgress(
                                model_turns=len(model_turn_ids),
                                tool_calls=tool_calls_total,
                                tool_failures=self._failure_memory.snapshot.total,
                                produced_characters=produced_characters,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                elapsed_seconds=elapsed,
                            ),
                            cancellation_token=cancellation_token,
                        )
                        final_run, assistant_entry = self._repository.finalize_run(
                            run.id,
                            content="".join(content_parts),
                            finish_reason="no_progress_stop",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                        return self._result(
                            RunStatus.COMPLETED,
                            final_run,
                            "".join(content_parts),
                            input_tokens,
                            output_tokens,
                            tuple(model_turn_ids),
                            (),
                            assistant_entry_id=assistant_entry.id,
                        )
                    if evaluation is not None and (
                        evaluation.level is StopLevel.REMIND
                        or evaluation.level is StopLevel.RESTRICT
                    ):
                        self._steering_messages.append(
                            no_progress_guidance(evaluation.level, evaluation.reasons)
                        )
        except RuntimeCancelled:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.CANCELLED,
                event_type="run_cancelled",
                cancelled_by="user",
            )
            return self._result(
                RunStatus.CANCELLED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except asyncio.CancelledError:
            # 宿主取消：外层 asyncio.timeout / 任务取消，不是运行自己的取消令牌
            # （后者走上面的 RuntimeCancelled）。CancelledError 是 BaseException 子类，
            # 既不会被 RuntimeCancelled 处理器命中，也不会被 except Exception 兜住；
            # 不在这里收尾，运行行就会停在活动态，冻结会话并需要人工恢复。
            # 只补落终态，然后原样重抛——协作式取消必须继续向上传播（绝不当成正常返回）。
            self._finalize_host_cancellation(run.id)
            raise
        except SafetyStopError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="safety_stop",
                payload={
                    "reason": error.reason.value,
                    "errorCode": error.code,
                    "safeMessage": error.safe_message,
                },
            )
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except ProviderError as error:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={"errorCode": error.code, "safeMessage": error.safe_message},
                error_code=error.code,
                safe_message=error.safe_message,
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        except Exception:
            self._append_current_model_turn_ids(run.id, model_turn_ids)
            logger.exception("Runtime v2 run failed unexpectedly", extra={"run_id": run.id})
            final_run = self._repository.transition_run_status(
                run.id,
                RunStatus.FAILED,
                event_type="run_failed",
                payload={
                    "errorCode": "runtime_internal_error",
                    "safeMessage": "执行过程出现内部错误。",
                },
                error_code="runtime_internal_error",
                safe_message="执行过程出现内部错误。",
            )
            return self._result(
                RunStatus.FAILED,
                final_run,
                "".join(content_parts),
                input_tokens,
                output_tokens,
                tuple(model_turn_ids),
                pending_approval_ids,
            )
        finally:
            self._active_run_id = None
            self._cancellation_token = None
            if self._metrics is not None:
                final_run = self._repository.get_run(run.id)
                self._metrics.record_run(
                    RunMetric(
                        run_id=run.id,
                        conversation_id=run.conversation_id,
                        status=final_run.status.value,
                        duration_ms=int((time.monotonic() - run_started) * 1000),
                        model_turn_count=len(model_turn_ids),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        compacted=self._run_compacted,
                        compaction_released_tokens=0,
                    )
                )
            if run_span is not None:
                try:
                    final_status_run = self._repository.get_run(run.id)
                    await run_span.end(
                        SpanStatus(final_status_run.status.value)
                        if final_status_run.status.value in {
                            "completed",
                            "failed",
                            "cancelled",
                        }
                        else SpanStatus.FAILED,
                        ended_at=final_status_run.finished_at or _iso_now(),
                        monotonic_ended=time.monotonic(),
                    )
                except Exception:
                    logger.exception(
                        "Failed to close run trace span",
                        extra={"run_id": run.id},
                    )
            if self._trace_observer is not None:
                # Observability projection (M6 W6-1): mirror terminal usage
                # into the runtime ledger under a hard timeout. The observer
                # is fail-open (errors swallowed, non-terminal/idempotent
                # runs skipped), so this can never break or hang a run.
                try:
                    await asyncio.wait_for(
                        self._trace_observer.on_run_terminal(run.id),
                        timeout=MIRROR_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.exception(
                        "Terminal trace observer failed for run",
                        extra={"run_id": run.id},
                    )

    def _finalize_host_cancellation(self, run_id: str) -> None:
        """宿主取消 → 把运行落成 ``CANCELLED``（幂等、失败兜底）。

        与取消令牌路径（``RuntimeCancelled``）不同，这条路径上没人会替我们迁移
        运行状态，所以只能由执行器自己收尾。它只做一件事——"活动态就落终态"：

        * 运行已是终态 → 什么都不做（不重复迁移，也不抛异常）；
        * 收尾自身失败 → 吞掉；调用方随后 ``raise``，取消照常向上传播。

        返回值一律为 None；是否重抛由调用方决定（这里绝不把取消变成正常返回）。
        """
        try:
            # 延迟导入：runtime_v2 顶层导入 storage 会与 storage → runtime_v2.domain
            # 的导入链成环（storage-first 导入顺序下直接 ImportError）。这里的导入
            # 发生在运行期，模块早已加载完毕，且只做一次 sys.modules 命中。
            from endless_task.storage.v2.base import ACTIVE_RUN_STATUSES

            run = self._repository.get_run(run_id)
            if run.status not in ACTIVE_RUN_STATUSES:
                return
            self._repository.transition_run_status(
                run_id,
                RunStatus.CANCELLED,
                event_type="run_cancelled",
                cancelled_by="host",
            )
        except Exception:
            logger.exception(
                "Failed to finalize run after host cancellation",
                extra={"run_id": run_id},
            )

    async def enqueue_user_message(self, content: str) -> bool:
        async with self._queue_lock:
            if self._active_run_id is None:
                self._follow_up_messages.append(content)
                return False
            self._steering_messages.append(content)
            return True

    async def drain_follow_up_messages(self) -> tuple[str, ...]:
        async with self._queue_lock:
            messages = tuple(self._follow_up_messages)
            self._follow_up_messages.clear()
            return messages

    async def request_cancel(self, *, cancelled_by: str = "user") -> None:
        if self._cancellation_token is not None:
            self._cancellation_token.cancel()
        if self._active_run_id is None:
            return
        run = self._repository.get_run(self._active_run_id)
        if run.status in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            return
        self._repository.transition_run_status(
            run.id,
            RunStatus.CANCELLING,
            event_type="run_cancel_requested",
            cancelled_by=cancelled_by,
        )

    def _observe_failure_memory(self, run: RunRecord) -> None:
        """C2：把"连续同因失败"变成外部状态，并作为下一轮决策的输入。

        * 一组新的重复失败出现 → 发 ``run_stuck`` 事件（产品层可见/可审计），
          并把精简的失败记忆注入下一轮：模型不该原样重复已失败的调用；
        * 该工具成功一次（连续失败清零）→ 发 ``run_progress_resumed`` 清掉卡住态。
        """
        memory = self._failure_memory.snapshot
        if not memory.repeated:
            if self._stuck_emitted:
                self._append_progress_resumed(run)
                self._stuck_emitted = False
                self._stuck_signature = None
                self._injected_failure_keys.clear()
                # 恢复后允许下一次卡住重新升级。
                self._escalated_reasons.clear()
            return
        signature = tuple(
            (
                item.tool_name,
                item.error_code,
                "restrict" if item.count >= 3 else "remind",
            )
            for item in memory.repeated
        )
        if signature == self._stuck_signature:
            return
        self._stuck_signature = signature
        self._stuck_emitted = True
        guidance = memory.guidance()
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_stuck",
            payload={
                "level": "restrict"
                if any(item.count >= 3 for item in memory.repeated)
                else "remind",
                "detector": "repeated_failure",
                "reasons": [
                    f"{item.tool_name} 已连续失败 {item.count} 次"
                    f"（{item.error_code}）"
                    for item in memory.repeated
                ],
                "repeatedFailures": [
                    item.as_json() for item in memory.repeated
                ],
                "attempts": list(memory.digest(limit=5)),
                "guidance": guidance,
            },
        )
        # 同一组（工具，错误码）只注入一次，避免每轮重复占上下文。
        new_keys = {
            (item.tool_name, item.error_code)
            for item in memory.repeated
            if (item.tool_name, item.error_code) not in self._injected_failure_keys
        }
        if guidance and new_keys:
            self._injected_failure_keys.update(new_keys)
            self._steering_messages.append(guidance)

    def _observe_no_progress_stuck(self, run: RunRecord, evaluation: object) -> None:
        """C2：no-progress 评估（启用时）也落到同一条 run.stuck 通道。

        只在级别变化时发事件，级别回落 ``none`` 时发恢复事件，避免每轮刷屏。
        """
        level = getattr(getattr(evaluation, "level", None), "value", None)
        if level in (None, "none"):
            if self._no_progress_stuck_level is not None:
                self._no_progress_stuck_level = None
                self._append_progress_resumed(run)
            return
        if level == self._no_progress_stuck_level:
            return
        self._no_progress_stuck_level = level
        memory = self._failure_memory.snapshot
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_stuck",
            payload={
                "level": level,
                "detector": getattr(evaluation, "detector", None),
                "reasons": list(getattr(evaluation, "reasons", ()) or ()),
                "consecutive": getattr(evaluation, "consecutive", None),
                "repeatedFailures": [
                    item.as_json() for item in memory.repeated
                ],
                "attempts": list(memory.digest(limit=5)),
                "guidance": memory.guidance(),
            },
        )

    def _load_user_profile(self, conversation_id: str) -> Optional[UserProfileBlock]:
        """B5：读取已存画像（只读一行；失败一律当作没有画像）。"""
        provider = self._user_profile_provider
        if provider is None:
            return None
        try:
            return provider(conversation_id)
        except Exception:  # noqa: BLE001 画像读取失败不影响运行
            logger.debug("User profile load failed", exc_info=True)
            return None

    def _side_effect_evidence(self, tool_calls: Sequence[ProviderToolCall]) -> list[str]:
        """本次运行里"有副作用"的工具调用（local_write / external_action）。"""
        evidence: list[str] = []
        for call in tool_calls:
            try:
                definition = self._tool_registry.resolve(call.name).definition
            except Exception:
                continue
            effect = getattr(definition.effect, "value", definition.effect)
            if effect and str(effect) != "read_only":
                evidence.append(f"{call.name}（{effect}）")
        return evidence

    def _verification_goal(self, run: RunRecord) -> str:
        """验证器的"目标"：本次运行的触发内容。"""
        if run.trigger_content_override:
            return run.trigger_content_override
        try:
            entry = self._repository.get_entry(run.trigger_entry_id)
        except Exception:
            return ""
        content = entry.payload.get("content") if isinstance(entry.payload, dict) else None
        return content if isinstance(content, str) else ""

    async def _maybe_verify(
        self,
        run: RunRecord,
        *,
        candidate: str,
        side_effects: Sequence[str],
        progress: EscalationProgress,
        cancellation_token: CancellationToken,
    ) -> Optional[VerificationVerdict]:
        """C1：制造者产出后，由独立验证者（独立上下文）判定结果。

        验证器只看目标 + 产出 + 工具证据，不共享制造者历史；判定写入
        ``run_verified`` 事件；判 fail 时同时走 C4 升级通道（带结论重试/接管）。
        验证失败（网络/超时）不改变本次运行结果——只记 uncertain。
        """
        if not should_verify(
            self._verifier_mode,
            has_side_effects=bool(side_effects),
            candidate=candidate,
        ):
            return None
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_verifying",
            payload={"runId": run.id, "model": self._verifier_model or self._model},
        )
        messages = build_verifier_messages(
            goal=self._verification_goal(run),
            candidate=candidate,
            evidence=tuple(side_effects),
        )
        model = self._verifier_model or self._model
        request = ProviderRequest(
            request_id=f"{run.id}:verify",
            model=model,
            messages=messages,
            max_output_tokens=min(512, self._max_output_tokens),
            temperature=0.0,
            tools=(),
        )
        started = time.monotonic()
        text_parts: list[str] = []
        completion: Optional[ProviderCompleted] = None
        try:
            async for event in self._provider.stream(request, cancellation_token):
                cancellation_token.raise_if_cancelled()
                if isinstance(event, ProviderTextDelta):
                    text_parts.append(event.text)
                elif isinstance(event, ProviderCompleted):
                    completion = event
        except Exception:
            logger.exception(
                "Verifier call failed",
                extra={"run_id": run.id},
            )
            verdict = VerificationVerdict(
                verdict="uncertain",
                reasons=("验证器调用失败，未能得到独立结论。",),
                model=model,
            )
        else:
            verdict = parse_verdict("".join(text_parts), model=model)
        verdict = replace(
            verdict,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=getattr(completion, "input_tokens", None),
            output_tokens=getattr(completion, "output_tokens", None),
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_verified",
            payload=verdict.as_json(),
        )
        if verdict.failed:
            self._escalate_verification_failure(run, verdict, progress=progress)
        return verdict

    def _escalate_verification_failure(
        self,
        run: RunRecord,
        verdict: VerificationVerdict,
        *,
        progress: EscalationProgress,
    ) -> None:
        """验证不通过 → 升级人工（C4），报告里带上验证结论。"""
        if VERIFICATION_REASON in self._escalated_reasons:
            return
        self._escalated_reasons.add(VERIFICATION_REASON)
        memory = self._failure_memory.snapshot
        report = build_escalation_report(
            reason=VERIFICATION_REASON,
            progress=progress,
            budget=EscalationBudget(used_tokens=0, limit_tokens=None),
            memory=memory,
            will_stop=False,
            verdict=verdict,
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_awaiting_user",
            payload=report.as_json(),
        )

    async def _finalize_time_budget(
        self,
        run: RunRecord,
        *,
        content_parts: list[str],
        input_tokens: int,
        output_tokens: int,
        model_turn_ids: list[str],
        side_effects: list[str],
        progress: EscalationProgress,
        cancellation_token: CancellationToken,
    ) -> RunExecutionResult:
        """到预算上限 → 优雅收尾：升级 + 引导语进正文 + 记 COMPLETED。

        为什么不沿用 SafetyStopError：那会把运行记为 FAILED，而到点是"阶段性交付"
        而不是"故障"。形态对齐已有的 no_progress STOP 路径（runner 里
        `finish_reason="no_progress_stop"` 那段）。

        M3-3：这条路径现在同时服务**时间到墙**与**成本到顶**（仅子运行档
        `stop_on_cost=True`）——同一套 `will_stop=True` + 引导语 + COMPLETED 语义，
        不另造第二套终止机制；差别只在"因为什么停下"（原因 / 终态标签 / 引导语）。

        调用方是谁不靠新参数区分（方法签名是冻结面，不许动）：三个时间调用点都只在
        `remaining_hard <= 0` 时进来，成本调用点只在墙体还早时进来，所以"墙是否真的
        过去"就能还原调用方。判据不成立时一律按**今天**的时间语义走——父档与既有的
        时间收尾逐字节不变。
        """
        remaining = self._run_budget.remaining_hard(progress.elapsed_seconds)
        cost_stop = (
            self._run_budget.stop_on_cost
            and (remaining is None or remaining > 0)
            and cost_cap_exceeded(
                cost_usd=self._cumulative_cost(progress),
                cap_usd=self._cost_cap_usd,
            )
        )
        reason = COST_REASON if cost_stop else TIME_REASON
        guidance = COST_BUDGET_GUIDANCE if cost_stop else TIME_BUDGET_GUIDANCE
        finish_reason = "cost_budget_stop" if cost_stop else "time_budget_stop"
        self._maybe_escalate(
            run,
            progress=progress,
            used_tokens=input_tokens + output_tokens,
            reason=reason,
            will_stop=True,
        )
        content_parts.append("\n\n" + guidance)
        await self._maybe_verify(
            run,
            candidate="".join(content_parts),
            side_effects=side_effects,
            progress=progress,
            cancellation_token=cancellation_token,
        )
        final_run, assistant_entry = self._repository.finalize_run(
            run.id,
            content="".join(content_parts),
            finish_reason=finish_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return self._result(
            RunStatus.COMPLETED,
            final_run,
            "".join(content_parts),
            input_tokens,
            output_tokens,
            tuple(model_turn_ids),
            (),
            assistant_entry_id=assistant_entry.id,
        )

    def _maybe_escalate(
        self,
        run: RunRecord,
        *,
        progress: EscalationProgress,
        used_tokens: int,
        reason: Optional[str] = None,
        will_stop: bool = False,
    ) -> None:
        """C4：无进展或预算将尽时发 ``run_awaiting_user``（同一原因只提一次）。

        升级不是暂停运行：报告给出进展/卡点/可选项，运行继续推进；若安全停止
        策略将结束本次运行，报告里 ``willStop`` 会说明。

        M3-3 的成本策略：预算带 ``stop_on_cost``（子运行档）时，成本到顶由主循环
        顶部的收尾路径统一发报告（``will_stop=True``），这里**不再先发一份**"继续
        推进"的建议；不带这个策略位（父档）时行为逐字节不变。
        """
        memory = self._failure_memory.snapshot
        cost = self._cumulative_cost(progress)
        cost_exceeded = cost_cap_exceeded(
            cost_usd=cost,
            cap_usd=self._cost_cap_usd,
        )
        if reason is None:
            if COST_REASON not in self._escalated_reasons and cost_exceeded:
                if self._run_budget.stop_on_cost:
                    # M3-3：这一档的成本不是"提请"而是**上界**。跨过上限之后运行要么
                    # 在下一轮顶部被优雅收尾（报告由那条路径带 will_stop=True 发出），
                    # 要么本轮就自己交付完成（运行已经结束，谈不上"继续推进"）——两种
                    # 情况下这里都不该先发一份"继续推进会继续消耗预算"的建议，否则同一
                    # 件事被报两次、且第一份还在劝它继续。
                    return
                reason = COST_REASON
            elif (
                TIME_REASON not in self._escalated_reasons
                and self._run_budget.reached_soft(progress.elapsed_seconds)
            ):
                reason = TIME_REASON
            elif NO_PROGRESS_REASON not in self._escalated_reasons and any(
                item.count >= 3 for item in memory.repeated
            ):
                reason = NO_PROGRESS_REASON
            elif BUDGET_REASON not in self._escalated_reasons and budget_exhausted(
                used_tokens=used_tokens,
                limit_tokens=self._escalation_limit_tokens(),
                ratio=self._escalation_budget_ratio,
            ):
                reason = BUDGET_REASON
        # 同一原因只提请注意一次；但"将要停止"必须让用户看到——即使此前就同一
        # 原因提请过注意（例如软时长阈值提示过后，到硬上限仍要报 willStop）。
        if reason is None:
            return
        if reason in self._escalated_reasons and not will_stop:
            return
        self._escalated_reasons.add(reason)
        report = build_escalation_report(
            reason=reason,
            progress=progress,
            budget=EscalationBudget(
                used_tokens=used_tokens,
                limit_tokens=self._escalation_limit_tokens(),
                used_seconds=progress.elapsed_seconds,
                limit_seconds=self._run_budget.hard_seconds,
            ),
            memory=memory,
            will_stop=will_stop,
            cost=(
                {
                    "usedUsd": cost,
                    "capUsd": self._cost_cap_usd,
                    "priced": cost is not None,
                }
                if reason == COST_REASON
                else None
            ),
        )
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_awaiting_user",
            payload=report.as_json(),
        )

    def _cumulative_cost(self, progress: EscalationProgress) -> Optional[float]:
        """按定价表估算本次运行累计成本（未定价模型返回 None）。"""
        return estimate_cost_usd(
            self._pricing_catalog,
            provider=getattr(self._provider, "name", None),
            model=self._model,
            input_tokens=progress.input_tokens,
            output_tokens=progress.output_tokens,
        )

    def _escalation_limit_tokens(self) -> Optional[int]:
        """可用上下文上限：窗口减去必须留给输出的配额。"""
        if self._context_window_tokens is None:
            return None
        limit = self._context_window_tokens - self._max_output_tokens
        return limit if limit > 0 else None

    def _append_progress_resumed(self, run: RunRecord) -> None:
        """C2：卡住态解除（失败记忆清零或 no-progress 级别回落）。"""
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="run_progress_resumed",
            payload={"runId": run.id},
        )

    async def _drain_steering_messages(self, run: RunRecord) -> list[ProviderMessage]:
        async with self._queue_lock:
            messages = self._steering_messages
            self._steering_messages = []
        projected: list[ProviderMessage] = []
        for message in messages:
            projected.append(ProviderMessage(role="user", content=message))
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="steer_injected",
                payload={"content": message},
            )
        return projected

    async def _maybe_compact_context(
        self,
        run: RunRecord,
        provider_messages: list[ProviderMessage],
    ) -> None:
        if self._compaction_hook is None:
            return
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_started",
            payload={},
        )
        result = await self._compaction_hook.compact(
            run,
            tuple(provider_messages),
        )
        if result is None:
            self._repository.append_runtime_event(
                run_id=run.id,
                event_type="compaction_completed",
                payload={"changed": False},
            )
            return
        self._run_compacted = True
        if result.summary_entry_id is not None:
            # entry 层面压缩:summary entry 已固化到 lane,从当前 leaf 重建投影。
            # covered entries 由 ContextProjection 跳过,当前 run 立即收敛;
            # 后续 Run 的投影同样命中同一规则。
            entries = self._repository.list_lane_context_entries(run.lane_id)
            projection = self._context_projection.project(entries)
            provider_messages[:] = [
                *self._context_prefix_messages,
                *projection.messages,
            ]
        else:
            provider_messages[:] = list(result.messages)
        self._repository.append_runtime_event(
            run_id=run.id,
            event_type="compaction_completed",
            payload={
                "changed": True,
                "summaryEntryId": result.summary_entry_id,
                "coveredEntryIds": list(result.covered_entry_ids),
            },
        )

    def _append_current_model_turn_ids(
        self,
        run_id: str,
        model_turn_ids: list[str],
    ) -> None:
        existing = set(model_turn_ids)
        model_turn_ids.extend(
            turn.id
            for turn in self._repository.list_model_turns(run_id)
            if turn.id not in existing
        )

    def _result(
        self,
        status: RunStatus,
        run: RunRecord,
        content: str,
        input_tokens: int,
        output_tokens: int,
        model_turn_ids: tuple[str, ...],
        pending_approval_ids: tuple[str, ...],
        *,
        assistant_entry_id: Optional[str] = None,
    ) -> RunExecutionResult:
        if self._no_progress_observer is not None:
            _evaluate_no_progress_shadow(
                repository=self._repository,
                run_id=run.id,
                observer=self._no_progress_observer,
            )
        return RunExecutionResult(
            status=status,
            run=run,
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model_turn_ids=model_turn_ids,
            pending_approval_execution_ids=pending_approval_ids,
            follow_up_messages=tuple(self._follow_up_messages),
            assistant_entry_id=assistant_entry_id,
            tool_call_count=self._tool_call_count,
        )
