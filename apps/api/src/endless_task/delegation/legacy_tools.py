"""v1-form delegation adapter registered into the production ToolRegistry (DR-2).

The runtime-v2 execution path resolves every model tool as a v1
:class:`RegisteredTool` (``call.response_variant_id`` carries the run id,
same mechanism as ``update_plan``). 切片 9 第四批（Task 3）起，本模块只剩
「v1 形状的最后一层适配」：生产注册的对象是文件末尾的 shim
(:class:`V2DelegationToolAsRegistered`)，它的内层是 :mod:`.agent_tools` 里的
**原生 v2 对象**，被执行的就是那个对象；shim 复用本模块既有的
``ToolCall → ToolExecutionRequest`` 映射与 ``ToolOutcome → ToolResult`` 投影。

五个 v1 ``*LegacyTool`` 类（v1 定义的第二份字面副本）**已退役**：定义/校验/
行为的唯一源是 :mod:`.agent_tools`（``spawn/query/cancel/list/steer_agent``），
准入元数据（``execution_mode`` / ``idempotency`` / ``required_capabilities``）的
唯一源是按名索引的审计表 ``tool_platform/legacy_policy.py``。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

from endless_task.agent_platform import AgentPlatformError, SafeDiagnostic, plain_json
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tool_platform import (
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
)
from endless_task.tool_platform.protocol import (
    AgentToolV2,
    ApprovalPolicy,
    ToolDefinitionV2,
    ToolEffect as V2ToolEffect,
)
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolCall,
    ToolCallError,
    ToolDefinition,
    ToolEffect,
    ToolResult,
    ToolValidationError,
)

from .agent_tools import DelegationToolHandler

CorrelationResolver = Callable[[ToolCall], str]


def _default_correlation(call: ToolCall) -> str:
    # v2 run execution sets response_variant_id to the run id (same
    # convention update_plan relies on); without a separate correlation id
    # the run id is the stable correlation anchor.
    return call.response_variant_id


class _LegacyDelegationTool(RegisteredTool):
    """Shared v1->v2 bridge for the delegation operations.

    切片 9 第四批（Task 3）退役五个 v1 子类之后，它**只作为 shim
    (:class:`V2DelegationToolAsRegistered`) 的基类存在**：提供
    ``ToolCall → ToolExecutionRequest`` 的字段映射、``_dispatch`` 钩子与
    ``ToolOutcome → ToolResult`` 的投影。生产路径不再直接实例化它。
    """

    handler: DelegationToolHandler
    _definition: ToolDefinition
    _correlation_resolver: CorrelationResolver

    async def execute(
        self,
        call: ToolCall,
        cancellation_token: CancellationToken,
    ) -> ToolResult:
        if call.tool_name != self._definition.name:
            raise ToolValidationError(
                "tool_name_mismatch",
                f"Tool call name {call.tool_name!r} does not match {self._definition.name!r}",
            )
        request = ToolExecutionRequest(
            call_id=call.id,
            tool_name=call.tool_name,
            arguments=dict(call.arguments),
            conversation_id=call.conversation_id,
            run_id=call.response_variant_id,
            model_turn_id=call.turn_id,
            correlation_id=self._correlation_resolver(call),
            cancellation=cancellation_token,
            created_at=call.created_at,
        )
        try:
            outcome = await self._dispatch(request)
        except Exception as error:
            return _result_from_outcome(
                _tool_failure_outcome(call.id, error),
                call.id,
            )
        return _result_from_outcome(outcome, call.id)

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        raise NotImplementedError


def _tool_failure_outcome(call_id: str, error: Exception) -> ToolOutcome:
    if isinstance(error, AgentPlatformError):
        code = error.code
        message = error.safe_message
    else:
        code = "delegation_tool_error"
        message = "子代理工具执行出错。"
    return ToolOutcome(
        call_id=call_id,
        status=ToolOutcomeStatus.FAILED,
        content=message,
        diagnostic=SafeDiagnostic(code=code, safe_message=message),
    )


def _result_from_outcome(outcome: ToolOutcome, call_id: str) -> ToolResult:
    if outcome.status is ToolOutcomeStatus.COMPLETED:
        return ToolResult(
            tool_call_id=call_id,
            content=outcome.content,
            structured_content=outcome.structured_content,
            is_truncated=outcome.is_truncated,
        )
    code = outcome.diagnostic.code if outcome.diagnostic is not None else "delegation_failed"
    message = (
        outcome.diagnostic.safe_message
        if outcome.diagnostic is not None
        else outcome.content
    )
    error = ToolCallError(
        code=code,
        safe_message=message,
        retryable=False,
    )
    return ToolResult(
        tool_call_id=call_id,
        content=message,
        error=error,
    )


def _v1_effect_from_v2(effect: V2ToolEffect) -> ToolEffect:
    """v2 影响面 → v1 影响面（显式 ``is`` 分支）。

    两个 ``ToolEffect`` 是**不同的枚举类**（成员值恰好同名），所以不能靠
    ``==``/字典查找——``is`` 才是身份判断。v1 词表装不下 ``PROCESS``/
    ``NETWORK``：大声失败，不静默降级成别的影响面（审批文案与影响面判定
    都读 v1 枚举，猜错就是静默给模型/用户一句假话）。
    """
    if effect is V2ToolEffect.READ_ONLY:
        return ToolEffect.READ_ONLY
    if effect is V2ToolEffect.LOCAL_WRITE:
        return ToolEffect.LOCAL_WRITE
    if effect is V2ToolEffect.EXTERNAL_ACTION:
        return ToolEffect.EXTERNAL_ACTION
    raise AgentPlatformError(
        "invalid_delegation_tool_effect",
        "v2 委派工具的影响面不在 v1 词表内，无法投影成 v1 定义。",
    )


def _v1_approval_from_v2(approval: ApprovalPolicy) -> ToolApprovalMode:
    """v2 审批策略 → v1 审批模式（显式 ``is`` 分支）。"""
    if approval is ApprovalPolicy.AUTO:
        return ToolApprovalMode.AUTO
    if approval is ApprovalPolicy.REQUIRED:
        return ToolApprovalMode.REQUIRED
    # ``ASK`` / ``FORBIDDEN_UNATTENDED`` 在 v1 里没有对应语义；宁可装配期
    # 大声失败，也不要把「需要审批」静默投影成 AUTO（那会让审批门消失）。
    raise AgentPlatformError(
        "invalid_delegation_tool_approval",
        "v2 委派工具的审批策略不在 v1 词表内，无法投影成 v1 定义。",
    )


def _v1_definition_from_v2(definition: ToolDefinitionV2) -> ToolDefinition:
    """把 v2 定义投影成 v1 形状（含 **v1** 的枚举）。

    v1 侧的全部消费者都读这份形状：审批闸门拿 ``effect is ToolEffect.LOCAL_WRITE``
    与本模块的 **v1** 枚举比、``api/wiring.py`` 的两个过滤器读
    ``definition.approval_mode``、``runner._side_effect_evidence`` 按名从 v1
    注册表读 effect、catalog 装配要 ``LegacyToolAdapter(工具)``。
    """
    return ToolDefinition(
        name=definition.name,
        description=definition.description,
        input_schema=plain_json(definition.input_schema),
        effect=_v1_effect_from_v2(definition.effect),
        approval_mode=_v1_approval_from_v2(definition.approval),
        timeout_seconds=definition.timeout_seconds,
        max_output_characters=definition.max_output_characters,
    )


class V2DelegationToolAsRegistered(_LegacyDelegationTool):
    """把原生 v2 委派工具暴露成 v1 执行契约（``RegisteredTool``）的薄 shim。

    **为什么需要它**：执行期只认 v1 注册表里的 ``RegisteredTool``
    （``definition`` + ``execute(call, cancellation_token)``），而 v2 家族是另一种
    形状（``definition: ToolDefinitionV2`` + ``execute(request: ToolExecutionRequest)
    -> ToolOutcome``）。本 shim 是**唯一的桥**：定义投影成 v1 形状，执行时复用
    :meth:`_LegacyDelegationTool.execute` 既有的
    ``ToolCall → ToolExecutionRequest`` 映射，然后调用**内层原生 v2 对象**的
    ``execute(request)``，再把 ``ToolOutcome`` 按既有 ``_result_from_outcome``
    投回 ``ToolResult``。这样 v2 类才是生产里真正被执行的那个对象，而
    ``ToolExecutionCoordinator`` 一行都不用动（零冻结面、零快照重生成）。

    **准入元数据不由本 shim 提供**：``execution_mode`` / ``idempotency`` /
    ``required_capabilities`` 装不进 v1 ``ToolDefinition``，它们仍然、且只由
    按工具名索引的审计策略表 ``tool_platform/legacy_policy.py`` 提供
    （``LegacyToolAdapter`` 在表面装配与调度准入时从那张表取）。v2 定义里
    的同名字段只被奇偶守卫比对，不是准入的真源。

    **``correlation_id`` 逐字复现今天的 v1 派生**：``_default_correlation`` 取
    ``call.response_variant_id``（v2 运行把 run id 放在那里）；本批不改这条，
    两条路径的差异登记在下一批。

    **``requires_explicit_confirmation``**：显式返回 ``False``，与今天完全一致
    ——今天五个 v1 类从 ``RegisteredTool`` 协议继承到同名方法，返回的就是
    ``False``（实测 ``getattr(tool, "requires_explicit_confirmation")`` 是可调用的
    绑定方法且返回 ``False``）。所以**强制确认不经此门**：委派五件的参数级恒确认
    从来不是靠这个方法，本 shim 不改变这一点。
    """

    _v2_tool: AgentToolV2

    def __init__(
        self,
        tool: AgentToolV2,
        *,
        correlation_resolver: Optional[CorrelationResolver] = None,
    ) -> None:
        definition = getattr(tool, "definition", None)
        if not isinstance(definition, ToolDefinitionV2) or not callable(
            getattr(tool, "execute", None)
        ):
            # 装错对象（例如又把 v1 类塞进来）必须在装配期就大声失败：
            # 这正是本批要防的「被执行的不是 v2」。
            raise AgentPlatformError(
                "invalid_delegation_tool",
                "v2 委派工具必须提供 ToolDefinitionV2 定义与 execute(request) 契约。",
            )
        self._v2_tool = tool
        self._correlation_resolver = correlation_resolver or _default_correlation
        self._definition = _v1_definition_from_v2(definition)

    @property
    def v2_tool(self) -> AgentToolV2:
        """内层原生 v2 对象；执行路径只经 :meth:`_dispatch`，这里供守卫/测试断言。"""
        return self._v2_tool

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def _dispatch(self, request: ToolExecutionRequest) -> ToolOutcome:
        return await self._v2_tool.execute(request)

    def requires_explicit_confirmation(self, call: ToolCall) -> bool:
        """与切换前一致：委派五件的强制确认不经此门（见类 docstring）。"""
        return False


__all__ = [
    "V2DelegationToolAsRegistered",
]
