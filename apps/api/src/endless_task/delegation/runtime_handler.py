"""Composition-root bridge for delegation tools (M4A DR-2 slice 1).

Implements :class:`DelegationToolHandler` over the in-process coordinator
without importing runtime_v2 (boundary discipline): every production
dependency is injected at construction by composition root.

- ``kernel_provider(request)`` returns the AgentKernel that executes
  children for the parent run owning ``request.run_id`` (composition root
  supplies the RuntimeV2AgentKernel production adapter),
- ``capability_provider(request)`` returns the parent run's effective
  capabilities (workspace binding / profile resolution lives at
  composition root),
- a ``profile_resolver`` narrows children to the declared profile layer.

Per-parent coordinators are cached in-process so query/cancel/list/steer
across tool calls resolve the same coordinator the spawn used. Because
production builds one kernel instance per parent run and a kernel only
knows the runs it started, kernel-dependent control (steer/cancel) is
routed through the child's own row to the coordinator that holds it, not
through the current call's run id — see ``_coordinator_for_child``.
The spawn idempotency key is the parent tool call id (06 §8.6): a
retried spawn for the same call id is refused instead of double-executed.
"""

from __future__ import annotations

import inspect
import logging

from dataclasses import replace
from datetime import datetime, timezone

from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from endless_task.agent_kernel import AgentKernel
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.recency import parse_timestamp
from endless_task.runtime_ledger import TraceContext
from endless_task.tool_platform import (
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

from .agent_tools import (
    CANCEL_AGENT_NAME,
    QUERY_AGENT_NAME,
    SPAWN_AGENT_NAME,
    DelegationToolHandler,
    child_run_handle_json,
)
from .child_context import DEFAULT_MAX_SUMMARY_CHARACTERS
from .child_run_store import ChildRunRow, ChildRunState
from .coordinator import MAX_LISTED_CHILDREN, InProcessChildCoordinator
from .limits import READ_ONLY_PROFILE_NAMES
from .protocol import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildStatus,
    SpawnSpec,
    WorkspaceMode,
)

logger = logging.getLogger(__name__)

KernelProvider = Callable[[ToolExecutionRequest], AgentKernel]
CapabilityProvider = Callable[[ToolExecutionRequest], frozenset[str]]

#: Capabilities a read-only research child requests by default (subset of
#: the builtin subagent_readonly profile; the parent must hold them all).
#:
#: ``memory.read`` is intentionally NOT requested: product memory is
#: injected via <runtime-memory> context, not exposed as a tool, so no
#: registered tool grants it and the parent's tool-union capability set
#: never contains it — requesting it would make every real spawn fail
#: closed as an escalation (real-provider E2E finding, 06 §27).
_READ_ONLY_CHILD_CAPABILITIES = frozenset(
    {"workspace.read", "session.query"}
)


