"""Model-callable delegation tools: spawn/query/cancel child agents (DR-2).

DR-2 registers ``spawn_agent / query_agent / cancel_agent`` so the main
agent can create and manage read-only child runs; slice 9 M2 adds
``list_agents / steer_agent`` (enumerate this conversation's child runs /
steer a running one). Following the AP-106 ``search_tools`` pattern, each
tool is an :class:`AgentToolV2` that stays declarative: it validates
arguments, delegates to an injected :class:`DelegationToolHandler`, and
projects the structured result.

The handler seam is the composition-root bridge: it owns the per-parent
coordinator/scheduler wiring (child kernel factory, capability context,
idempotency keys). Tools never construct a coordinator themselves, so
tool_platform stays free of delegation/runtime imports and the tools are
directly testable with a fake handler.

M4A read-only gate: spawn arguments carry the child task, an optional
expected-output schema and the optional ``mode`` (readonly/isolated_write);
capability/workspace policy is decided by the handler (M4A = read-only
children only), not by the tool surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    SafeDiagnostic,
    plain_json,
)
from endless_task.tool_platform.protocol import (
    AgentToolV2,
    ApprovalPolicy,
    IdempotencyPolicy,
    ToolDefinitionV2,
    ToolEffect,
    ToolExecutionMode,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)

SPAWN_AGENT_NAME = "spawn_agent"
QUERY_AGENT_NAME = "query_agent"
CANCEL_AGENT_NAME = "cancel_agent"
LIST_AGENTS_NAME = "list_agents"
STEER_AGENT_NAME = "steer_agent"

_MAX_TASK_CHARACTERS = 8_192
_MAX_REASON_CHARACTERS = 512
# 切片 10 T1 Step 3：这里原有 `_DEFAULT_TIMEOUT_SECONDS = 120.0`，但**全仓没有任何
# 引用**——工具面从不读它（`timeoutSeconds` 的 schema 描述是字面文案），真正生效的
# 默认值在 `CoordinatorDelegationHandler.__init__` 的 `default_timeout_seconds` 上。
# 留着它会让"子运行默认墙到底在哪"多一个假源头（改错地方毫无效果，正是本切片要修
# 的那类毛病），所以删掉；默认墙现在唯一由设置
# `ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS`（默认 120）经装配根注入。
_MAX_TIMEOUT_SECONDS = 86_400.0


class DelegationToolHandler(Protocol):
    """Composition-root bridge between delegation tools and the runtime.

    Implementations own the coordinator/scheduler that actually executes
    child runs, so the tools stay declarative and independently testable.
    """

    async def spawn_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    async def query_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    async def cancel_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    # 切片 9 M2：枚举本会话的子运行 / 给运行中的子运行送一句纠偏。
    # v2 形态的工具类（ListAgentsTool / SteerAgentTool）与 v1 生产面
    # （`.legacy_tools` 的同名 RegisteredTool）由奇偶守卫锁成一对。
    async def list_children(self, request: ToolExecutionRequest) -> ToolOutcome: ...

    async def steer_child(self, request: ToolExecutionRequest) -> ToolOutcome: ...


@dataclass(frozen=True)
class SpawnAgentTool(AgentToolV2):
    """Creates one read-only child agent and returns its run handle."""

    handler: DelegationToolHandler
    max_task_characters: int = _MAX_TASK_CHARACTERS
    timeout_seconds: float = 30.0
    max_output_characters: int = 20_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "spawn_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "spawn_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=SPAWN_AGENT_NAME,
                # 描述与 legacy 生产面逐字一致（奇偶守卫：任一侧改动即红）；
                # 以 v1 注册面为准——Task 3 已把模型唯一看得见的文案改成异步契约。
                description=(
                    "创建一个只读子代理去执行独立研究/检查任务，**立刻返回 child_run_id 句柄**"
                    "（子代理在后台运行，结果不在本次返回值里）；"
                    "随后用 query_agent 查询结果、用 cancel_agent 中止。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": self.max_task_characters,
                            "description": "子代理要执行的独立任务说明。",
                        },
                        "expectedOutputSchema": {
                            "type": "object",
                            "description": "子代理结构化结果的 JSON Schema（可选）。",
                        },
                        "timeoutSeconds": {
                            "type": "number",
                            "minimum": 1.0,
                            "maximum": _MAX_TIMEOUT_SECONDS,
                            "description": "子代理超时秒数，默认 120。",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["readonly", "isolated_write"],
                            "description": (
                                "readonly（默认）= 只读子代理；isolated_write = M4B "
                                "隔离写子代理（仅在隔离 scratch 工作区写，不触碰主工作区）。"
                            ),
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                # 调度准入字段以审计策略表（tool_platform/legacy_policy.py: spawn_agent）
                # 为准：生产 adapter 从表里取 EXCLUSIVE 交给 scheduler admission
                # （tool_platform/pipeline.py），v2 侧必须同名同值。
                execution_mode=ToolExecutionMode.EXCLUSIVE,
                idempotency=IdempotencyPolicy.UNKNOWN,
                required_capabilities=frozenset({"agent.delegate"}),
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        task = request.arguments.get("task")
        if not isinstance(task, str) or not task.strip():
            return _tool_failed(
                request.call_id,
                "invalid_delegation_task",
                "spawn_agent 的 task 必须是文本。",
            )
        schema = request.arguments.get("expectedOutputSchema")
        if schema is not None and not isinstance(schema, Mapping):
            return _tool_failed(
                request.call_id,
                "invalid_expected_output_schema",
                "expectedOutputSchema 必须是 JSON Schema 对象。",
            )
        timeout = request.arguments.get("timeoutSeconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 1.0 <= float(timeout) <= _MAX_TIMEOUT_SECONDS
        ):
            return _tool_failed(
                request.call_id,
                "invalid_delegation_timeout",
                "timeoutSeconds 必须在 1 到 86400 之间。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.spawn_child(request)


@dataclass(frozen=True)
class QueryAgentTool(AgentToolV2):
    """Queries one child run outcome by its handle."""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 20_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "query_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "query_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=QUERY_AGENT_NAME,
                description=(
                    "按 child_run_id 查询子代理的进展或结果：区分排队中（queued）、"
                    "已开始跑（running）与终态（completed/failed/cancelled/"
                    "interrupted），并给出已用时（elapsedSeconds）与最近活动时间"
                    "（updatedAt）；查无此子运行按失败返回。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "childRunId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": "spawn_agent 返回的子代理运行句柄。",
                        },
                    },
                    "required": ["childRunId"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.SAFE,
                required_capabilities=frozenset({"agent.delegate"}),
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        child_run_id = request.arguments.get("childRunId")
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            return _tool_failed(
                request.call_id,
                "invalid_child_run_id",
                "query_agent 的 childRunId 必须是文本。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.query_child(request)


@dataclass(frozen=True)
class CancelAgentTool(AgentToolV2):
    """Cancels one child run by its handle."""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 4_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "cancel_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "cancel_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=CANCEL_AGENT_NAME,
                description="取消一个进行中的子代理运行。",
                input_schema={
                    "type": "object",
                    "properties": {
                        "childRunId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": "要取消的子代理运行句柄。",
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_REASON_CHARACTERS,
                            "description": "取消原因（可选）。",
                        },
                    },
                    "required": ["childRunId"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                # 同 spawn_agent：审计策略表（cancel_agent）声明 EXCLUSIVE。
                execution_mode=ToolExecutionMode.EXCLUSIVE,
                idempotency=IdempotencyPolicy.UNKNOWN,
                required_capabilities=frozenset({"agent.delegate"}),
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        child_run_id = request.arguments.get("childRunId")
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            return _tool_failed(
                request.call_id,
                "invalid_child_run_id",
                "cancel_agent 的 childRunId 必须是文本。",
            )
        reason = request.arguments.get("reason")
        if reason is not None and (
            not isinstance(reason, str) or not reason.strip()
        ):
            return _tool_failed(
                request.call_id,
                "invalid_cancel_reason",
                "cancel_agent 的 reason 必须是文本。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.cancel_child(request)


@dataclass(frozen=True)
class ListAgentsTool(AgentToolV2):
    """枚举本会话派过的子代理运行（只读）。"""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 20_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "list_children", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "list_agents requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=LIST_AGENTS_NAME,
                # 描述与 legacy 生产面逐字一致（奇偶守卫）。
                description=(
                    "列出本会话派过的子代理运行（child_run_id / 状态 / 任务摘要 / "
                    "已用时 / 是否隔离写），最近创建的在前，上限 50 条；没有子运行时空列表。"
                    "子运行跨轮次可见；运行中的可以用 steer_agent 纠偏、cancel_agent 取消。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                approval=ApprovalPolicy.AUTO,
                execution_mode=ToolExecutionMode.PARALLEL,
                idempotency=IdempotencyPolicy.SAFE,
                required_capabilities=frozenset({"agent.delegate"}),
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        return await self.handler.list_children(request)


@dataclass(frozen=True)
class SteerAgentTool(AgentToolV2):
    """给运行中的子代理送一句纠偏说明。"""

    handler: DelegationToolHandler
    timeout_seconds: float = 30.0
    max_output_characters: int = 4_000

    def __post_init__(self) -> None:
        if not callable(getattr(self.handler, "steer_child", None)):
            raise AgentPlatformError(
                "invalid_delegation_handler",
                "steer_agent requires a delegation tool handler",
            )
        object.__setattr__(
            self,
            "definition",
            ToolDefinitionV2(
                name=STEER_AGENT_NAME,
                # 描述与 legacy 生产面逐字一致（奇偶守卫）。
                description=(
                    "给一个**运行中**的子代理送一句纠偏说明（只允许一句话纠偏；"
                    "要改任务/改范围请先用 cancel_agent 取消，再用 spawn_agent 重新派）。"
                    "子运行已结束则回 already_terminal；还在排队（尚未开始）或不属于本会话"
                    "会报错。"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "childRunId": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 256,
                            "description": "spawn_agent 返回的子代理运行句柄。",
                        },
                        "message": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 2_000,
                            "description": "要送进子代理的纠偏说明（一句话，不要改任务本身）。",
                        },
                    },
                    "required": ["childRunId", "message"],
                    "additionalProperties": False,
                },
                effect=ToolEffect.READ_ONLY,
                approval=ApprovalPolicy.AUTO,
                # 同 spawn_agent：审计策略表（steer_agent）声明 EXCLUSIVE；
                # brief 字面写的 PARALLEL 已被控制器裁决取代。
                execution_mode=ToolExecutionMode.EXCLUSIVE,
                idempotency=IdempotencyPolicy.UNKNOWN,
                required_capabilities=frozenset({"agent.delegate"}),
                timeout_seconds=self.timeout_seconds,
                max_output_characters=self.max_output_characters,
            ),
        )

    async def execute(self, request: ToolExecutionRequest) -> ToolOutcome:
        request.cancellation.raise_if_cancelled()
        child_run_id = request.arguments.get("childRunId")
        if not isinstance(child_run_id, str) or not child_run_id.strip():
            return _tool_failed(
                request.call_id,
                "invalid_child_run_id",
                "steer_agent 的 childRunId 必须是文本。",
            )
        message = request.arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            return _tool_failed(
                request.call_id,
                "invalid_steer_message",
                "steer_agent 的 message 必须是文本。",
            )
        request.cancellation.raise_if_cancelled()
        return await self.handler.steer_child(request)


def child_run_handle_json(
    child_run_id: str,
    status: str,
    summary: Optional[str] = None,
) -> Mapping[str, Any]:
    """Structured handle row shared by delegation tools and handlers."""
    payload: dict[str, Any] = {"childRunId": child_run_id, "status": status}
    if summary is not None:
        payload["summary"] = summary
    return payload


def _tool_failed(
    call_id: str,
    code: str,
    message: str,
) -> ToolOutcome:
    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.FAILED,
        content=message,
        diagnostic=SafeDiagnostic(code=code, safe_message=message),
    )


__all__ = [
    "CANCEL_AGENT_NAME",
    "LIST_AGENTS_NAME",
    "QUERY_AGENT_NAME",
    "SPAWN_AGENT_NAME",
    "STEER_AGENT_NAME",
    "CancelAgentTool",
    "DelegationToolHandler",
    "ListAgentsTool",
    "QueryAgentTool",
    "SpawnAgentTool",
    "SteerAgentTool",
    "child_run_handle_json",
]
