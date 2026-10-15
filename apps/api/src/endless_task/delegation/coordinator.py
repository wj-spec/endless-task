"""In-process single-child coordinator (M4A DR-0 slice 2).

DR-0 builds the coordinator boundary without a scheduler or model tools:

- a coordinator is owned by one parent run (``parent_run_id``) and can
  only be constructed from that parent's capability set,
- ``spawn`` turns a validated :class:`SpawnSpec` into an AgentKernel
  :class:`RunCommand` whose user message is the child task plus the
  explicit excerpts declared in :class:`ChildContextPolicy` — never the
  parent transcript (06 §5.3),
- workspace mode and effective capabilities are stage-gated: M4A only
  opens ``none`` / ``read_only_shared`` and refuses children whose
  effective capabilities include write/process/external capabilities
  (isolated write children are M4B, 06 §9 DR-4),
- child run ids are deterministic from ``(parent_run_id,
  parent_tool_call_id)`` so a duplicate spawn for the same parent tool
  call is detected instead of double-executed (06 §8.6 idempotency key),
- ``spawn`` / ``execute_prepared`` execute the child through the injected
  AgentKernel and project the result with :mod:`.result_projection`; they
  await the terminal outcome, so they keep blocking semantics,
- ``start`` / ``start_child`` reserve the same spawn key and hand that very
  coroutine to an injected ``BackgroundTaskSupervisor`` instead, returning
  the child handle at once — a child that outlives the parent turn no
  longer holds the parent's model turn. Shutdown cancels such children and
  records them as ``cancelled`` rather than waiting for them.

The coordinator is *Not Integrated*: no feature flag, no composition
root wiring, no ledger persistence. It proves the DR-0 acceptance gates
(capability 提升禁止 + 结果有界/schema 受控) against a real AgentKernel
instance (FakeAgentKernel in tests, the production kernel later).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional

from endless_task.agent_kernel import (
    AgentKernel,
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunUsage,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
# 库层拒绝收尾（行已经终态 / 行已不存在）是"另一个写者先到"的良性竞态：
# 委派层允许依赖 domain，用它的稳定异常类型把这种失败与真故障区分开。
from endless_task.domain.repositories import InvalidStateError
from endless_task.runtime_ledger import TraceContext

from .child_run_store import ChildRunRow, ChildRunState, ChildRunStore
from .limits import (
    READ_ONLY_FORBIDDEN_CAPABILITIES,
    READ_ONLY_PROFILE_NAMES,
    ChildCapabilityDecision,
    compute_child_capabilities,
)
from .protocol import (
    ChildOutcome,
    ChildStatus,
    DelegationRuntime,
    SpawnSpec,
    UsageSummary,
    WorkspaceMode,
)
from .result_projection import ChildResultPolicy, project_child_outcome

if TYPE_CHECKING:
    # scheduler 反向依赖本模块（InProcessChildScheduler），顶层导入会成环，
    # 所以类型期才导入；运行期的默认值走 __init__ 里的局部导入。
    from .scheduler import ChildBatchLimits

logger = logging.getLogger(__name__)

#: Workspace modes M4A (read-only delegation) may execute.
READ_ONLY_WORKSPACE_MODES = frozenset(
    {
        WorkspaceMode.NONE,
        WorkspaceMode.READ_ONLY_SHARED,
    }
)

#: Maximum delegation depth for the DR-0 coordinator (root = depth 0).
MAX_DEPTH = 1

#: 会话键枚举（`list_agents` 工具与 HTTP 列表端点共用的
#: `CoordinatorDelegationHandler.list_children_for_conversation`）单次返回的最大
#: 条数：输出的上界，避免把整段历史灌进模型上下文 / 页签 payload。
MAX_LISTED_CHILDREN = 50


class ChildRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: 协调器终态 → 存储状态（TIMEOUT 没有独立形态：硬墙是优雅收尾为 COMPLETED）。
_STORE_STATE_BY_STATUS = {
    ChildRunStatus.COMPLETED: ChildRunState.COMPLETED,
    ChildRunStatus.FAILED: ChildRunState.FAILED,
    ChildRunStatus.CANCELLED: ChildRunState.CANCELLED,
}

#: 协议侧"终态"状态集合：直接取"能落库的协议状态"的键集（唯一真相源），
#: 将来多一个终态只需改上面那张映射。`ChildStatus.TIMEOUT/UNKNOWN` 全仓无产出点
#: （已核：`grep -rn "ChildStatus.TIMEOUT\|ChildStatus.UNKNOWN" src tests` 零命中）。
_TERMINAL_CHILD_STATUSES = frozenset(_STORE_STATE_BY_STATUS)


@dataclass(frozen=True)
class ChildRunRecord:
    """One completed child run tracked by the coordinator."""

    child_run_id: str
    parent_run_id: str
    parent_tool_call_id: str
    depth: int
    spec: SpawnSpec
    decision: ChildCapabilityDecision
    status: ChildRunStatus
    outcome: ChildOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.spec, SpawnSpec):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child run record requires a SpawnSpec",
            )
        if not isinstance(self.decision, ChildCapabilityDecision):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child run record requires a capability decision",
            )
        if not isinstance(self.outcome, ChildOutcome):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child run record requires a ChildOutcome",
            )
        if self.outcome.child_run_id != self.child_run_id:
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child run record and outcome must reference the same child run",
            )


@dataclass(frozen=True)
class PreparedChildSpawn:
    """Validated spawn intent, ready for kernel execution.

    Splitting validation from execution lets a scheduler fail fast on
    every spec in a batch before any child starts, while keeping the
    synchronous single-child path of :meth:`InProcessChildCoordinator.spawn`
    byte-for-byte equivalent.
    """

    spec: SpawnSpec
    child_run_id: str
    depth: int
    decision: ChildCapabilityDecision

    def __post_init__(self) -> None:
        if not isinstance(self.spec, SpawnSpec):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Prepared spawn requires a SpawnSpec",
            )
        if not isinstance(self.decision, ChildCapabilityDecision):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Prepared spawn requires a capability decision",
            )
        if not isinstance(self.depth, int) or self.depth <= 0:
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Prepared spawn depth must be a positive integer",
            )


class InProcessChildCoordinator:
    """Single-child coordinator over one AgentKernel.

    Two entry pairs share one execution body: ``spawn`` / ``execute_prepared``
    await the child to its terminal outcome, while ``start`` / ``start_child``
    detach it onto the injected ``task_supervisor`` and return the handle
    immediately.

    Construction is bound to the owning parent run: capabilities are the
    parent's, so a spawned child can never widen them (intersection is
    computed against ``parent_capabilities`` at spawn time).

    DR-1 (read-only child) profile semantics are enforced here:

    - only profile names in the M4A read-only set may spawn (default
      ``READ_ONLY_PROFILE_NAMES`` = ``subagent_readonly``); a SpawnSpec
      carrying another profile name is refused before any kernel work,
    - when a :class:`CapabilityProfileResolver`-shaped ``profile_resolver``
      is injected, the resolved profile capability layer participates in
      the intersection instead of defaulting to the requested set, so a
      child can never receive capabilities its profile does not grant.

    The DR-1 "child uses the same AgentKernel factory" requirement is a
    composition-root concern: this coordinator already accepts any
    AgentKernel instance, and the production kernel (02 §11.3) is a later
    milestone, so no factory seam is invented here.
    """

    #: 每个协调器内存里最多保留多少条**终态**记录；超出即丢（库是真相）。
    #: 保留窗口只服务快路径（query/cancel/steer 的命中分支），不是正确性依赖。
    MAX_RETAINED_TERMINAL_RECORDS = 32

    def __init__(
        self,
        kernel: AgentKernel,
        *,
        parent_run_id: str,
        parent_capabilities: frozenset[str],
        parent_trace: TraceContext,
        parent_depth: int = 0,
        result_policy: Optional[ChildResultPolicy] = None,
        allowed_profile_names: Optional[frozenset[str]] = None,
        profile_resolver: Optional[object] = None,
        # M4B P2b-i: when enabled, ISOLATED_SNAPSHOT workspace mode may be
        # requested (isolated scratch child); off = M4A read-only gates.
        isolated_write_enabled: bool = False,
        parent_conversation_id: str = "",
        child_run_store: Optional[ChildRunStore] = None,
        # 异步派发：注入后 start/start_child 才可用。**不自己造**——异步入口
        # 要求显式注入，避免把后台任务的归属藏进协调器内部。
        task_supervisor: Optional[object] = None,
        # 唤醒服务（切片 9 M2）：终态落库后**尽力**通知父会话。鸭子类型——有
        # schedule 就用；不注入 = 只落库不回流（注入块仍是下一轮的兜底通道）。
        child_wakeup: Optional[object] = None,
        # 并发闸门：本父运行的在册/同时在跑上限（06 §5.6 默认并发 4 / 在册 8）。
        batch_limits: Optional[ChildBatchLimits] = None,
    ) -> None:
        if not callable(getattr(kernel, "run", None)):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child coordinator requires an AgentKernel",
            )
        if not isinstance(parent_capabilities, frozenset):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "parent_capabilities must be a frozenset",
            )
        if parent_depth < 0:
            raise AgentPlatformError(
                "invalid_delegation_value",
                "parent_depth must be non-negative",
            )
        if result_policy is not None and not isinstance(result_policy, ChildResultPolicy):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "result_policy must be a ChildResultPolicy",
            )
        allowed = (
            frozenset(READ_ONLY_PROFILE_NAMES)
            if allowed_profile_names is None
            else frozenset(allowed_profile_names)
        )
        if not allowed:
            raise AgentPlatformError(
                "invalid_delegation_value",
                "allowed_profile_names must not be empty",
            )
        if profile_resolver is not None and not callable(
            getattr(profile_resolver, "resolve", None)
        ):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "profile_resolver must expose resolve(name)",
            )
        if child_run_store is not None and not callable(
            getattr(child_run_store, "reserve_child_run", None)
        ):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "child_run_store must expose reserve_child_run(...)",
            )
        if task_supervisor is not None and not callable(
            getattr(task_supervisor, "spawn", None)
        ):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "task_supervisor must expose spawn(awaitable, *, name=..., on_done=...)",
            )
        self._parent_conversation_id = parent_conversation_id
        self._child_run_store = child_run_store
        self._task_supervisor = task_supervisor
        self._child_wakeup = child_wakeup
        if batch_limits is None:
            # 局部导入：scheduler 反向依赖本模块（InProcessChildScheduler），
            # 顶层互导会成环。
            from .scheduler import ChildBatchLimits

            batch_limits = ChildBatchLimits()
        self._batch_limits = batch_limits
        # 惰性创建：信号量绑到创建它的那个事件循环，构造期还没有循环可绑。
        self._child_semaphore: Optional[asyncio.Semaphore] = None
        #: 在册名额（排队闸门）：容量 = max_children_per_parent，与并发闸门同形状。
        self._child_admission_semaphore: Optional[asyncio.Semaphore] = None
        self._kernel = kernel
        self._parent_run_id = parent_run_id
        self._parent_capabilities = parent_capabilities
        self._parent_trace = parent_trace
        self._parent_depth = parent_depth
        self._result_policy = result_policy if result_policy is not None else ChildResultPolicy()
        self._allowed_profile_names = allowed
        self._profile_resolver = profile_resolver
        self._isolated_write_enabled = bool(isolated_write_enabled)
        #: ``_children`` 里**只有终态记录**：两个插入点都在子运行跑完之后才写入
        #: （内核抛异常 → ``_failed_record``；正常收尾 → ``_record_for_outcome``），
        #: 所以这个 dict 的长度就是"这位父运行派完过多少个"，不是"多少个在飞"。
        self._children: dict[str, ChildRunRecord] = {}
        self._spawn_keys: dict[tuple[str, str], str] = {}
        # 后台任务按子运行 id 登记：闸门外排队的子运行内核里还没有活动运行，
        # 显式取消只能靠取消这个任务来落地。
        self._child_tasks: dict[str, asyncio.Task] = {}

    # -- DelegationRuntime -------------------------------------------------

    async def spawn(self, spec: SpawnSpec) -> str:
        prepared = self.prepare_spawn(spec)
        await self.execute_prepared(prepared)
        return prepared.child_run_id

    def prepare_spawn(self, spec: SpawnSpec) -> PreparedChildSpawn:
        """Validate a spawn intent and produce its execution plan.

        Pure with respect to the kernel and the spawn-key map: no child is
        started and nothing is reserved, so a scheduler can validate a whole
        batch (fail fast on any gate violation) before executing any child.
        Reservation happens atomically inside :meth:`execute_prepared`
        (no await between the duplicate check and the key write), which
        keeps the synchronous single-child spawn path equivalent.
        """
        if not isinstance(spec, SpawnSpec):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child spawn requires a SpawnSpec",
            )
        if spec.parent_run_id != self._parent_run_id:
            raise AgentPlatformError(
                "delegation_parent_mismatch",
                "SpawnSpec parent run does not own this coordinator",
                retryable=False,
                details={
                    "coordinator_parent_run_id": self._parent_run_id,
                    "spec_parent_run_id": spec.parent_run_id,
                },
            )
        child_depth = self._parent_depth + 1
        if child_depth > MAX_DEPTH:
            raise AgentPlatformError(
                "delegation_max_depth_exceeded",
                "Delegation depth exceeds the allowed maximum",
                retryable=False,
                details={"max_depth": MAX_DEPTH, "attempted_depth": child_depth},
            )
        allowed_modes = frozenset(READ_ONLY_WORKSPACE_MODES)
        if self._isolated_write_enabled:
            allowed_modes = allowed_modes | {WorkspaceMode.ISOLATED_SNAPSHOT}
        if spec.workspace_mode not in allowed_modes:
            raise AgentPlatformError(
                "delegation_workspace_mode_not_supported",
                (
                    "Delegation only supports none / read_only_shared"
                    if not self._isolated_write_enabled
                    else "Delegation supports none / read_only_shared / isolated_snapshot"
                ),
                retryable=False,
                details={"workspace_mode": spec.workspace_mode.value},
            )
        if spec.capability_profile not in self._allowed_profile_names:
            raise AgentPlatformError(
                "delegation_profile_not_allowed",
                "SpawnSpec capability profile is not allowed for this coordinator",
                retryable=False,
                details={
                    "allowed_profiles": sorted(self._allowed_profile_names),
                    "requested_profile": spec.capability_profile,
                },
            )
        profile_capabilities = self._resolve_profile_capabilities(spec)
        decision = compute_child_capabilities(
            spec,
            parent_capabilities=self._parent_capabilities,
            profile_capabilities=profile_capabilities,
        )
        forbidden = decision.effective & READ_ONLY_FORBIDDEN_CAPABILITIES
        isolated = (
            self._isolated_write_enabled
            and spec.workspace_mode is WorkspaceMode.ISOLATED_SNAPSHOT
        )
        if forbidden and not isolated:
            raise AgentPlatformError(
                "delegation_write_child_not_supported",
                "M4A delegation is read-only; write/process/external capabilities "
                "are not executed until M4B isolated-write delegation",
                retryable=False,
                details={"forbidden": sorted(forbidden)},
            )
        key = (spec.parent_run_id, spec.parent_tool_call_id)
        self._ensure_spawn_key_free(key)
        return PreparedChildSpawn(
            spec=spec,
            child_run_id=_child_run_id_for(key),
            depth=child_depth,
            decision=decision,
        )

    async def execute_prepared(self, prepared: PreparedChildSpawn) -> ChildRunRecord:
        """Run one prepared child through the kernel and record the outcome."""
        if not isinstance(prepared, PreparedChildSpawn):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child execution requires a PreparedChildSpawn",
            )
        self._reserve(prepared)
        record = await self._run_reserved(prepared)
        return record

    def start_child(self, prepared: PreparedChildSpawn) -> str:
        """预留后交给后台任务跑，立刻返回句柄（不等结果）。

        与 :meth:`execute_prepared` 的区别只有一处：后者 await 到底，
        前者把协程交给 ``BackgroundTaskSupervisor``。**同步方法**——预留
        （写库 + 占 spawn key）本来就没有 await 点，所以工具调用面不需要
        为它制造一个可被取消的等待窗口。

        错误语义也随之外移：后台 body 里的失败（含内核契约违规）不再回到
        调用方的返回值，只由主管记日志，调用方之后要靠 query 才看得到。
        """
        if not isinstance(prepared, PreparedChildSpawn):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Child start requires a PreparedChildSpawn",
            )
        if self._task_supervisor is None:
            raise AgentPlatformError(
                "delegation_task_supervisor_missing",
                "异步派发需要注入后台任务主管（BackgroundTaskSupervisor）。",
                retryable=False,
            )
        self._reserve(prepared)
        try:
            task = self._task_supervisor.spawn(
                self._run_reserved(prepared),
                name=f"delegation-child:{prepared.child_run_id}",
            )
            self._child_tasks[prepared.child_run_id] = task
        except RuntimeError as error:
            # 主管已关闭（关停窗口）：预留已经写库，不能把行留在 running。
            self._spawn_keys.pop(
                (prepared.spec.parent_run_id, prepared.spec.parent_tool_call_id), None
            )
            if self._child_run_store is not None:
                self._child_run_store.mark_child_terminal(
                    prepared.child_run_id,
                    state=ChildRunState.FAILED,
                    error_code="delegation_supervisor_closed",
                    safe_message="后台任务主管已关闭，子运行未能启动。",
                )
            raise AgentPlatformError(
                "delegation_task_supervisor_closed",
                "后台任务主管已关闭，子运行未能启动。",
                retryable=False,
            ) from error
        return prepared.child_run_id

    def start(self, spec: SpawnSpec) -> str:
        """校验 + 预留 + 后台执行，立刻返回 child_run_id。"""
        return self.start_child(self.prepare_spawn(spec))

    async def drain(self) -> None:
        """等所有后台子运行结束（测试用；生产关停走 :meth:`shutdown`）。"""
        if self._task_supervisor is not None:
            await self._task_supervisor.drain()

    async def shutdown(self) -> None:
        """关停：取消失败会让进程退出挂住，所以这里**取消**而不是干等。"""
        if self._task_supervisor is not None:
            await self._task_supervisor.shutdown(cancel=True)
        self.finalize_cancelled_children()

    def finalize_cancelled_children(self) -> None:
        """关停后收尾：把本协调器**托管的后台任务**里仍未终态的行补标为 cancelled。

        只看本协调器真正交给主管跑过的子运行（``_child_tasks``）——阻塞路径
        （``spawn`` / ``execute_prepared``）上的子运行不归主管管，标掉它们会让
        在途的 ``_store_terminal`` 撞 InvalidStateError。

        任务在"第一个事件循环步之前"就被取消时，``_run_reserved`` 的取消处理器
        不会执行，所以库行只能在这里补；反过来，已经写过终态的行会被跳过。
        """
        tracked = tuple(self._child_tasks)
        self._child_tasks.clear()
        if self._child_run_store is None or not tracked:
            return
        try:
            rows = {
                row.child_run_id: row
                for row in self._child_run_store.list_child_runs(self._parent_run_id)
            }
        except Exception:
            logger.exception(
                "读取子运行记录失败，关停收尾已跳过",
                extra={"parent_run_id": self._parent_run_id},
            )
            return
        for child_run_id in tracked:
            row = rows.get(child_run_id)
            if row is not None and not row.terminal:
                self._mark_child_cancelled(child_run_id)

    def running_child_count(self) -> int:
        if self._task_supervisor is None:
            return 0
        return self._task_supervisor.running_count()

    def inflight_child_count(self) -> int:
        """本父运行**已预留、未终态**的子运行数（在册数）。

        有 store 时以库为准（跨进程/跨轮次都准），没有 store 时退回本协调器
        托管的后台任务数。``has_inflight_children`` / ``_reserve`` / 调度器三处
        共用它，避免"在册"这件事出现第三个定义。
        """
        if self._child_run_store is not None:
            return sum(
                1
                for row in self._child_run_store.list_child_runs(self._parent_run_id)
                if not row.terminal
            )
        return self.running_child_count()

    def has_inflight_children(self) -> bool:
        """本协调器（本父运行）是否还有未终态的子运行。

        不能用 supervisor 的全局在跑数：一个 handler 下的所有协调器共用同一个
        主管，全局计数会让"别的父运行有子运行在跑"把我们这里也判成忙，
        回收就永远不会发生。
        """
        return self.inflight_child_count() > 0

    def _reserve(self, prepared: PreparedChildSpawn) -> None:
        """占住 spawn key，并把子运行落成 running（有 store 时）。"""
        spec = prepared.spec
        key = (spec.parent_run_id, spec.parent_tool_call_id)
        self._ensure_spawn_key_free(key)
        # 这里只守**绝对**上限：在册容量满了不拒绝，改成在 admission 闸门外排队
        # （见 `_child_admission_slots`）；没有绝对上限的排队就是无界队列。
        tracked = self.inflight_child_count()
        if tracked >= self._batch_limits.max_queued_children:
            raise AgentPlatformError(
                "delegation_children_limit_exceeded",
                "在册（含排队）的子运行数量已达上限。",
                retryable=False,
                details={
                    "max_queued_children": self._batch_limits.max_queued_children,
                    "tracked": tracked,
                },
            )
        if self._child_run_store is not None:
            row = self._child_run_store.reserve_child_run(
                child_run_id=prepared.child_run_id,
                parent_run_id=spec.parent_run_id,
                parent_tool_call_id=spec.parent_tool_call_id,
                parent_conversation_id=self._parent_conversation_id,
                depth=prepared.depth,
                capability_profile=spec.capability_profile,
                workspace_mode=spec.workspace_mode.value,
                timeout_seconds=spec.timeout_seconds,
                task=spec.task,
                child_workspace_id=spec.child_workspace_id,
            )
            if row is None:
                raise AgentPlatformError(
                    "delegation_duplicate_spawn",
                    "A child for this parent tool call was already spawned",
                    retryable=False,
                    details={
                        "child_run_id": self._spawn_key_hint(
                            spec.parent_run_id, spec.parent_tool_call_id
                        )
                    },
                )
        self._spawn_keys[key] = prepared.child_run_id

    def _ensure_spawn_key_free(self, key: tuple[str, str]) -> None:
        """内存映射里该 spawn key 已被占用就抛 `delegation_duplicate_spawn`。

        `prepare_spawn`（校验期快速失败）与 `_reserve`（真正占位）各需要同一条判定，
        抽在这里避免两份逐字重复的守卫各自演化。details 沿用内存映射里的占位者
        ——它必然非空，因为判定命中的就是它。跨进程（内存映射为空）那条重复由
        `_reserve` 里"库返回 None"的分支负责，details 由 `_spawn_key_hint` 回填。
        """
        existing = self._spawn_keys.get(key)
        if existing is not None:
            raise AgentPlatformError(
                "delegation_duplicate_spawn",
                "A child for this parent tool call was already spawned",
                retryable=False,
                details={"child_run_id": existing},
            )

    def _spawn_key_hint(self, parent_run_id: str, parent_tool_call_id: str) -> str:
        if self._child_run_store is None:
            return ""
        row = self._child_run_store.find_child_run_by_spawn_key(
            parent_run_id=parent_run_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        return row.child_run_id if row is not None else ""

    def _child_slots(self) -> asyncio.Semaphore:
        """并发闸门（惰性创建：信号量会绑到首次使用的事件循环）。"""
        if self._child_semaphore is None:
            self._child_semaphore = asyncio.Semaphore(
                self._batch_limits.max_concurrent_children
            )
        return self._child_semaphore

    def _child_admission_slots(self) -> asyncio.Semaphore:
        """在册名额（**排队**用的闸）：容量 = `max_children_per_parent`。

        与并发闸门的区别：并发名额限制"同时在跑"的子运行，在册名额限制
        "已预留、还没终态"的子运行。超出的子运行在**这里**排队——库里它已经是
        running 行但 `started_at` 仍为空，也就是 wire 上的 `queued`。
        """
        if self._child_admission_semaphore is None:
            self._child_admission_semaphore = asyncio.Semaphore(
                self._batch_limits.max_children_per_parent
            )
        return self._child_admission_semaphore

    async def _run_reserved(self, prepared: PreparedChildSpawn) -> ChildRunRecord:
        try:
            # 两个等待都发生在 `_mark_child_started` 之前，所以"等名额"与"等并发"
            # 在库里的样子完全一样：running ∧ started_at IS NULL（= wire 的 queued）。
            async with self._child_admission_slots():
                async with self._child_slots():
                    return await self._run_reserved_locked(prepared)
        except asyncio.CancelledError:
            # 关停/显式取消：无论子运行是在闸门外排队还是已经在内核里跑，
            # 库行都不能留在 running（否则要等下次启动对账）。
            self._mark_child_cancelled(prepared.child_run_id)
            raise
        finally:
            # 任务收尾即摘牌：cancel 靠这个表判断"还有没有活的后台任务"。
            self._child_tasks.pop(prepared.child_run_id, None)

    def _best_available_usage(self, child_run_id: str) -> Optional[UsageSummary]:
        """最好可用的用量：拿不到就是 ``None``（由调用方落 0，绝不编造）。

        内核是**鸭子类型**取用的（协调器构造期就用
        ``callable(getattr(kernel, "run", None))`` 校验），所以这里不往
        ``AgentKernel`` Protocol 里加成员；没有读口的老内核/假内核行为完全不变。

        **读口必须包在自己的 try/except 里（连同映射）**：在
        ``_mark_child_cancelled`` / ``_mark_child_failed_after_escape`` 里，一个在
        写库**之前**抛出的读口会中断整个收尾（外层只记日志），把库行永久留在
        ``running``。外来内核还可能**返回**（而不是抛出）一个形状不对的值——
        ``_usage_summary`` 会因此抛 ``AttributeError``（dict/字符串之类）或
        ``AgentPlatformError``（负数/非 int 字段），所以映射也必须在 guard **之内**：
        畸形快照一律降级成 ``None``（调用方据此落 0，绝不编造数字）。
        """
        probe = getattr(self._kernel, "usage_snapshot", None)
        if not callable(probe):
            return None
        try:
            snapshot = probe(child_run_id)
            return _usage_summary(snapshot) if snapshot is not None else None
        except Exception:  # noqa: BLE001 - 读口炸了/返回畸形值都不能拖垮收尾
            return None

    def _mark_child_cancelled(self, child_run_id: str) -> None:
        """尽力把子运行行标成 cancelled；写库失败只记日志，绝不覆盖原始取消。"""
        if self._child_run_store is None:
            return
        try:
            self._child_run_store.mark_child_terminal(
                child_run_id,
                state=ChildRunState.CANCELLED,
                safe_message="子运行在后台被取消。",
                # 取消时已经完成的模型轮是**真花掉**的 token：写 0 是一句假话
                # （M3 成本账会低估父预算）。拿不到快照才落 0。
                usage=self._best_available_usage(child_run_id),
            )
        except InvalidStateError:
            # 良性竞态：另一个写者（内核收尾 / 关停对账）已经把行写成了终态。
            # 取消的目标已经达成，不必留堆栈。（行不存在是 NotFoundError，
            # 那说明预留根本没落库，属于真异常，落到下面的 logger.exception。）
            logger.debug(
                "Child run row already terminal before cancel marking",
                extra={"child_run_id": child_run_id},
            )
        except Exception:
            logger.exception(
                "Failed to mark child run cancelled",
                extra={"child_run_id": child_run_id},
            )

    def _mark_child_started(self, child_run_id: str) -> None:
        """记下"真在跑"的时刻（已经在闸门内、即将调内核）。

        `started_at` 是区分"排队中"与"真在跑"的唯一依据，但它本身是观测量：
        写失败绝不能把一个已经具备执行条件的子运行挡在门外——挡回去只会让库行
        停在 running，反而更难查。所以这里吞异常 + 记日志，与 `_store_terminal`
        的"写库失败必须被看见"是两种不同的失败面。
        """
        if self._child_run_store is None:
            return
        try:
            self._child_run_store.mark_child_started(child_run_id)
        except Exception:
            logger.exception(
                "Failed to mark child run started",
                extra={"child_run_id": child_run_id},
            )

    async def _run_reserved_locked(self, prepared: PreparedChildSpawn) -> ChildRunRecord:
        """跑一个已预留的子运行；**任何逃逸都要留下终态**。

        `CancelledError`（取消/关停）由 `_run_reserved` 统一收尾，这里放行；
        其余一切（`_record_for_outcome` 的非契约异常、`_store_terminal` 写库失败、
        将来新加的任何一步）都在这里兜一次：库行绝不允许停在 running。
        """
        try:
            return await self._run_reserved_body(prepared)
        except asyncio.CancelledError:
            raise
        except BaseException:  # noqa: BLE001 - 兜底收尾，然后原样 re-raise
            self._mark_child_failed_after_escape(prepared.child_run_id)
            raise

    def _mark_child_failed_after_escape(self, child_run_id: str) -> None:
        """兜底把库行标成 failed；已经终态（良性竞态）只记 debug，绝不覆盖原始异常。"""
        if self._child_run_store is None:
            return
        try:
            self._child_run_store.mark_child_terminal(
                child_run_id,
                state=ChildRunState.FAILED,
                error_code="delegation_child_crashed",
                safe_message="子运行内部错误，已按失败收尾。",
                # 逃逸发生在哪一步决定快照是最终还是部分，但两者都比 0 诚实：
                # 内核可能已经真跑完并烧掉了 token。
                usage=self._best_available_usage(child_run_id),
            )
        except InvalidStateError:
            logger.debug(
                "兜底收尾时该行已经是终态", extra={"child_run_id": child_run_id}
            )
        except Exception:
            logger.exception("兜底收尾失败", extra={"child_run_id": child_run_id})

    async def _run_reserved_body(self, prepared: PreparedChildSpawn) -> ChildRunRecord:
        spec = prepared.spec
        key = (spec.parent_run_id, spec.parent_tool_call_id)
        # 闸门已拿到：从这里开始才是"真在跑"，started_at 记的就是这条分界。
        self._mark_child_started(prepared.child_run_id)
        try:
            outcome = await self._kernel.run(
                self._build_run_command(spec, prepared.child_run_id)
            )
        except Exception as error:  # kernel failure must not crash the parent
            record = self._failed_record(
                child_run_id=prepared.child_run_id,
                spec=spec,
                decision=prepared.decision,
                depth=prepared.depth,
                error=error,
                # 内核"成功跑完之后才抛"是真实可达的（投影/收尾里再炸）：那时
                # outcome 根本回不到这里，只有按 child_run_id 留存的快照能给真值。
                usage=self._best_available_usage(prepared.child_run_id),
            )
            self._children[prepared.child_run_id] = record
            self._store_terminal(record)
            return record
        try:
            record = self._record_for_outcome(
                child_run_id=prepared.child_run_id,
                spec=spec,
                decision=prepared.decision,
                depth=prepared.depth,
                outcome=outcome,
            )
        except AgentPlatformError:
            # 内核答回了错误的 run id：释放内存 spawn key，让本次进程内的重复判定
            # 不再挡住重试。但注入 store 后这样并不够——那一行已经是 FAILED 且仍占着
            # UNIQUE (parent_run_id, parent_tool_call_id)，所以修正后的重试必须换一个
            # 新的 parent_tool_call_id 才能真正预留成功。
            self._spawn_keys.pop(key, None)
            if self._child_run_store is not None:
                self._child_run_store.mark_child_terminal(
                    prepared.child_run_id,
                    state=ChildRunState.FAILED,
                    error_code="delegation_contract_violation",
                    safe_message="子运行内核返回了不匹配的 run id。",
                    # **只认按 child_run_id 留存的那份快照**：`outcome.usage` 属于
                    # 一个我们刚拒绝归属的 run id，把它记到这条子运行头上就是错记。
                    usage=self._best_available_usage(prepared.child_run_id),
                )
            raise
        self._children[prepared.child_run_id] = record
        self._store_terminal(record)
        return record

    def _store_terminal(self, record: ChildRunRecord) -> None:
        """把终态记录写库（有 store 时）。写库失败显式抛出——记录丢了必须被看见。"""
        if self._child_run_store is None:
            return
        state = _STORE_STATE_BY_STATUS[record.status]
        outcome = record.outcome
        self._child_run_store.mark_child_terminal(
            record.child_run_id,
            state=state,
            summary=outcome.summary,
            error_code=outcome.diagnostics[0].code if outcome.diagnostics else None,
            safe_message=(
                outcome.diagnostics[0].safe_message if outcome.diagnostics else None
            ),
            usage=outcome.usage,
        )
        self._schedule_child_wakeup(record.child_run_id)
        # 落库已经成功：内存里的终态记录可以收进保留窗口了。这是唯一挂载点——
        # 两个插入点都与它配对，且它在 store is None 时提前返回，所以"没有库"
        # 的配置天然不触发收回（那时内存是唯一真相）。
        self._reclaim_terminal_records()

    def _reclaim_terminal_records(self) -> None:
        """终态落库之后，把内存里的终态记录收到最近 `MAX_RETAINED_TERMINAL_RECORDS` 条。

        **没有 store 时一条都不丢**：那时内存就是唯一真相，丢了会让 ``query`` 对
        老的子运行报 unknown（回落那条链不存在）。有 store 时四个公开面都能回落：
        ``query`` 走 ``_outcome_from_row``，``cancel``/``steer`` 用行状态给出同样的
        ``delegation_child_not_active``，``child_workspace_id`` 由 handler 层的库
        回落兜住（见 ``runtime_handler.child_workspace_id_for`` 的注释）。

        ``_children`` 保插入序，但两个插入点都在 ``await self._kernel.run(...)``
        **之后**：异步派发（``start_child`` 并发起跑）时它实际是**完成序**，不是派发序。
        所以从头丢是"先丢最久完成的那条"——对"只留最近"的语义来说更合理。非终态
        记录一律不丢——构造上不该出现在这里，真出现了说明多了一个插入点。
        """
        if self._child_run_store is None:
            return
        excess = len(self._children) - self.MAX_RETAINED_TERMINAL_RECORDS
        if excess <= 0:
            return
        for child_run_id, record in tuple(self._children.items()):
            if excess <= 0:
                return
            if record.status not in _TERMINAL_CHILD_STATUSES:
                continue
            self._children.pop(child_run_id, None)
            excess -= 1

    def _schedule_child_wakeup(self, child_run_id: str) -> None:
        """终态**已落库**之后，尽力让父会话知道子运行结束了。

        回流失败绝不能影响终态落库，也不能把后台任务打挂：`schedule` 是同步、
        非阻塞、尽力而为的，所以这里把**任何**失败（服务缺失、主管已关闭、服务
        内部炸掉）都吞掉并记日志。真丢了回流也不是终态丢失——未汇报的行会在
        下一轮运行的 `<delegated-children>` 注入或下一次终态窗口里补上。
        """
        service = self._child_wakeup
        if service is None or not callable(getattr(service, "schedule", None)):
            return
        try:
            service.schedule(child_run_id, self._parent_conversation_id)
        except Exception:  # noqa: BLE001 - 回流是"尽力"，落库已经成功
            logger.exception(
                "通知父会话的唤醒服务失败（子运行终态已落库）",
                extra={"child_run_id": child_run_id},
            )

    def child_workspace_id(self, child_run_id: str) -> Optional[str]:
        """Isolated scratch workspace id for a completed child, if any (P3)."""
        for record in self._children.values():
            if getattr(record, "child_run_id", None) != child_run_id:
                continue
            spec = getattr(record, "spec", None)
            if spec is None:
                continue
            if spec.workspace_mode is not WorkspaceMode.ISOLATED_SNAPSHOT:
                raise AgentPlatformError(
                    "delegation_child_not_isolated",
                    "该子代理不是隔离写模式，无 scratch workspace。",
                    retryable=False,
                )
            status_value = getattr(record.status, "value", record.status)
            if status_value != "completed":
                raise AgentPlatformError(
                    "delegation_child_not_completed",
                    "隔离写子代理未完成，暂不能产出 patch。",
                    retryable=False,
                )
            return spec.child_workspace_id
        raise AgentPlatformError(
            "delegation_unknown_child",
            "No child run is tracked under this id",
            retryable=False,
            details={"child_run_id": child_run_id},
        )

    # -- 归属判定 ----------------------------------------------------------

    def _owns_row(self, row: ChildRunRow) -> bool:
        """本协调器是否有权看到/操作这条子运行记录。

        边界是**会话**而不是运行：一次用户消息就是一个运行，而子运行的生命周期
        刻意比父运行长；若按运行限定，下一轮的 query/cancel 就够不到它还活着的
        子运行（里程碑要的正是"跨轮次仍能管住子运行"）。同一会话内的任何运行都
        可以；跨会话一律当作不存在。两边会话 id 有一个为空时，退回运行 id 比较
        （兼容未注入会话的构造路径）。
        """
        if self._parent_conversation_id and row.parent_conversation_id:
            return row.parent_conversation_id == self._parent_conversation_id
        return row.parent_run_id == self._parent_run_id

    def covers_conversation(self, conversation_id: Optional[str]) -> bool:
        """本协调器是否代答这个会话：未指定会话、或本协调器没有会话信息时一律
        可以（保持旧的无范围行为）；两边都有会话且不同则不可以。边界是会话而不是
        运行——与 :meth:`_owns_row` 的判定保持一致。"""
        if not conversation_id or not self._parent_conversation_id:
            return True
        return conversation_id == self._parent_conversation_id

    async def query(self, child_run_id: str) -> ChildOutcome:
        record = self._children.get(child_run_id)
        if record is not None:
            return record.outcome
        if self._child_run_store is not None:
            row = self._child_run_store.get_child_run(child_run_id)
            # 库在多个会话之间共享：不是本会话的子运行等于不存在（见 _owns_row）。
            if row is not None and self._owns_row(row):
                if row.state is ChildRunState.RUNNING:
                    raise AgentPlatformError(
                        "delegation_child_running",
                        "子运行仍在进行中，稍后再查询。",
                        retryable=False,
                        details={"child_run_id": child_run_id},
                    )
                return _outcome_from_row(row)
        raise AgentPlatformError(
            "delegation_unknown_child",
            "No child run is tracked under this id",
            retryable=False,
            details={"child_run_id": child_run_id},
        )

    async def cancel(self, child_run_id: str, reason: str) -> None:
        """取消一个**运行中**的子运行（终态子运行仍然拒绝）。"""
        record = self._children.get(child_run_id)
        if record is not None:
            raise AgentPlatformError(
                "delegation_child_not_active",
                "Child run is already terminal and cannot be cancelled",
                retryable=False,
                details={
                    "child_run_id": child_run_id,
                    "status": record.status.value,
                },
            )
        row = None
        if self._child_run_store is not None:
            row = self._child_run_store.get_child_run(child_run_id)
            # 别的会话的子运行不能借本协调器取消：等于库里没有这一行（见 _owns_row）。
            if row is not None and not self._owns_row(row):
                row = None
            if row is not None and row.terminal:
                raise AgentPlatformError(
                    "delegation_child_not_active",
                    "Child run is already terminal and cannot be cancelled",
                    retryable=False,
                    details={"child_run_id": child_run_id, "status": row.state.value},
                )
        if row is None:
            raise AgentPlatformError(
                "delegation_unknown_child",
                "No child run is tracked under this id",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        try:
            await self._kernel.cancel(child_run_id, reason)
        except AgentPlatformError as error:
            # 内核说"没有这个活动运行"：可能是因为并发闸门把子运行挡在门外——
            # 它的库行是 running，但内核里根本没有它的活动运行；也可能只是刚好
            # 跑完了。先看我们手里的后台任务。
            if error.code == "invalid_agent_kernel_value":
                task = self._child_tasks.get(child_run_id)
                if task is not None and not task.done():
                    # 闸门外排队中：取消后台任务本身就是成功的取消。等它收尾，
                    # 终态行在 cancel 返回前一定写好。
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    latest = (
                        self._child_run_store.get_child_run(child_run_id)
                        if self._child_run_store is not None
                        else None
                    )
                    if latest is not None and latest.terminal:
                        if latest.state is ChildRunState.CANCELLED:
                            return
                        # 子运行在我们取消它之前就已经跑完了：这不是一次取消。
                        raise AgentPlatformError(
                            "delegation_child_not_active",
                            "Child run is already terminal and cannot be cancelled",
                            retryable=False,
                            details={
                                "child_run_id": child_run_id,
                                "status": latest.state.value,
                            },
                        )
                    # 任务在"第一个事件循环步之前"就被取消时，_run_reserved 的处理器
                    # 不会执行，库行会停在 running；这里补一次幂等收尾。
                    self._mark_child_cancelled(child_run_id)
                    return
                # 没有活的后台任务：以库为准复核一次。
                if self._child_run_store is not None:
                    latest = self._child_run_store.get_child_run(child_run_id)
                    if latest is not None and latest.terminal:
                        raise AgentPlatformError(
                            "delegation_child_not_active",
                            "Child run is already terminal and cannot be cancelled",
                            retryable=False,
                            details={
                                "child_run_id": child_run_id,
                                "status": latest.state.value,
                            },
                        ) from error
            raise

    # -- 纠偏（模型面） ----------------------------------------------------

    async def steer(self, child_run_id: str, message: str) -> None:
        """给一个**运行中**的子运行送一句纠偏（终态/排队中一律拒绝）。

        分支与 :meth:`cancel` 同形状，但"排队中"必须单独判：库行是 running 而
        `started_at IS NULL` 表示它还在并发闸门外，内核里根本没有它的活动运行
        ——这时 steer 会静默丢失，所以报 ``delegation_child_not_started`` 而不是
        谎报成功（`started_at` 是"真在跑"的唯一依据）。
        """
        record = self._children.get(child_run_id)
        if record is not None:
            raise AgentPlatformError(
                "delegation_child_not_active",
                "子运行已结束，无法再纠偏。",
                retryable=False,
                details={
                    "child_run_id": child_run_id,
                    "status": record.status.value,
                },
            )
        row = None
        if self._child_run_store is not None:
            row = self._child_run_store.get_child_run(child_run_id)
            # 别的会话的子运行不能借本协调器纠偏：等于库里没有这一行（见 _owns_row）。
            if row is not None and not self._owns_row(row):
                row = None
            if row is not None and row.terminal:
                raise AgentPlatformError(
                    "delegation_child_not_active",
                    "子运行已结束，无法再纠偏。",
                    retryable=False,
                    details={"child_run_id": child_run_id, "status": row.state.value},
                )
        if row is None:
            raise AgentPlatformError(
                "delegation_unknown_child",
                "No child run is tracked under this id",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        if row.started_at is None:
            raise AgentPlatformError(
                "delegation_child_not_started",
                "子运行还在排队（尚未进入内核），不能纠偏；等它开始后再试。",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        try:
            await self._kernel.steer(
                child_run_id,
                AgentMessage(role=AgentMessageRole.USER, content=message),
            )
        except AgentPlatformError as error:
            # 内核说"没有这个活动运行"：可能刚好跑完了（复核库行 → not_active），
            # 也可能真的还在跑（保留原始错误——那不是终态，不该被翻译成良性回答）。
            if (
                error.code == "invalid_agent_kernel_value"
                and self._child_run_store is not None
            ):
                latest = self._child_run_store.get_child_run(child_run_id)
                if latest is not None and latest.terminal:
                    raise AgentPlatformError(
                        "delegation_child_not_active",
                        "子运行已结束，无法再纠偏。",
                        retryable=False,
                        details={
                            "child_run_id": child_run_id,
                            "status": latest.state.value,
                        },
                    ) from error
            raise

    # -- record helpers ----------------------------------------------------

    def _resolve_profile_capabilities(
        self,
        spec: SpawnSpec,
    ) -> Optional[frozenset[str]]:
        """Resolve the declared profile into its capability layer.

        Without a resolver the profile layer is left as ``None`` and
        :func:`compute_child_capabilities` falls back to the requested set
        (M4A DR-0 behavior). With a resolver, the profile's granted
        capabilities become an explicit intersection layer, so a child can
        never receive capabilities the resolved profile does not grant.
        """
        if self._profile_resolver is None:
            return None
        profile = self._profile_resolver.resolve(spec.capability_profile)
        capabilities = getattr(profile, "capabilities", None)
        if not isinstance(capabilities, frozenset):
            raise AgentPlatformError(
                "invalid_delegation_value",
                "Profile resolver returned a profile without a capabilities set",
            )
        return capabilities

    def _build_run_command(self, spec: SpawnSpec, child_run_id: str) -> RunCommand:
        namespace = f"delegation_{child_run_id}"
        message = AgentMessage(
            role=AgentMessageRole.USER,
            content=_assemble_child_prompt(spec),
            metadata={
                "delegation": {
                    "parent_run_id": spec.parent_run_id,
                    "parent_tool_call_id": spec.parent_tool_call_id,
                    "child_run_id": child_run_id,
                }
            },
        )
        trace = TraceContext(
            trace_id=self._parent_trace.trace_id,
            run_id=child_run_id,
            correlation_id=self._parent_trace.correlation_id,
            parent_span_id=self._parent_trace.span_id,
            child_run_id=child_run_id,
        )
        return RunCommand(
            run_id=child_run_id,
            conversation_id=f"conv_{namespace}",
            lane_id=f"lane_{namespace}",
            trigger_entry_id=f"entry_{namespace}",
            message=message,
            trace=trace,
            capability_profile=spec.capability_profile,
            deadline_monotonic=time.monotonic() + spec.timeout_seconds,
            metadata={
                "delegation": {
                    "parent_run_id": spec.parent_run_id,
                    "parent_tool_call_id": spec.parent_tool_call_id,
                    "expected_output": spec.expected_output.name,
                },
                **(
                    {"child_workspace_id": spec.child_workspace_id}
                    if spec.child_workspace_id
                    else {}
                ),
            },
        )

    def _record_for_outcome(
        self,
        *,
        child_run_id: str,
        spec: SpawnSpec,
        decision: ChildCapabilityDecision,
        depth: int,
        outcome: RunOutcome,
    ) -> ChildRunRecord:
        if outcome.run_id != child_run_id:
            raise AgentPlatformError(
                "delegation_child_run_mismatch",
                "Kernel returned an outcome for a different run",
                retryable=False,
                details={
                    "expected_child_run_id": child_run_id,
                    "kernel_run_id": outcome.run_id,
                },
            )
        projected = project_child_outcome(
            outcome,
            policy=self._result_policy,
            usage=_usage_summary(outcome.usage),
        )
        return ChildRunRecord(
            child_run_id=child_run_id,
            parent_run_id=spec.parent_run_id,
            parent_tool_call_id=spec.parent_tool_call_id,
            depth=depth,
            spec=spec,
            decision=decision,
            status=ChildRunStatus(projected.status.value),
            outcome=projected,
        )

    def _failed_record(
        self,
        *,
        child_run_id: str,
        spec: SpawnSpec,
        decision: ChildCapabilityDecision,
        depth: int,
        error: Exception,
        usage: Optional[UsageSummary] = None,
    ) -> ChildRunRecord:
        retryable = error.retryable if isinstance(error, AgentPlatformError) else False
        code = error.code if isinstance(error, AgentPlatformError) else "child_kernel_error"
        message = (
            error.safe_message
            if isinstance(error, AgentPlatformError)
            else "Child kernel run raised an unexpected error."
        )
        return ChildRunRecord(
            child_run_id=child_run_id,
            parent_run_id=spec.parent_run_id,
            parent_tool_call_id=spec.parent_tool_call_id,
            depth=depth,
            spec=spec,
            decision=decision,
            status=ChildRunStatus.FAILED,
            outcome=ChildOutcome(
                child_run_id=child_run_id,
                status=ChildStatus.FAILED,
                summary="Child run failed before producing a result.",
                diagnostics=(
                    SafeDiagnostic(
                        code=code,
                        safe_message=message,
                        retryable=retryable,
                    ),
                ),
                # 内核失败不等于没花过 token：调用方给得出快照就必须落进库行，
                # 拿不到才落空用量（四列 0 是"没花过"的诚实表述）。
                usage=usage if usage is not None else UsageSummary(),
            ),
        )

    # -- introspection -----------------------------------------------------

    @property
    def children(self) -> tuple[ChildRunRecord, ...]:
        return tuple(sorted(self._children.values(), key=lambda item: item.child_run_id))

    @property
    def tracked_child_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._children))


#: 存储状态 → 协议状态（wire 词汇里没有 interrupted，按失败上报）。
_CHILD_STATUS_BY_STORE_STATE = {
    ChildRunState.COMPLETED: ChildStatus.COMPLETED,
    ChildRunState.FAILED: ChildStatus.FAILED,
    ChildRunState.CANCELLED: ChildStatus.CANCELLED,
    ChildRunState.INTERRUPTED: ChildStatus.FAILED,
}


def _outcome_from_row(row: ChildRunRow) -> ChildOutcome:
    """从库里的终态行重建有界结果（进程重启后查询走这里）。"""
    diagnostics: tuple[SafeDiagnostic, ...] = ()
    if row.error_code or row.safe_message:
        diagnostics = (
            SafeDiagnostic(
                code=row.error_code or "delegation_child_failed",
                safe_message=row.safe_message or "子运行未能完成。",
            ),
        )
    return ChildOutcome(
        child_run_id=row.child_run_id,
        status=_CHILD_STATUS_BY_STORE_STATE[row.state],
        summary=row.summary or "",
        usage=UsageSummary(
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            model_turns=row.model_turns,
            tool_calls=row.tool_calls,
        ),
        diagnostics=diagnostics,
    )


def _usage_summary(usage: Optional[RunUsage]) -> UsageSummary:
    """把协议包的**中性**用量映射成委派层的 ``UsageSummary``。

    中性类型存在的意义就是这个映射留在委派层：``agent_kernel`` 不能反过来
    import 委派层。内核没带用量（``None``）时保持空用量，既有调用点行为不变。
    """
    if usage is None:
        return UsageSummary()
    return UsageSummary(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model_turns=usage.model_turns,
        tool_calls=usage.tool_calls,
    )


def _child_run_id_for(key: tuple[str, str]) -> str:
    """Deterministic child run id derived from the parent spawn key.

    Parent tool call ids already identify the spawn attempt, so the child
    run id is stable across retries of the same parent tool call without
    colliding across parents.
    """
    digest = hashlib.sha256(f"{key[0]}:{key[1]}".encode("utf-8")).hexdigest()[:24]
    return f"child_{digest}"


def _assemble_child_prompt(spec: SpawnSpec) -> str:
    """Child user prompt: task + explicit excerpts only (06 §5.3)."""
    parts = [spec.task.strip()]
    if spec.context_policy.excerpts:
        blocks = []
        for index, excerpt in enumerate(spec.context_policy.excerpts, start=1):
            blocks.append(f"[excerpt {index}]\n{excerpt}")
        parts.append("Provided excerpts:\n\n" + "\n\n".join(blocks))
    return "\n\n".join(parts)


__all__ = [
    "MAX_DEPTH",
    "MAX_LISTED_CHILDREN",
    "READ_ONLY_WORKSPACE_MODES",
    "ChildRunRecord",
    "ChildRunStatus",
    "InProcessChildCoordinator",
    "_assemble_child_prompt",
]