class CoordinatorDelegationHandler:
    """DelegationToolHandler backed by per-parent in-process coordinators."""

    #: 缓存里最多保留多少个父运行的协调器；超出时回收最久且空闲的那些。
    #: 库是真相，所以回收只影响"能否对老子运行下发取消"，不影响查询。
    MAX_TRACKED_PARENT_RUNS = 32

    def __init__(
        self,
        *,
        kernel_provider: KernelProvider,
        capability_provider: CapabilityProvider,
        profile_resolver: Optional[object] = None,
        default_timeout_seconds: float = 120.0,
        # M4B P2b-i: enables ISOLATED_SNAPSHOT children on this handler's
        # coordinators.
        isolated_write_enabled: bool = False,
        # M4B P2b-ii: prepares the isolated scratch workspace for a child
        # (conversation_id, child_run_id) -> workspace_id (composition root
        # closure over the workspace resolver/repositories).
        isolated_workspace_provider=None,
        # 切片 9：子运行记录存储；注入后子运行生命周期跨重启可查。
        child_run_store=None,
        # 异步派发：子运行在后台跑，关停要能取消它们；不注入则造一个进程级主管。
        task_supervisor=None,
        # 切片 9 M2：子运行终态回流父会话的唤醒服务（鸭子类型，有 schedule 即用）；
        # 不注入 = 只落库不回流。它是进程级单例，所有协调器共用同一个。
        child_wakeup=None,
        # 调度上限（06 §5.6）：并发/在册/队列的容量。不注入 = 协调器用默认值。
        batch_limits=None,
    ) -> None:
        if not callable(kernel_provider):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "Delegation handler requires a kernel provider",
            )
        if not callable(capability_provider):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "Delegation handler requires a capability provider",
            )
        if (
            not isinstance(default_timeout_seconds, (int, float))
            or isinstance(default_timeout_seconds, bool)
            or not 1.0 <= float(default_timeout_seconds) <= 86_400.0
        ):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "default_timeout_seconds must be within (0, 86400]",
            )
        self._kernel_provider = kernel_provider
        self._capability_provider = capability_provider
        self._profile_resolver = profile_resolver
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._isolated_write_enabled = bool(isolated_write_enabled)
        self._isolated_workspace_provider = isolated_workspace_provider
        self._child_run_store = child_run_store
        self._child_wakeup = child_wakeup
        self._batch_limits = batch_limits
        self._task_supervisor = (
            task_supervisor
            if task_supervisor is not None
            else BackgroundTaskSupervisor(name="delegation-children")
        )
        self._coordinators: dict[str, InProcessChildCoordinator] = {}

    # -- DelegationToolHandler ---------------------------------------------

    def child_workspace_id_for(
        self, child_run_id: str, *, conversation_id: Optional[str] = None
    ) -> str:
        """Isolated scratch workspace id of one completed child (P3).

        ``conversation_id`` 是本次代答所属的会话：库在多个会话之间共享，传了它
        就只认同一会话的子运行（别的会话一律当作不存在）；不传（None/空）保持
        旧的无范围行为，兼容拿不到会话上下文的调用方。边界是会话而不是运行——
        与 :meth:`InProcessChildCoordinator.query` 的判定保持一致。
        """
        for coordinator in self._coordinators.values():
            # 缓存里可能有别的会话的协调器（库在会话之间共享）：代答范围不是本
            # 会话就跳过，等于没见过这个子运行（与 :meth:`_owns_row` 的边界一致）。
            if not coordinator.covers_conversation(conversation_id):
                continue
            try:
                return coordinator.child_workspace_id(child_run_id)
            except AgentPlatformError as error:
                if error.code in (
                    "delegation_child_not_isolated",
                    "delegation_child_not_completed",
                ):
                    raise
                continue
        # 内存协调器可能已被回收（或换了进程）：以库回落，否则隔离写子代理
        # 在父运行回收后再也拿不到 patch 目标。回落必须与内存路径的判定**同序**：
        # 先隔离、后完成。顺序反了会让「只读 + 未完成」的子运行在内存里报
        # not_isolated、被收回后改报 not_completed——同一个 id 两个错码，调用方会
        # 误以为"换个时机就能 apply"。
        if self._child_run_store is not None:
            row = self._child_run_store.get_child_run(child_run_id)
            if row is not None and (
                not conversation_id or row.parent_conversation_id == conversation_id
            ):
                if row.workspace_mode != WorkspaceMode.ISOLATED_SNAPSHOT.value:
                    raise AgentPlatformError(
                        "delegation_child_not_isolated",
                        "该子代理不是隔离写模式，无 scratch workspace。",
                        retryable=False,
                    )
                if row.state is not ChildRunState.COMPLETED:
                    raise AgentPlatformError(
                        "delegation_child_not_completed",
                        "隔离写子代理未完成，暂不能产出 patch。",
                        retryable=False,
                    )
                if not row.child_workspace_id:
                    raise AgentPlatformError(
                        "delegation_child_not_isolated",
                        "该子代理不是隔离写模式，无 scratch workspace。",
                        retryable=False,
                    )
                return row.child_workspace_id
        raise AgentPlatformError(
            "delegation_unknown_child",
            "No child run is tracked under this id",
            retryable=False,
            details={"child_run_id": child_run_id},
        )

    async def spawn_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        try:
            coordinator = self._coordinator_for(request)
        except AgentPlatformError as error:
            return _failed(request, error)
        task = str(request.arguments.get("task", "")).strip()
        if not task:
            return _failed(
                request,
                AgentPlatformError(
                    "invalid_delegation_task",
                    "spawn_agent 的 task 必须是文本。",
                ),
            )
        timeout = request.arguments.get("timeoutSeconds")
        if timeout is None:
            timeout = self._default_timeout_seconds
        schema_arg = request.arguments.get("expectedOutputSchema")
        mode = str(request.arguments.get("mode") or "readonly").strip().lower()
        if mode not in ("readonly", "isolated_write"):
            return _failed(
                request,
                AgentPlatformError(
                    "invalid_delegation_mode",
                    "spawn_agent 的 mode 只允许 readonly 或 isolated_write。",
                ),
            )
        workspace_mode = (
            WorkspaceMode.READ_ONLY_SHARED
            if mode == "readonly"
            else WorkspaceMode.ISOLATED_SNAPSHOT
        )
        spec = SpawnSpec(
            task=task,
            expected_output=ChildOutputSchema(
                name=_output_schema_name(request),
                schema=schema_arg if isinstance(schema_arg, Mapping) else {"type": "object"},
            ),
            capability_profile=_read_only_profile_name(),
            requested_capabilities=_READ_ONLY_CHILD_CAPABILITIES,
            tool_allowlist=(),
            model_policy=ChildModelPolicy(preferred_model=None, allow_fallback=False),
            context_policy=ChildContextPolicy(),
            workspace_mode=workspace_mode,
            timeout_seconds=float(timeout),
            parent_run_id=request.run_id,
            parent_tool_call_id=request.call_id,
        )
        try:
            if mode == "isolated_write":
                if not self._isolated_write_enabled:
                    raise AgentPlatformError(
                        "delegation_isolated_write_disabled",
                        "全局 delegation 模式未开启 isolated_write（ENDLESS_TASK_DELEGATION），"
                        "不能以隔离写模式创建子代理。",
                        retryable=False,
                    )
                if not callable(self._isolated_workspace_provider):
                    raise AgentPlatformError(
                        "delegation_isolated_workspace_unavailable",
                        "隔离写子代理需要 scratch workspace 提供方。",
                        retryable=False,
                    )
                prepared = coordinator.prepare_spawn(spec)
                workspace_id = self._isolated_workspace_provider(
                    request.conversation_id, prepared.child_run_id
                )
                if inspect.isawaitable(workspace_id):
                    workspace_id = await workspace_id
                spec = replace(spec, child_workspace_id=workspace_id)
                prepared = replace(prepared, spec=spec)
                child_run_id = coordinator.start_child(prepared)
            else:
                child_run_id = coordinator.start(spec)
        except AgentPlatformError as error:
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=(
                f"子代理已启动（运行中）：{child_run_id}；"
                "用 query_agent 查询它的结果。"
            ),
            structured_content=child_run_handle_json(child_run_id, "running"),
        )

    async def query_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        child_run_id = str(request.arguments.get("childRunId", "")).strip()
        try:
            coordinator = self._coordinator_for(request)
            outcome = await coordinator.query(child_run_id)
        except AgentPlatformError as error:
            if error.code == "delegation_child_running":
                return self._progress_answer(request, child_run_id)
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=outcome.summary[:20_000],
            structured_content=_outcome_json(
                outcome, row=self._row_for_child(child_run_id)
            ),
        )

    def _progress_answer(
        self, request: ToolExecutionRequest, child_run_id: str
    ) -> ToolOutcome:
        """运行中/排队中的回答（规格 M2-3）。

        状态与已用时都走既有派生（:func:`child_run_status` /
        :func:`child_run_elapsed_seconds`），不另造第二条推导：协调器的
        ``delegation_child_running`` 同时覆盖"闸门外排队"与"真在跑"，只有库行的
        ``started_at`` 能分辨——把排队中的答成 running 会让模型去 steer 一个内核
        里不存在的运行。

        协调器只在**库行仍是 running** 时才抛这个错误，所以正常路径上这一行必然
        在；真读不到（并发删除一类的极端情况）就退回旧回答，宁可不给进展字段也
        不编造计时。
        """
        row = self._row_for_child(child_run_id)
        if row is None:
            return ToolOutcome(
                call_id=request.call_id,
                status=ToolOutcomeStatus.COMPLETED,
                content=f"子代理仍在运行中：{child_run_id}",
                structured_content=child_run_handle_json(child_run_id, "running"),
            )
        status = child_run_status(row)
        elapsed_seconds = child_run_elapsed_seconds(row, datetime.now(timezone.utc))
        payload = dict(child_run_handle_json(child_run_id, status))
        payload["startedAt"] = row.started_at
        payload["elapsedSeconds"] = elapsed_seconds
        # updatedAt 是"最近一次活动"而不是 startedAt 的副本：中间可能又写过
        # injected_at/reported_at 之类的标记。
        payload["updatedAt"] = row.updated_at
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=_render_progress_text(child_run_id, status, elapsed_seconds),
            structured_content=payload,
        )

    async def cancel_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        """取消一个子运行（工具面入口）。

        会话边界**只有一份实现**：库里有这一行时一律走
        :meth:`cancel_child_for_conversation`——HTTP 取消端点用的是同一个方法，
        工具面不再自带第二条"这行算不算我的"规则。旧路径经
        ``InProcessChildCoordinator._owns_row`` 判定，而它在任一侧没有会话 id 时
        退回运行 id 比较（空侧回落），与 HTTP 面的严格相等（空 ⇒ 不是我的一律
        查无此子运行）在 `parent_conversation_id` 为空的行上会给出相反答案。

        :meth:`_coordinator_for_child` 只留给**库里没有这一行**的情况：没有注入
        store 的构造路径下，终态记录只存在于协调器的 ``_children`` 内存里，那种
        路径只能按本轮请求找到协调器。
        """
        child_run_id = str(request.arguments.get("childRunId", "")).strip()
        reason = request.arguments.get("reason")
        reason_text = str(reason) if reason is not None else "用户取消"
        try:
            if self._row_for_child(child_run_id) is not None:
                status = await self.cancel_child_for_conversation(
                    request.conversation_id, child_run_id, reason_text
                )
            else:
                coordinator = self._coordinator_for_child(request, child_run_id)
                status = await self._cancel_via_coordinator(
                    coordinator, child_run_id, reason_text
                )
        except AgentPlatformError as error:
            return _failed(request, error)
        if status == "already_terminal":
            # Terminal children need no cancellation; report it as a
            # benign no-op rather than an error.
            return ToolOutcome(
                call_id=request.call_id,
                status=ToolOutcomeStatus.COMPLETED,
                content="子代理已结束，无需取消。",
                structured_content={
                    "childRunId": child_run_id,
                    "status": status,
                },
            )
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="取消请求已发送。",
            structured_content={
                "childRunId": child_run_id,
                "status": status,
            },
        )

    async def list_children(self, request: ToolExecutionRequest) -> ToolOutcome:
        """本会话的子运行清单（最近创建的在前，上限由本 handler 把关）。

        与 HTTP 面**同一份**枚举实现（``list_children_for_conversation``）：枚举
        只读库，不需要内核，所以不再为它建一个协调器。
        """
        rows = self.list_children_for_conversation(request.conversation_id)
        now = datetime.now(timezone.utc)
        children = [_child_run_json(row, now=now) for row in rows]
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=_render_children_text(children),
            structured_content={"children": children, "total": len(children)},
        )

    async def steer_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        """给运行中的子运行送一句纠偏；终态按良性 no-op 回答。"""
        child_run_id = str(request.arguments.get("childRunId", "")).strip()
        message = str(request.arguments.get("message") or "").strip()
        try:
            coordinator = self._coordinator_for_child(request, child_run_id)
            await coordinator.steer(child_run_id, message)
        except AgentPlatformError as error:
            if error.code == "delegation_child_not_active":
                # 终态子运行不需要纠偏：按既有风格回良性 no-op，而不是报错。
                return ToolOutcome(
                    call_id=request.call_id,
                    status=ToolOutcomeStatus.COMPLETED,
                    content="子代理已结束，无需纠偏。",
                    structured_content={
                        "childRunId": child_run_id,
                        "status": "already_terminal",
                    },
                )
            # 排队中（delegation_child_not_started）与未知/跨会话一律如实回失败：
            # 内核里没有它的活动运行，谎报"已纠偏"会让模型以为话带到了。
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="纠偏消息已发送给子代理。",
            structured_content={
                "childRunId": child_run_id,
                "status": "steering",
            },
        )

    # -- coordinator lifecycle ---------------------------------------------

    async def cancel_children_of_conversation(
        self, conversation_id: str, reason: str
    ) -> int:
        """父会话被删除时终止它名下**仍在跑**的子运行，返回实际下发的取消条数。

        为什么需要它：删除路径只沿 `parent_conversation_id` 级联（
        `list_descendant_ids`），而委派子会话身上挂的是 `delegation_parent_run_id`
        ——那是子运行 id，不在级联链上；v2 的 runs/entries 又按子会话级联，
        于是父会话删掉之后子运行照跑，没有任何东西收得住它。库里的
        `delegation_child_runs.parent_conversation_id` 是唯一能枚举出来的父子
        关联，所以这里按它取名册。

        纪律（每条都是刻意的）：

        * **按子运行自己的 `parent_run_id` 路由到持有它的协调器**：生产上每个父
          运行一个新内核，子运行刻意比父运行活得久，按本次调用的运行 id 找协调器
          会落在一个对它一无所知的新内核上（与 :meth:`_coordinator_for_child`
          同一个根因）。
        * **绝不写终态**：还在跑的子运行手里握着自己的后台任务，它会写自己的终态行
          （已取消/已完成）；这里替它写只会和那次写入打架。因此返回的只是"下发成功"
          的条数，不是"已经确认终态"的条数。
        * **绝不抛**：调用方是父会话删除路径——回流侧的任何毛病都不能把删除挡住。
          整段兜底；单条下发失败也只记日志继续（取消失败最常见的原因是子运行刚好
          跑完的良性竞态，不能因此把同会话其余子运行落下）。
        * **拥有者不在本进程**（已回收/换进程/本次没走过 spawn）：只记日志跳过。
          启动时的对账扫尾是这种情况的网；凭库行硬改状态会骗过真正在跑的它。
        * **只认本会话**：库在多个会话之间共享，枚举到的行还要核一次拥有者是否
          代答同一会话（与 :meth:`InProcessChildCoordinator.covers_conversation`
          同一条边界）。

        子会话本体**不删**：它是子运行的审计记录，删掉还会把子运行自己的 v2 run
        行一起级联掉，让 `delegation_child_runs` 悬挂。
        """
        try:
            store = self._child_run_store
            if store is None:
                return 0
            rows = store.list_child_runs_for_conversation(conversation_id)
            cancelled = 0
            for row in rows:
                if row.terminal:
                    continue
                owner = self._coordinators.get(row.parent_run_id)
                if owner is None or not owner.covers_conversation(conversation_id):
                    logger.info(
                        "子运行没有可达的拥有者协调器，跳过取消（交给启动对账）",
                        extra={
                            "child_run_id": row.child_run_id,
                            "parent_run_id": row.parent_run_id,
                            "parent_conversation_id": conversation_id,
                        },
                    )
                    continue
                self._touch(row.parent_run_id)
                try:
                    await owner.cancel(row.child_run_id, reason)
                except Exception:  # noqa: BLE001 - 单条失败不能落下其余子运行
                    logger.exception(
                        "取消父会话的子运行失败，跳过这一条",
                        extra={
                            "child_run_id": row.child_run_id,
                            "parent_run_id": row.parent_run_id,
                            "parent_conversation_id": conversation_id,
                        },
                    )
                    continue
                cancelled += 1
            return cancelled
        except Exception:  # noqa: BLE001 - 删除路径绝不能被回流侧挡住
            logger.exception(
                "枚举父会话的子运行失败，本次取消放弃",
                extra={"parent_conversation_id": conversation_id},
            )
            return 0

    # -- 会话键的读/控面（HTTP 路由与工具面共用同一条边界） -----------------

    def list_children_for_conversation(
        self, conversation_id: str
    ) -> tuple[ChildRunRow, ...]:
        """本会话派过的子运行**库行**，最近创建的在前，最多 50 条。

        为什么读库而不是问协调器：库是唯一真相，而且进程回收/换进程之后协调器
        可能已经不在了（枚举不该因此变空）；按会话的边界由
        ``list_child_runs_for_conversation`` 的查询本身把关，与会话内任何运行的
        可见范围一致（``InProcessChildCoordinator._owns_row``）。

        顺序与协调器的会话边界同源（``_owns_row`` 划定的可见范围就是这条查询）：
        先按库的 ``created_at`` 正序取出，再按 ``(created_at, child_run_id)`` 反序
        取前 :data:`MAX_LISTED_CHILDREN` 条——这个上限**只有这一处实现**（工具面
        `list_agents` 与 HTTP 列表端点共用）。payload 里 items 就是这个顺序（最近
        的在前），``total`` 是**返回条数**（与工具面同口径，不是库里的总行数）。
        """
        if self._child_run_store is None:
            return ()
        rows = self._child_run_store.list_child_runs_for_conversation(conversation_id)
        ordered = sorted(
            rows,
            key=lambda row: (row.created_at, row.child_run_id),
            reverse=True,
        )
        return tuple(ordered[:MAX_LISTED_CHILDREN])

    async def cancel_child_for_conversation(
        self, conversation_id: str, child_run_id: str, reason: str
    ) -> str:
        """取消本会话的一个子运行，返回 ``cancelling`` / ``already_terminal``。

        这是会话边界（以及边界之后的终态/路由语义）的**唯一实现**：HTTP 取消
        端点直接用它，工具面 ``cancel_child`` 在库里有这一行时也用它——两边不会
        各自演化出一条"这行算不算我的"规则。

        HTTP 面没有 ``ToolExecutionRequest``（没有本轮 run_id / 内核提供方），所以
        不能走 :meth:`_coordinator_for_child` 的"回落本轮协调器"那一支；但**边界
        与路由是同一份**：会话边界先按库行核一次（不属于本会话 = 查无此子运行，
        不泄漏存在性），再把取消交给 ``parent_run_id`` 指向的、代答同一会话的
        协调器（:meth:`_owning_coordinator_for_row`），最后与工具面共用
        :meth:`_cancel_via_coordinator` 的终态语义。

        终态**先按库行判**：拥有者协调器早就被回收的旧子运行照样要能"取消"成
        良性 no-op，否则页签会在历史子运行上莫名报错。非终态却找不到可达的
        拥有者时**不谎报成功**：只是回一个 ``cancelling`` 会让页签显示"取消中"
        而子运行照跑，所以抛可重试的错误让调用方如实呈现。
        """
        row = self._row_for_child(child_run_id)
        if row is None or not _row_belongs_to_conversation(row, conversation_id):
            raise AgentPlatformError(
                "delegation_unknown_child",
                "No child run is tracked under this id",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        if row.terminal:
            return "already_terminal"
        owner = self._owning_coordinator_for_row(row, conversation_id)
        if owner is None:
            raise AgentPlatformError(
                "delegation_child_owner_unavailable",
                "该子运行的执行协调器已不在本进程内，暂不能取消。",
                retryable=True,
                details={"child_run_id": child_run_id},
            )
        return await self._cancel_via_coordinator(owner, child_run_id, reason)

    async def _cancel_via_coordinator(
        self, coordinator, child_run_id: str, reason: str
    ) -> str:
        """向持有子运行的协调器下发取消，回小状态串（工具面与 HTTP 面共用）。

        ``delegation_child_not_active`` 是良性 no-op（子运行在我们下发前刚好跑完
        的竞态），映射成 ``already_terminal`` 而不是错误；其余错误照抛——谎报
        "已取消"会让调用方以为子运行停下了。
        """
        try:
            await coordinator.cancel(child_run_id, reason)
        except AgentPlatformError as error:
            if error.code == "delegation_child_not_active":
                return "already_terminal"
            raise
        return "cancelling"

    def _row_for_child(self, child_run_id: str) -> Optional[ChildRunRow]:
        """库里的那一行；没有注入 store（或库里没有）时为 None。"""
        if self._child_run_store is None:
            return None
        return self._child_run_store.get_child_run(child_run_id)

    def _parent_depth_of(self, run_id: str) -> int:
        """该运行自己的委派深度：库里那一行说了算（顶层运行没有行 = 0）。

        协调器按 parent run id 建，而 ``request.run_id`` 就是它自己的 run id；
        "我是某个父运行的子运行"这件事在父协调器 ``_reserve`` 时就落了库
        （``delegation_child_runs.depth``），所以这里查得到；查不到即顶层运行。

        缺 store（嵌入式/部分测试的装配）时同样退回 0——**这是有意 fail-open**：
        否则顶层运行会因为"查不到自己"而拒绝派子运行。生产装配总是注入 store。

        另外两条边界见路线图 ⑧ 与发布说明 §2.3：深度是 **per-run 而非
        per-conversation**（同一子会话里出现第二个 run 会被读成 0），且历史
        ``depth`` 值**不能当嵌套统计**（缺陷期写下的孙行也是 1）。
        """
        row = self._row_for_child(run_id)
        return row.depth if row is not None else 0

    def _owning_coordinator_for_row(
        self, row: ChildRunRow, conversation_id: str
    ) -> Optional[InProcessChildCoordinator]:
        """按库行的 ``parent_run_id`` 找出持有该子运行、且代答本会话的协调器。

        找到却属于别的会话时**不路由**（返回 None）：否则会话边界会被路由悄悄
        拆掉——与 :meth:`cancel_children_of_conversation` 同一条纪律。
        """
        owner = self._coordinators.get(row.parent_run_id)
        if owner is None or not owner.covers_conversation(conversation_id):
            return None
        self._touch(row.parent_run_id)
        return owner

    def _touch(self, parent_run_id: str) -> None:
        """把父运行标成"刚用过"（LRU 的最近端）。

        `dict` 保序，所以"用过" = 移到末尾；淘汰从头部开始扫，取到的就是最久未用。
        只对已在缓存里的条目生效（未命中时由 `_coordinator_for` 新建，天然落在末尾）。
        只在**交出现有协调器**的路径上调用：全表扫描（`child_workspace_id_for`）与
        观测口（`tracked_parent_run_ids`）都不 touch——否则观测会改变行为。
        """
        coordinator = self._coordinators.pop(parent_run_id, None)
        if coordinator is not None:
            self._coordinators[parent_run_id] = coordinator

    def _coordinator_for_child(
        self, request: ToolExecutionRequest, child_run_id: str
    ) -> InProcessChildCoordinator:
        """找出**真正持有**这个子运行的内核与后台任务句柄的协调器。

        为什么要多绕这一下：生产装配（``api/wiring.py`` 的
        ``_delegation_kernel_provider``）**每个父运行新建一个内核实例**，而内核
        只认自己启动过的运行（``RuntimeV2AgentKernel`` 的 ``_active`` 是实例级
        字典）。一次用户消息就是一个运行，所以上一轮 spawn 出来的子运行虽然库行
        还是 running，活动运行与后台任务句柄都在**上一轮那个协调器**手里。若只按
        ``request.run_id`` 找协调器（:meth:`_coordinator_for`），跨轮次的
        steer/cancel 就会拿到一个新内核：它对那个子运行一无所知，抛
        ``invalid_agent_kernel_value``；而复核库行又发现它确实还在 running，于是
        工具以一个不透明的错误失败，子运行继续失控地跑。

        归属与边界仍由被选中协调器的 ``_owns_row`` 判定；按 ``parent_run_id``
        找到的协调器若不属于本次请求的会话，则**不路由**（否则会话边界会被这里
        悄悄拆掉），回落 :meth:`_coordinator_for` 保持"跨会话等于不存在"的既有
        错误。没有 store 或库里没有这一行时同样回落。
        """
        row = self._row_for_child(child_run_id)
        if row is not None:
            owner = self._owning_coordinator_for_row(row, request.conversation_id)
            if owner is not None:
                return owner
        return self._coordinator_for(request)

    def _coordinator_for(self, request: ToolExecutionRequest) -> InProcessChildCoordinator:
        existing = self._coordinators.get(request.run_id)
        if existing is not None:
            self._touch(request.run_id)
            return existing
        self._evict_idle_coordinators()
        kernel = self._kernel_provider(request)
        capabilities = self._capability_provider(request)
        if not isinstance(capabilities, frozenset):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "Capability provider must return a frozenset",
            )
        coordinator = InProcessChildCoordinator(
            kernel,
            parent_run_id=request.run_id,
            parent_conversation_id=request.conversation_id,
            parent_capabilities=capabilities,
            parent_trace=_trace_for(request),
            parent_depth=self._parent_depth_of(request.run_id),
            profile_resolver=self._profile_resolver,
            isolated_write_enabled=self._isolated_write_enabled,
            child_run_store=self._child_run_store,
            task_supervisor=self._task_supervisor,
            child_wakeup=self._child_wakeup,
            batch_limits=self._batch_limits,
        )
        self._coordinators[request.run_id] = coordinator
        return coordinator

    def _evict_idle_coordinators(self) -> None:
        """缓存超界时回收**最久未用且空闲**的协调器（库是真相，查询不掉）。

        唯一的硬约束：**不得回收还有非终态子运行的协调器**——跨轮次的取消/纠偏靠
        它持有内核与任务句柄。因此这个界是"软"的：所有条目都忙时**允许暂时超过**
        （调用方接下来还会插入新条目），并 debug 记一笔；拿在飞子运行的句柄去换
        一个数字是更坏的交易。这个方法在**插入之前**被调用，所以"腾到
        ``len < MAX``" + "插入一个"正好停在 ``MAX``。
        """
        if len(self._coordinators) < self.MAX_TRACKED_PARENT_RUNS:
            return
        for parent_run_id, coordinator in tuple(self._coordinators.items()):
            if len(self._coordinators) < self.MAX_TRACKED_PARENT_RUNS:
                return
            if coordinator.has_inflight_children():
                continue  # 还有未终态子运行：回收会牵动正在用的内核
            self._coordinators.pop(parent_run_id, None)
        if len(self._coordinators) >= self.MAX_TRACKED_PARENT_RUNS:
            logger.debug(
                "delegation.coordinators.all_busy tracked=%d limit=%d parents=%s",
                len(self._coordinators),
                self.MAX_TRACKED_PARENT_RUNS,
                ",".join(sorted(self._coordinators)),
            )

    async def drain(self) -> None:
        """等所有后台子运行结束（测试用；生产关停走 :meth:`shutdown`）。"""
        await self._task_supervisor.drain()

    async def shutdown(self) -> None:
        """关停：取消失败会让进程退出挂住，所以这里**取消**而不是干等。

        主管关掉之后还要逐个协调器收尾：只关主管的话，"没跑过第一个事件循环步
        就被取消"的子运行会一直停在 running，直到下次进程启动对账。
        """
        await self._task_supervisor.shutdown(cancel=True)
        for coordinator in tuple(self._coordinators.values()):
            coordinator.finalize_cancelled_children()

    @property
    def tracked_parent_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._coordinators))


