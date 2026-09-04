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

Per-parent coordinators are cached in-process so query/cancel across tool
calls resolve the same coordinator the spawn used. The spawn idempotency
key is the parent tool call id (06 §8.6): a retried spawn for the same
call id is refused instead of double-executed.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional

from endless_task.agent_kernel import AgentKernel
from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic
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
)
from .coordinator import InProcessChildCoordinator
from .limits import READ_ONLY_PROFILE_NAMES
from .protocol import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildStatus,
    SpawnSpec,
    WorkspaceMode,
)

KernelProvider = Callable[[ToolExecutionRequest], AgentKernel]
CapabilityProvider = Callable[[ToolExecutionRequest], frozenset[str]]

#: Capabilities a read-only research child requests by default (subset of
#: the builtin subagent_readonly profile; the parent must hold them all).
_READ_ONLY_CHILD_CAPABILITIES = frozenset(
    {"workspace.read", "session.query", "memory.read"}
)


class CoordinatorDelegationHandler:
    """DelegationToolHandler backed by per-parent in-process coordinators."""

    def __init__(
        self,
        *,
        kernel_provider: KernelProvider,
        capability_provider: CapabilityProvider,
        profile_resolver: Optional[object] = None,
        default_timeout_seconds: float = 120.0,
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
        self._coordinators: dict[str, InProcessChildCoordinator] = {}

    # -- DelegationToolHandler ---------------------------------------------

    async def spawn_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        try:
            coordinator = self._coordinator_for(request)
        except AgentPlatformError as error:
            return _failed(request, error)
        task = str(request.arguments.get("task", "")).strip()
        timeout = request.arguments.get("timeoutSeconds")
        if timeout is None:
            timeout = self._default_timeout_seconds
        schema_arg = request.arguments.get("expectedOutputSchema")
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
            workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
            timeout_seconds=float(timeout),
            parent_run_id=request.run_id,
            parent_tool_call_id=request.call_id,
        )
        try:
            child_run_id = await coordinator.spawn(spec)
            outcome = await coordinator.query(child_run_id)
        except AgentPlatformError as error:
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=f"子代理已创建：{child_run_id}",
            structured_content=_outcome_json(outcome),
        )

    async def query_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        try:
            coordinator = self._coordinator_for(request)
            child_run_id = str(request.arguments.get("childRunId", "")).strip()
            outcome = await coordinator.query(child_run_id)
        except AgentPlatformError as error:
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content=outcome.summary[:20_000],
            structured_content=_outcome_json(outcome),
        )

    async def cancel_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        try:
            coordinator = self._coordinator_for(request)
            child_run_id = str(request.arguments.get("childRunId", "")).strip()
            reason = request.arguments.get("reason")
            reason_text = str(reason) if reason is not None else "用户取消"
            await coordinator.cancel(child_run_id, reason_text)
        except AgentPlatformError as error:
            if error.code == "delegation_child_not_active":
                # Terminal children need no cancellation; report it as a
                # benign no-op rather than an error.
                return ToolOutcome(
                    call_id=request.call_id,
                    status=ToolOutcomeStatus.COMPLETED,
                    content="子代理已结束，无需取消。",
                    structured_content={
                        "childRunId": str(
                            request.arguments.get("childRunId", "")
                        ).strip(),
                        "status": "already_terminal",
                    },
                )
            return _failed(request, error)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="取消请求已发送。",
            structured_content={
                "childRunId": str(request.arguments.get("childRunId", "")).strip(),
                "status": "cancelling",
            },
        )

    # -- coordinator lifecycle ---------------------------------------------

    def _coordinator_for(self, request: ToolExecutionRequest) -> InProcessChildCoordinator:
        existing = self._coordinators.get(request.run_id)
        if existing is not None:
            return existing
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
            parent_capabilities=capabilities,
            parent_trace=_trace_for(request),
            profile_resolver=self._profile_resolver,
        )
        self._coordinators[request.run_id] = coordinator
        return coordinator

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


def _outcome_json(outcome) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "childRunId": outcome.child_run_id,
        "status": outcome.status.value,
        "summary": outcome.summary,
        "diagnostics": [
            {"code": item.code, "message": item.safe_message}
            for item in outcome.diagnostics
        ],
    }
    if outcome.confidence is not None:
        payload["confidence"] = outcome.confidence
    return payload


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
]
