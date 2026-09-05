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
- the run executes synchronously through the injected AgentKernel and
  the result is projected by :mod:`.result_projection`; queued/running
  state transitions, concurrency, timeout and cancel propagation belong
  to DR-2 (scheduler) and are deliberately not invented here.

The coordinator is *Not Integrated*: no feature flag, no composition
root wiring, no ledger persistence. It proves the DR-0 acceptance gates
(capability 提升禁止 + 结果有界/schema 受控) against a real AgentKernel
instance (FakeAgentKernel in tests, the production kernel later).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from endless_task.agent_kernel import (
    AgentKernel,
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
)
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
from endless_task.runtime_ledger import TraceContext

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

#: Workspace modes M4A (read-only delegation) may execute.
READ_ONLY_WORKSPACE_MODES = frozenset(
    {
        WorkspaceMode.NONE,
        WorkspaceMode.READ_ONLY_SHARED,
    }
)

#: Maximum delegation depth for the DR-0 coordinator (root = depth 0).
MAX_DEPTH = 1


class ChildRunStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
    """Synchronous single-child coordinator over one AgentKernel.

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
        self._kernel = kernel
        self._parent_run_id = parent_run_id
        self._parent_capabilities = parent_capabilities
        self._parent_trace = parent_trace
        self._parent_depth = parent_depth
        self._result_policy = result_policy if result_policy is not None else ChildResultPolicy()
        self._allowed_profile_names = allowed
        self._profile_resolver = profile_resolver
        self._isolated_write_enabled = bool(isolated_write_enabled)
        self._children: dict[str, ChildRunRecord] = {}
        self._spawn_keys: dict[tuple[str, str], str] = {}

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
        existing = self._spawn_keys.get(key)
        if existing is not None:
            raise AgentPlatformError(
                "delegation_duplicate_spawn",
                "A child for this parent tool call was already spawned",
                retryable=False,
                details={"child_run_id": existing},
            )
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
        spec = prepared.spec
        key = (spec.parent_run_id, spec.parent_tool_call_id)
        existing = self._spawn_keys.get(key)
        if existing is not None:
            raise AgentPlatformError(
                "delegation_duplicate_spawn",
                "A child for this parent tool call was already spawned",
                retryable=False,
                details={"child_run_id": existing},
            )
        self._spawn_keys[key] = prepared.child_run_id
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
            )
            self._children[prepared.child_run_id] = record
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
            # A kernel answering for the wrong run is a contract violation;
            # release the spawn key so a corrected retry is not mislabelled
            # as a duplicate.
            self._spawn_keys.pop(key, None)
            raise
        self._children[prepared.child_run_id] = record
        return record

    async def query(self, child_run_id: str) -> ChildOutcome:
        record = self._children.get(child_run_id)
        if record is None:
            raise AgentPlatformError(
                "delegation_unknown_child",
                "No child run is tracked under this id",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        return record.outcome

    async def cancel(self, child_run_id: str, reason: str) -> None:
        record = self._children.get(child_run_id)
        if record is None:
            raise AgentPlatformError(
                "delegation_unknown_child",
                "No child run is tracked under this id",
                retryable=False,
                details={"child_run_id": child_run_id},
            )
        # DR-0 executes children synchronously, so a returned record is
        # already terminal; active cancellation belongs to DR-2.
        raise AgentPlatformError(
            "delegation_child_not_active",
            "Child run is already terminal and cannot be cancelled",
            retryable=False,
            details={
                "child_run_id": child_run_id,
                "status": record.status.value,
            },
        )

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
                }
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
            usage=UsageSummary(),
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
            ),
        )

    # -- introspection -----------------------------------------------------

    @property
    def children(self) -> tuple[ChildRunRecord, ...]:
        return tuple(sorted(self._children.values(), key=lambda item: item.child_run_id))

    @property
    def tracked_child_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._children))


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
    "READ_ONLY_WORKSPACE_MODES",
    "ChildRunRecord",
    "ChildRunStatus",
    "InProcessChildCoordinator",
    "_assemble_child_prompt",
]