def _trace_for(request: ToolExecutionRequest) -> TraceContext:
    return TraceContext(
        trace_id=f"trace_{request.correlation_id}",
        run_id=request.run_id,
        correlation_id=request.correlation_id,
    )


def _read_only_profile_name() -> str:
    return sorted(READ_ONLY_PROFILE_NAMES)[0]


def _output_schema_name(request: ToolExecutionRequest) -> str:
    return f"child_output_{request.call_id}"


def _outcome_json(outcome, row: Optional[ChildRunRow] = None) -> Mapping[str, Any]:
    """终态回答的 structured_content（既有字段一个不改，另加进展字段）。

    ``startedAt`` / ``terminalAt`` / ``elapsedSeconds`` 是规格 M2-3 补的进展面，
    全部从库行派生（终态时已用时冻结在 ``terminal_at``）。``row`` 缺省——未注入
    store 的构造路径——时三个键**照样产出**但为 null：键必须稳定存在，否则工具
    契约会在两种形状之间摇摆，而 null 才是"本进程没有这一行的计时记录"的如实
    表达。
    """
    payload: dict[str, Any] = {
        "childRunId": outcome.child_run_id,
        "status": outcome.status.value,
        "summary": outcome.summary,
        "diagnostics": [
            {"code": item.code, "message": item.safe_message}
            for item in outcome.diagnostics
        ],
        "startedAt": row.started_at if row is not None else None,
        "terminalAt": row.terminal_at if row is not None else None,
        "elapsedSeconds": (
            child_run_elapsed_seconds(row, datetime.now(timezone.utc))
            if row is not None
            else None
        ),
    }
    if outcome.confidence is not None:
        payload["confidence"] = outcome.confidence
    return payload


def _row_belongs_to_conversation(row: ChildRunRow, conversation_id: str) -> bool:
    """库行是否属于该会话（会话键的严格判定）。

    与 ``InProcessChildCoordinator._owns_row`` 同一目的，差别只在"任一侧没有会话
    id"时的回落：那里拿着 ``ToolExecutionRequest``，可以退回运行 id 比较；会话键
    的调用方没有运行 id 可比，所以一律当作**不属于**——宁可 404，也不把别处的
    子运行放进来（不泄漏存在性）。
    """
    return bool(conversation_id) and row.parent_conversation_id == conversation_id


def child_run_status(row: ChildRunRow) -> str:
    """库里时间戳派生的三态之一：``queued``（还在闸门外）/ ``running`` / 终态。

    ``ChildRunState.RUNNING`` 同时覆盖"排队中"与"真在跑"，只有 ``started_at``
    能分辨；把排队中的报成 running 会让模型去 steer 一个内核里不存在的运行。
    """
    if row.state is ChildRunState.RUNNING and row.started_at is None:
        return "queued"
    return row.state.value


def child_run_elapsed_seconds(row: ChildRunRow, now: datetime) -> Optional[int]:
    """已用时秒数：从开始到结束（还没结束就到现在）。

    终态行用 ``terminal_at`` 收口：已经结束的子运行若继续按 now 计时，会给出一个
    随时间增长的"已用时"，那是假信息。还没开始（排队中）或时间戳解析不了时为 None。
    """
    start = parse_timestamp(row.started_at)
    if start is None:
        return None
    end = parse_timestamp(row.terminal_at) or now
    return max(0, int((end - start).total_seconds()))


def truncate_child_task(task: str) -> str:
    """任务摘要截断（上限与 ``<delegated-children>`` 注入块同源）。"""
    flattened = " ".join(task.split())
    if len(flattened) <= DEFAULT_MAX_SUMMARY_CHARACTERS:
        return flattened
    return flattened[:DEFAULT_MAX_SUMMARY_CHARACTERS] + "…"


def _child_run_json(row: ChildRunRow, *, now: datetime) -> Mapping[str, Any]:
    # patchesApplied（"是否已应用 patch"）暂不产出：apply_child_patches 目前
    # 不留任何可读标记，编一个值只会骗模型。
    return {
        "childRunId": row.child_run_id,
        "status": child_run_status(row),
        "task": truncate_child_task(row.task),
        "startedAt": row.started_at,
        "elapsedSeconds": child_run_elapsed_seconds(row, now),
        "isolatedWrite": row.workspace_mode == WorkspaceMode.ISOLATED_SNAPSHOT.value,
    }


def _render_progress_text(
    child_run_id: str, status: str, elapsed_seconds: Optional[int]
) -> str:
    """非终态回答的人类可读文本：排队中与已开始跑分开说（规格 M2-3）。

    ``elapsed_seconds`` 为 None 只可能是 ``started_at`` 解析不了（正常 running 行
    必有它）：那时只报状态，不编一个时长。
    """
    if status == "queued":
        return f"子代理仍在排队（尚未开始）：{child_run_id}"
    if elapsed_seconds is None:
        return f"子代理仍在运行中：{child_run_id}"
    return f"子代理仍在运行中（已用时 {elapsed_seconds} 秒）：{child_run_id}"


def _render_children_text(children: Sequence[Mapping[str, Any]]) -> str:
    """给模型的文本摘要：structured_content 之外的一份可读副本。

    ``len(children)`` 是**页大小**（最多 50 条），不是该会话的子运行总数，所以措辞
    只能是"最近 N 个"——与前端「子任务」页签的「最近 N 条」同义，不能写成"共有 N
    个"，否则模型会把分页当成全集。
    """
    if not children:
        return "本会话没有子运行。"
    lines = [f"本会话最近 {len(children)} 个子运行（最多 50 条，最近创建的在前）："]
    lines.extend(
        f"- {item['childRunId']} | {item['status']} | {item['task']}"
        for item in children
    )
    return "\n".join(lines)


def _failed(request: ToolExecutionRequest, error: AgentPlatformError) -> ToolOutcome:
    return ToolOutcome(
        call_id=request.call_id,
        status=ToolOutcomeStatus.FAILED,
        content=error.safe_message,
        diagnostic=SafeDiagnostic(code=error.code, safe_message=error.safe_message),
    )


__all__ = [
    "CoordinatorDelegationHandler",
    "KernelProvider",
    "CapabilityProvider",
    # 派生/截断只有一份实现：HTTP 路由的 payload 直接复用这些（不另造表示）。
    "child_run_elapsed_seconds",
    "child_run_status",
    "truncate_child_task",
]
