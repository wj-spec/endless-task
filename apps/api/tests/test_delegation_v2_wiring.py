"""切片 9 第四批：委派五件「被执行的是原生 v2 类」的接线级守卫。

本模块钉三件事（对应规格 §4 的 1/2 与控制器追加的非法入参矩阵）：

1. **生产注册表里按名解析到的对象，内层就是 ``delegation/agent_tools.py`` 的
   原生 v2 类**，而它对外暴露的 ``definition`` 是 **v1 形状**（含 **v1** 的
   ``ToolEffect`` / ``ToolApprovalMode`` 枚举），并且模型面逐字段等于本文件内联的
   **切换前字面快照**（``PRE_SWITCH_SURFACE``）——v1 的五个 ``*LegacyTool`` 类已在
   Task 3 退役，快照是它们的替代基准（留用旧类等于"等于被删掉的东西"）。
2. **真 HTTP → 真 runtime → 真注册表**跑一次调用，原生 v2 类的 ``execute``
   真的被执行（对类打间谍并断言计数），而不是「注册表里名字还在」。
3. **非法入参矩阵（22 行）在生产路径上每一行都被拒，且逐行三元组等于冻结表**
   （``EXPECTED_FAILURES``）。v2 ``execute`` 里的前置校验成分必须说准：**只有**
   ``invalid_expected_output_schema`` / ``invalid_delegation_timeout`` 这两条在
   生产路径上**不可达**——v2 管线的第一步（协调器 ``_validate_arguments``，读
   ``tool.definition.input_schema``，即由 shim 从 v2 定义投影出来的那份 schema）
   先把这两类入参挡成 ``invalid_tool_arguments``。测试同时给出反证——绕过 schema
   门直接 ``execute``，那两条校验才会出现——所以「不可达」这条断言不是空转。
   **另有 ``invalid_child_run_id`` / ``invalid_cancel_reason`` /
   ``invalid_steer_message`` 三条是可达的**：schema 的 ``minLength: 1`` 只挡空串，
   **纯空白**串能穿过 schema 门到达工具，于是 v2 的前置校验把它们提前拒掉，并把
   5 行入参的码从 ``delegation_unknown_child`` 精化（见 ``VALIDATION_REFINEMENT``）。
4. **精化面被冻成字面清单**（恰好那 5 行，见
   ``IllegalArgumentMatrixTest.test_the_argument_validation_refinement_is_exactly_the_documented_set``），
   并且**唯一**走到工具体且两族等价的那一行（``task 纯空格``）与直接调生产 handler
   的三元组逐字一致。
   （切换前 v1 对照家族已随 Task 3 退役；"切换前后逐字相同"的结论由 Task 1 的提交
   与报告固定——本修复波只把**精确口径**写准并把整张表钉住。）
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Mapping, Optional, Sequence
from unittest.mock import patch

import httpx

from endless_task.agent_kernel import (
    AgentMessage,
    AgentMessageRole,
    RunCommand,
    RunOutcome,
    RunOutcomeStatus,
)
from endless_task.agent_platform import plain_json
from endless_task.api import AppSettings, create_app
from endless_task.delegation import (
    CancelAgentTool,
    CoordinatorDelegationHandler,
    ListAgentsTool,
    QueryAgentTool,
    SpawnAgentTool,
    SteerAgentTool,
    V2DelegationToolAsRegistered,
)
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderRequest,
    ProviderStreamEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from endless_task.runtime_v2.execution_impl.coordinator import ToolExecutionCoordinator
from endless_task.tool_platform import (
    DefaultToolPipeline,
    LegacyToolAdapter,
    ResolvedToolCall,
    ToolBatchRequest,
    ToolEffect as V2ToolEffect,
    ToolExecutionRequest,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolScheduler,
)
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolEffect,
    ToolResult,
    ToolValidationError,
)
from tests.fixtures.v2_client import (
    run_snapshot,
    send_message,
    wait_for_run_terminal,
)
from tests.fixtures.workspace_client import create_bound_conversation

#: 生产在册的委派五件：v2 原生类（被 shim 持有、真正被执行的对象）。
V2_TOOL_CLASSES = {
    "spawn_agent": SpawnAgentTool,
    "query_agent": QueryAgentTool,
    "cancel_agent": CancelAgentTool,
    "list_agents": ListAgentsTool,
    "steer_agent": SteerAgentTool,
}

#: **切换面的冻结字面快照**（v1 ``ToolDefinition`` 口径）。
#: 只含 v1 定义面上有的字段——``execution_mode`` / ``idempotency`` /
#: ``required_capabilities`` **不在其中**（v1 ``ToolDefinition`` 装不下，准入由
#: ``tool_platform/legacy_policy.py`` 的表按名提供，其值级奇偶由 parity 守卫负责）。
#: 基准原来是 Task 1 现场构造的五个 v1 ``*LegacyTool`` 类；Task 3 退役了它们，
#: 于是把当时的值内联到本文件——否则这条断言会变成"等于被删掉的东西"（假红）。
#: ``effect`` / ``approval_mode`` 存**成员名**：v1/v2 的枚举是两个类、成员名相同。
PRE_SWITCH_SURFACE = {
    "spawn_agent": {
        "name": "spawn_agent",
        "description": (
            "创建一个只读子代理去执行独立研究/检查任务，**立刻返回 child_run_id 句柄**"
            "（子代理在后台运行，结果不在本次返回值里）；"
            "随后用 query_agent 查询结果、用 cancel_agent 中止。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 8_192,
                    "description": "子代理要执行的独立任务说明。",
                },
                "expectedOutputSchema": {
                    "type": "object",
                    "description": "子代理结构化结果的 JSON Schema（可选）。",
                },
                "timeoutSeconds": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 86_400.0,
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
        "effect": "READ_ONLY",
        "approval_mode": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    "query_agent": {
        "name": "query_agent",
        "description": (
            "按 child_run_id 查询子代理的进展或结果：区分排队中（queued）、"
            "已开始跑（running）与终态（completed/failed/cancelled/"
            "interrupted），并给出已用时（elapsedSeconds）与最近活动时间"
            "（updatedAt）；查无此子运行按失败返回。"
        ),
        "input_schema": {
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
        "effect": "READ_ONLY",
        "approval_mode": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    "cancel_agent": {
        "name": "cancel_agent",
        "description": "取消一个进行中的子代理运行。",
        "input_schema": {
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
                    "maxLength": 512,
                    "description": "取消原因（可选）。",
                },
            },
            "required": ["childRunId"],
            "additionalProperties": False,
        },
        "effect": "READ_ONLY",
        "approval_mode": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 4_000,
    },
    "list_agents": {
        "name": "list_agents",
        "description": (
            "列出本会话派过的子代理运行（child_run_id / 状态 / 任务摘要 / "
            "已用时 / 是否隔离写），最近创建的在前，上限 50 条；没有子运行时空列表。"
            "子运行跨轮次可见；运行中的可以用 steer_agent 纠偏、cancel_agent 取消。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "effect": "READ_ONLY",
        "approval_mode": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    "steer_agent": {
        "name": "steer_agent",
        "description": (
            "给一个**运行中**的子代理送一句纠偏说明（只允许一句话纠偏；"
            "要改任务/改范围请先用 cancel_agent 取消，再用 spawn_agent 重新派）。"
            "子运行已结束则回 already_terminal；还在排队（尚未开始）或不属于本会话"
            "会报错。"
        ),
        "input_schema": {
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
        "effect": "READ_ONLY",
        "approval_mode": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 4_000,
    },
}

#: 模型面上与 v1 定义逐字段对齐的字段。
MODEL_FACING_FIELDS = (
    "name",
    "description",
    "timeout_seconds",
    "max_output_characters",
)

PARENT_CAPABILITIES = frozenset(
    {
        "workspace.read",
        "workspace.write",
        "memory.read",
        "session.query",
    }
)


class _EchoAgentKernel:
    """假内核：矩阵用例只需要 handler 走完 ``_coordinator_for`` 与业务校验。"""

    def __init__(self) -> None:
        self.commands: list[RunCommand] = []

    async def run(self, command: RunCommand) -> RunOutcome:
        self.commands.append(command)
        return RunOutcome(
            run_id=command.run_id,
            status=RunOutcomeStatus.COMPLETED,
            trace=command.trace,
            assistant_message=AgentMessage(
                role=AgentMessageRole.ASSISTANT,
                content="调查完成，结论如下。",
            ),
        )

    async def steer(self, run_id: str, message: AgentMessage) -> None:
        raise AssertionError

    async def cancel(self, run_id: str, reason: str) -> None:
        raise AssertionError


def delegation_handler() -> CoordinatorDelegationHandler:
    return CoordinatorDelegationHandler(
        kernel_provider=lambda request: _EchoAgentKernel(),
        capability_provider=lambda request: PARENT_CAPABILITIES,
    )


class _RecordingHandler:
    """只记录调用的假 handler：钉住 shim 真的把 request 送到 v2 类、再送到 handler。"""

    def __init__(self) -> None:
        self.requests: list[ToolExecutionRequest] = []

    async def spawn_child(self, request: ToolExecutionRequest) -> ToolOutcome:
        self.requests.append(request)
        return ToolOutcome(
            call_id=request.call_id,
            status=ToolOutcomeStatus.COMPLETED,
            content="子代理已启动。",
            structured_content={"childRunId": "child_1", "status": "running"},
        )


class _ScriptedProvider:
    """按请求序号吐脚本化响应的 provider（与 p1 release gate 同形）。"""

    name = "scripted"

    def __init__(self, responses: Sequence[Sequence[ProviderStreamEvent]] = ()) -> None:
        self.responses = list(responses)
        self.requests: list[ProviderRequest] = []

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        for event in self.responses[len(self.requests) - 1]:
            cancellation_token.raise_if_cancelled()
            yield event


@asynccontextmanager
async def production_app(
    database_path: Path,
    provider,
) -> AsyncIterator[Any]:
    """装配根形状：delegation=readonly + v2 管线开（生产默认）。"""
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            delegation_mode="readonly",
            tool_platform_v2_enabled=True,
        ),
        provider=provider,
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    try:
        yield client, app
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


def _provider_definition(
    request: ProviderRequest, name: str
) -> Optional[ProviderToolDefinition]:
    for definition in request.tools:
        if definition.name == name:
            return definition
    return None


_RUN = SimpleNamespace(
    id="run_parent_1", conversation_id="conversation_1", correlation_id=None
)
_MODEL_TURN = SimpleNamespace(
    id="turn_1", run_id="run_parent_1", created_at="2026-09-03T00:00:00Z"
)


def _production_tool_call(tool_name: str, arguments: Mapping[str, Any]) -> ToolCall:
    """用**生产代码本身**（``ToolExecutionCoordinator._tool_call``）造 v1 ToolCall。"""
    item = SimpleNamespace(
        provider_call=SimpleNamespace(
            id="call_1", name=tool_name, arguments=dict(arguments)
        )
    )
    return ToolExecutionCoordinator._tool_call(item, _RUN, _MODEL_TURN)


async def _pipeline_outcome(tool, call: ToolCall) -> ToolOutcome:
    """按 ``_execute_v2_auto_batch`` 的形状跑一遍生产默认的 v2 管线。"""
    adapter = LegacyToolAdapter(tool)
    request = _tool_execution_request(call)
    pipeline = DefaultToolPipeline(scheduler=ToolScheduler(max_concurrent=1))
    result = await pipeline.execute(
        ToolBatchRequest(calls=(ResolvedToolCall(request=request, tool=adapter),))
    )
    return result.outcomes[0]


def _tool_execution_request(call: ToolCall) -> ToolExecutionRequest:
    """``_execute_v2_auto_batch`` 形状的 v2 请求（供管线与「直接调 handler」共用）。"""
    return ToolExecutionRequest(
        call_id=call.id,
        tool_name=call.tool_name,
        arguments=call.arguments,
        conversation_id=call.conversation_id,
        run_id=call.response_variant_id,
        model_turn_id=call.turn_id,
        correlation_id=_RUN.correlation_id or _RUN.id,
        cancellation=CancellationToken(),
        created_at=call.created_at,
        legacy_turn_id=call.turn_id,
        legacy_response_variant_id=call.response_variant_id,
    )


def _result_observable(result: ToolResult) -> tuple:
    if result.error is None:
        return ("completed", None, result.content)
    return ("failed", result.error.code, result.error.safe_message)


def _outcome_observable(outcome: ToolOutcome) -> tuple:
    if outcome.status is ToolOutcomeStatus.COMPLETED:
        return ("completed", None, outcome.content)
    diagnostic = outcome.diagnostic
    code = diagnostic.code if diagnostic is not None else "delegation_failed"
    message = (
        diagnostic.safe_message if diagnostic is not None else outcome.content
    )
    return (outcome.status.value, code, message)


async def _production_observable(
    tool, *, tool_name: str, arguments: Mapping[str, Any]
) -> tuple:
    """把生产链路按顺序跑一遍，返回模型/审计可观测的结果。

    用的都是**生产代码本身**，不是复写：

    1. ``ToolExecutionCoordinator._validate_arguments``——``_prepare_tool_batch``
       里的 schema 门（v2 管线与 legacy 路径共用；失败即 ``_fail_item``）；
    2. ``ToolExecutionCoordinator._tool_call`` 造 v1 ``ToolCall``（run id 走
       ``response_variant_id``）→ 工具的 ``execute(call, token)``（flag off 的
       v1 执行契约，也是 shim 的入口）；
    3. ``LegacyToolAdapter`` + ``DefaultToolPipeline``——flag on 的执行机制，
       与 ``_execute_v2_auto_batch`` 相同的对象与顺序。
    """
    try:
        ToolExecutionCoordinator._validate_arguments(tool, arguments)
    except ToolValidationError as error:
        # ``_prepare_tool_batch`` 的 except 分支把 code 原样留下、把文案统一改成
        # 下面这句（见 coordinator 源码）；端到端用例
        # ``test_illegal_arguments_end_to_end_agree_with_the_production_path``
        # 会拿真实生产快照核对这句文案，防止这里的常量与生产漂移。
        return (
            ("schema_gate", "failed", error.code, "模型提供的工具调用或参数不符合要求。"),
            None,
            None,
        )
    call = _production_tool_call(tool_name, arguments)
    result = await tool.execute(call, CancellationToken())
    outcome = await _pipeline_outcome(tool, call)
    return (None, _result_observable(result), _outcome_observable(outcome))


class ProductionDelegationRegistryTest(unittest.IsolatedAsyncioTestCase):
    """生产注册表：注册的是 shim，内层是原生 v2 类；面逐字段等于切换前。"""

    async def test_registered_delegation_tools_wrap_the_native_v2_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = _ScriptedProvider()
            async with production_app(
                Path(directory) / "wiring.db", provider
            ) as (client, app):
                registry = app.state.container.tool_registry
                registered = {
                    definition.name for definition in registry.definitions()
                }
                self.assertEqual(
                    set(V2_TOOL_CLASSES), set(V2_TOOL_CLASSES) & registered
                )
                for name, tool_class in V2_TOOL_CLASSES.items():
                    with self.subTest(tool=name):
                        tool = registry.resolve(name)
                        self.assertIsInstance(tool, V2DelegationToolAsRegistered)
                        self.assertIs(type(tool.v2_tool), tool_class)
                        # D2：投影出来的是 **v1** 形状——审批闸门用
                        # ``effect is ToolEffect.LOCAL_WRITE`` 与 v1 枚举比，
                        # 裸 v2 枚举会让影响面/文案静默出错。
                        self.assertIsInstance(tool.definition.effect, ToolEffect)
                        self.assertNotIsInstance(
                            tool.definition.effect, V2ToolEffect
                        )
                        self.assertIs(tool.definition.effect, ToolEffect.READ_ONLY)
                        self.assertIsInstance(
                            tool.definition.approval_mode, ToolApprovalMode
                        )
                        self.assertIs(
                            tool.definition.approval_mode, ToolApprovalMode.AUTO
                        )
                        # 内层 v2 定义仍带 v2 枚举：证明这是投影，不是把 v1 定义
                        # 原样挂到 v2 对象上。
                        self.assertIsInstance(
                            tool.v2_tool.definition.effect, V2ToolEffect
                        )
                        self.assertNotIsInstance(
                            tool.v2_tool.definition.effect, ToolEffect
                        )
                        # D3：与切换前一致——委派五件的强制确认不经此门。
                        self.assertIs(
                            False,
                            tool.requires_explicit_confirmation(
                                _production_tool_call(name, {})
                            ),
                        )

    async def test_registered_definition_projection_equals_the_pre_switch_surface(
        self,
    ) -> None:
        """面快照：生产注册项的定义逐字段等于本文件内联的**切换前字面快照**。

        切换前 ``api/wiring.py`` 注册的正是五个 v1 ``*LegacyTool``；Task 3 退役了
        它们，所以基准换成 ``PRE_SWITCH_SURFACE``——继续现场构造旧类会变成
        "等于被删掉的东西"（假红）。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with production_app(
                Path(directory) / "surface.db", _ScriptedProvider()
            ) as (client, app):
                container = app.state.container
                for name, expected in PRE_SWITCH_SURFACE.items():
                    with self.subTest(tool=name):
                        after = container.tool_registry.resolve(name).definition
                        for field in MODEL_FACING_FIELDS:
                            self.assertEqual(
                                expected[field],
                                getattr(after, field),
                                f"{name}.{field} 与切换前不一致",
                            )
                        self.assertEqual(
                            expected["input_schema"],
                            plain_json(after.input_schema),
                            f"{name}.input_schema 与切换前不一致",
                        )
                        # v1/v2 的枚举是两个类、成员名相同 → 比成员名。
                        self.assertEqual(
                            expected["effect"],
                            after.effect.name,
                            f"{name}.effect 与切换前不一致",
                        )
                        self.assertEqual(
                            expected["approval_mode"],
                            after.approval_mode.name,
                            f"{name}.approval_mode 与切换前不一致",
                        )


class NativeV2ExecutionTest(unittest.IsolatedAsyncioTestCase):
    """执行证据：被执行的确实是原生 v2 类（不是「注册表里名字还在」）。"""

    async def test_the_v2_pipeline_executes_the_native_v2_class(self) -> None:
        """生产默认执行机制的形状：registry → LegacyToolAdapter → pipeline → v2 类。"""
        handler = _RecordingHandler()
        shim = V2DelegationToolAsRegistered(SpawnAgentTool(handler=handler))
        seen: list[ToolExecutionRequest] = []
        original = SpawnAgentTool.execute

        async def spy(self, request: ToolExecutionRequest) -> ToolOutcome:
            seen.append(request)
            return await original(self, request)

        arguments = {"task": "调研 A"}
        call = _production_tool_call("spawn_agent", arguments)
        with patch.object(SpawnAgentTool, "execute", spy):
            outcome = await _pipeline_outcome(shim, call)

        self.assertEqual(1, len(seen), "原生 v2 类的 execute 没有被执行")
        self.assertEqual("completed", outcome.status.value)
        self.assertEqual(1, len(handler.requests))
        request = seen[0]
        # shim 复用既有映射（D4）：run_id/model_turn_id/correlation_id 逐字复现今天
        # 的 v1 派生，handler 拿到的是同一个 request 对象。
        self.assertIs(handler.requests[0], request)
        self.assertEqual("call_1", request.call_id)
        self.assertEqual("spawn_agent", request.tool_name)
        self.assertEqual("conversation_1", request.conversation_id)
        self.assertEqual("run_parent_1", request.run_id)
        self.assertEqual("turn_1", request.model_turn_id)
        self.assertEqual("run_parent_1", request.correlation_id)
        self.assertEqual("2026-09-03T00:00:00Z", request.created_at)
        self.assertEqual("调研 A", request.arguments["task"])

    async def test_a_real_run_executes_the_native_v2_class(self) -> None:
        """真 HTTP → 真 runtime → 真注册表：对 v2 类打间谍，跑一次真调用。"""
        with tempfile.TemporaryDirectory() as directory:
            provider = _ScriptedProvider(
                (
                    (
                        ProviderToolCall(
                            id="call_list", name="list_agents", arguments={}
                        ),
                        ProviderCompleted("tool_calls"),
                    ),
                    (
                        ProviderTextDelta("列完了。"),
                        ProviderCompleted("stop"),
                    ),
                )
            )
            async with production_app(
                Path(directory) / "e2e.db", provider
            ) as (client, app):
                container = app.state.container
                conversation_id = (await create_bound_conversation(client))["id"]
                seen: list[ToolExecutionRequest] = []
                original = ListAgentsTool.execute

                async def spy(self, request: ToolExecutionRequest) -> ToolOutcome:
                    seen.append(request)
                    return await original(self, request)

                with patch.object(ListAgentsTool, "execute", spy):
                    handle = await send_message(
                        client,
                        conversation_id,
                        "看看派过哪些子代理",
                        idempotency_key="request-1",
                    )
                    await wait_for_run_terminal(container, handle["runId"])
                    snapshot = await run_snapshot(client, conversation_id)

                self.assertEqual(
                    1, len(seen), "真运行里原生 v2 类的 execute 没有被执行"
                )
                self.assertEqual("list_agents", seen[0].tool_name)
                states = snapshot["toolStates"]
                self.assertEqual(1, len(states))
                self.assertEqual("list_agents", states[0]["toolName"])
                self.assertEqual("completed", states[0]["status"])

                # 面不变：真 provider 请求里五个委派工具的定义仍是切换前的面。
                request = provider.requests[0]
                for name, expected in PRE_SWITCH_SURFACE.items():
                    with self.subTest(tool=name):
                        projected = _provider_definition(request, name)
                        self.assertIsNotNone(projected, f"{name} 不在模型面里")
                        self.assertEqual(
                            expected["description"], projected.description
                        )
                        self.assertEqual(
                            expected["input_schema"],
                            plain_json(projected.input_schema),
                        )


#: 非法入参矩阵（**22 行**）：每行是（标签, 工具名, 参数）。
#: 覆盖控制器点名的五类：空白/纯空格 task、非 Mapping 的 expectedOutputSchema、
#: 越界 timeoutSeconds、非数字/布尔 timeoutSeconds、非法 mode；外加其余三个
#: 工具的同类边界；末尾 5 行是**校验精化面**（纯空白 childRunId / reason /
#: message，见 ``VALIDATION_REFINEMENT``）。每行的期望三元组见 ``EXPECTED_FAILURES``。
ILLEGAL_ARGUMENT_MATRIX = (
    ("task 空串", "spawn_agent", {"task": ""}),
    ("task 纯空格", "spawn_agent", {"task": "   "}),
    ("task 非文本", "spawn_agent", {"task": 123}),
    (
        "expectedOutputSchema 非 Mapping",
        "spawn_agent",
        {"task": "x", "expectedOutputSchema": "no"},
    ),
    (
        "expectedOutputSchema 是列表",
        "spawn_agent",
        {"task": "x", "expectedOutputSchema": ["no"]},
    ),
    ("timeoutSeconds 越界（下）", "spawn_agent", {"task": "x", "timeoutSeconds": -1}),
    (
        "timeoutSeconds 越界（上）",
        "spawn_agent",
        {"task": "x", "timeoutSeconds": 86_401},
    ),
    ("timeoutSeconds 非数字", "spawn_agent", {"task": "x", "timeoutSeconds": "90"}),
    ("timeoutSeconds 布尔", "spawn_agent", {"task": "x", "timeoutSeconds": True}),
    (
        "timeoutSeconds 非数字文本",
        "spawn_agent",
        {"task": "x", "timeoutSeconds": "很快"},
    ),
    ("mode 非法", "spawn_agent", {"task": "x", "mode": "bogus"}),
    ("query childRunId 空串", "query_agent", {"childRunId": ""}),
    ("query childRunId 缺省", "query_agent", {}),
    ("cancel reason 空串", "cancel_agent", {"childRunId": "child_x", "reason": ""}),
    (
        "steer message 空串",
        "steer_agent",
        {"childRunId": "child_x", "message": ""},
    ),
    ("steer message 缺省", "steer_agent", {"childRunId": "child_x"}),
    ("list_agents 多余参数", "list_agents", {"unexpected": 1}),
    # 以下 5 行 = 「校验精化面」：切换让 v2 的前置参数校验第一次在生产路径上生效，
    # 这 5 类**纯空白**入参从 `delegation_unknown_child`（对空白 id 是误导）变成
    # v2 各自的 `invalid_child_run_id` / `invalid_cancel_reason` / `invalid_steer_message`。
    # 见 `VALIDATION_REFINEMENT`：保留参数精化行为并精确钉住。
    ("query childRunId 纯空格", "query_agent", {"childRunId": "   "}),
    ("query childRunId 制表符", "query_agent", {"childRunId": "\t"}),
    ("cancel childRunId 纯空格", "cancel_agent", {"childRunId": "   "}),
    ("cancel reason 纯空格", "cancel_agent", {"childRunId": "child_x", "reason": "   "}),
    ("steer message 纯空格", "steer_agent", {"childRunId": "child_x", "message": "   "}),
)

#: **冻结字面表**：每个非法行在生产路径上的可观测 `(status, code, safe_message)`。
#:
#: 值 = **切换后实测**（用本模块自己的 `_production_observable` + shim 家族跑出来的
#: 真值）。
#: 为什么必须钉**三元组**而不只是 code：终审 G 区证明——Task 3 退役 v1 家族后，矩阵
#: 只断言 `status == "failed"`，**消息文案在生产路径上没有任何钉子**（改
#: `delegation/agent_tools.py` 里的文案不会红任何用例）。这里的 22 行把三列一起冻住。
#:
#: 16 行被 `_validate_arguments` 的 schema 门先挡（`invalid_tool_arguments`），
#: 文案是 `_prepare_tool_batch` 的 except 分支统一改写的那一句（与
#: `_production_observable` 内注释所指的端到端锚点一致）；其余 6 行走工具体。
EXPECTED_FAILURES: dict[str, tuple[str, str, str]] = {
    # -- schema 门（`_validate_arguments`，先于管线分叉）：16 行 --
    "task 空串": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "task 非文本": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "expectedOutputSchema 非 Mapping": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "expectedOutputSchema 是列表": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "timeoutSeconds 越界（下）": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "timeoutSeconds 越界（上）": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "timeoutSeconds 非数字": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "timeoutSeconds 布尔": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "timeoutSeconds 非数字文本": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "mode 非法": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "query childRunId 空串": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "query childRunId 缺省": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "cancel reason 空串": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "steer message 空串": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "steer message 缺省": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    "list_agents 多余参数": (
        "failed",
        "invalid_tool_arguments",
        "模型提供的工具调用或参数不符合要求。",
    ),
    # -- 工具体（`execute` 的业务校验），6 行 --
    "task 纯空格": (
        "failed",
        "invalid_delegation_task",
        "spawn_agent 的 task 必须是文本。",
    ),
    "query childRunId 纯空格": (
        "failed",
        "invalid_child_run_id",
        "query_agent 的 childRunId 必须是文本。",
    ),
    "query childRunId 制表符": (
        "failed",
        "invalid_child_run_id",
        "query_agent 的 childRunId 必须是文本。",
    ),
    "cancel childRunId 纯空格": (
        "failed",
        "invalid_child_run_id",
        "cancel_agent 的 childRunId 必须是文本。",
    ),
    "cancel reason 纯空格": (
        "failed",
        "invalid_cancel_reason",
        "cancel_agent 的 reason 必须是文本。",
    ),
    "steer message 纯空格": (
        "failed",
        "invalid_steer_message",
        "steer_agent 的 message 必须是文本。",
    ),
}

#: **校验精化集合**（冻结字面）：`label -> (切换前码, 切换后码)`。
#:
#: 恰好 5 行，就是 `EXPECTED_FAILURES` 里**码与切换前不同**的那些行。这是防
#: "静默扩大/缩小精化面"的钉子：将来有人又改了一个入参的码（多一行）、或把某条
#: v2 前置校验绕开（少一行），本表与实测表就比对不上。
#:
#: **为什么这是有意的**（终审 F1 裁定）：切换把 v2 工具层的参数校验**第一次**放上
#: 生产路径（切换前 v1 `*LegacyTool._dispatch` 是裸转调 handler，v2 `execute` 的
#: 校验在生产上是死代码）。方向是**更严更准**——没有任何入参从"被拒"变成"通过"：
#: `""`（空串）本来就被 schema 的 `minLength: 1` 挡住，能到 handler 的只有**纯空白**
#: 串；而纯空白 `childRunId` 从前会走到"查无此子运行"（对空白 id 是误导），现在被
#: v2 用精确码提前拒掉。其中带**合法**子运行的纯空白 `reason`（从前照样下发取消）与
#: 纯空白 `message`（从前被 `strip()` 成空串照样下达空纠偏）现在**先拒**。
VALIDATION_REFINEMENT: dict[str, tuple[str, str]] = {
    "query childRunId 纯空格": ("delegation_unknown_child", "invalid_child_run_id"),
    "query childRunId 制表符": ("delegation_unknown_child", "invalid_child_run_id"),
    "cancel childRunId 纯空格": ("delegation_unknown_child", "invalid_child_run_id"),
    "cancel reason 纯空格": ("delegation_unknown_child", "invalid_cancel_reason"),
    "steer message 纯空格": ("delegation_unknown_child", "invalid_steer_message"),
}


class IllegalArgumentMatrixTest(unittest.IsolatedAsyncioTestCase):
    """非法入参矩阵：每一行都被拒、三元组逐行钉住；精化面恰好是记录的那 5 行。"""

    def _shim_tools(self):
        """切换后生产形状：v2 原生类外包一层 shim（v1 旧副本已在 Task 3 退役）。"""
        handler = delegation_handler()
        return {
            name: V2DelegationToolAsRegistered(v2_class(handler=handler))
            for name, v2_class in V2_TOOL_CLASSES.items()
        }

    async def test_illegal_arguments_are_rejected_on_the_production_path(self) -> None:
        """矩阵每一行都必须在生产路径上真的被「拒掉」，且三元组等于**冻结表**。

        三元组 = ``(status, code, safe_message)``。终审 G 区证明：Task 3 退役 v1
        家族后，矩阵只断言了 ``status == "failed"``，所以**消息文案在生产路径上
        没有任何钉子**——改 ``delegation/agent_tools.py`` 里的文案不会红任何用例。
        这里逐行核对 ``EXPECTED_FAILURES``（值 = 切换后实测），把三列一起冻住。

        切换前的对照家族（五个 v1 ``*LegacyTool``）已在 Task 3 退役，所以这里不再
        复写一份 v1 定义去比"切换前后逐字相同"（那份第二份字面定义正是本批要消灭的
        漂移面）；精确口径见模块 docstring 与
        ``test_the_argument_validation_refinement_is_exactly_the_documented_set``。
        """
        self.assertEqual(
            {label for label, _, _ in ILLEGAL_ARGUMENT_MATRIX},
            set(EXPECTED_FAILURES),
            "冻结表和矩阵的标签集合不一致——矩阵改了就必须同步改表",
        )
        tools = self._shim_tools()
        for label, tool_name, arguments in ILLEGAL_ARGUMENT_MATRIX:
            with self.subTest(row=label):
                gate, v1_result, pipeline_result = await _production_observable(
                    tools[tool_name], tool_name=tool_name, arguments=arguments
                )
                self.assertIsNotNone(gate or v1_result)
                if gate is None:
                    # 工具体路径：v1 执行契约的投影与生产管线观察必须逐字一致。
                    self.assertEqual("failed", v1_result[0])
                    self.assertEqual(v1_result, pipeline_result)
                    observed = (v1_result[0], v1_result[1], v1_result[2])
                else:
                    observed = (gate[1], gate[2], gate[3])
                self.assertEqual(
                    EXPECTED_FAILURES[label],
                    observed,
                    f"{label}：(status, code, safe_message) 与冻结表不一致",
                )

    async def test_the_argument_validation_refinement_is_exactly_the_documented_set(
        self,
    ) -> None:
        """**参数校验精化面恰好是 ``VALIDATION_REFINEMENT`` 那 5 行**——不多不少。

        **为什么这是有意的**（终审 F1 裁定，不是缺陷）：切换把 v2 工具层的前置参数
        校验**第一次放上生产路径**（切换前 v1 ``*LegacyTool._dispatch`` 是裸转调
        handler，v2 ``execute`` 的校验在生产上是死代码）。方向是**更严更准**：没有
        任何入参从"被拒"变成"通过"，只有 5 类**纯空白**入参的错误码被精化
        （``delegation_unknown_child`` → 各自的新码；对空白 id 说"查无此子运行"是
        误导）。其中带**合法**子运行的纯空白 ``reason`` / ``message`` 从前会照样
        下发取消 / 下达空纠偏，现在**先拒**。

        本用例是"静默扩大/缩小精化面"的钉子：它**只比对两份冻结字面**——
        ``EXPECTED_FAILURES``（冻结的三元组表）与 ``VALIDATION_REFINEMENT``（冻结的
        精化集合）——**自身不碰生产**。所以它守的是"两份记录互相自洽"：精化行必须
        **恰好**是那 5 行，``EXPECTED_FAILURES`` 里的码必须等于记录的新码，被精确掉的
        老码必须**在任何一行都不再出现**。生产是否仍与 ``EXPECTED_FAILURES`` 一致，
        由 ``test_illegal_arguments_are_rejected_on_the_production_path``（矩阵用例，逐行
        走生产路径）与
        ``test_illegal_arguments_end_to_end_agree_with_the_production_path``（端到端锚点）
        负责：生产漂了在那里红，记录改了则必须在这里同步。
        """
        self.assertEqual(5, len(VALIDATION_REFINEMENT))
        for label, (pre_switch_code, post_switch_code) in VALIDATION_REFINEMENT.items():
            with self.subTest(row=label):
                self.assertIn(label, EXPECTED_FAILURES)
                self.assertEqual(post_switch_code, EXPECTED_FAILURES[label][1])
                self.assertNotEqual(pre_switch_code, post_switch_code)

        # "码与切换前不同"的行 = 冻结表里出现新码的那些行 = 记录的 5 行。
        new_codes = {post for _, post in VALIDATION_REFINEMENT.values()}
        refined_rows = {
            label
            for label, triple in EXPECTED_FAILURES.items()
            if triple[1] in new_codes
        }
        self.assertEqual(set(VALIDATION_REFINEMENT), refined_rows)

        # 被精确掉的老码不再出现在**任何**一行的冻结三元组里（精化面没有漏网的）。
        pre_switch_codes = {pre for pre, _ in VALIDATION_REFINEMENT.values()}
        self.assertEqual({"delegation_unknown_child"}, pre_switch_codes)
        self.assertEqual(
            set(),
            pre_switch_codes
            & {triple[1] for triple in EXPECTED_FAILURES.values()},
            "切换前的码仍然出现在矩阵里——精化面被静默缩小了",
        )

        # 矩阵里走到工具体的行**恰好** 6 行 = 精化的 5 行 + 唯一两族同码的 `task`。
        tool_body_rows = {
            label for label, triple in EXPECTED_FAILURES.items() if triple[1] != "invalid_tool_arguments"
        }
        self.assertEqual(
            set(VALIDATION_REFINEMENT) | {"task 纯空格"}, tool_body_rows
        )

    async def test_the_whitespace_task_row_matches_the_handler_directly(self) -> None:
        """唯一两族都走到工具体的非法行：shim 路径三元组 == 直接调生产 handler。

        ``task 纯空格`` 能穿过 schema 门（``minLength: 1`` 只挡空串），两族最终都调
        ``CoordinatorDelegationHandler.spawn_child(request)``，所以这里直接调**生产
        handler** 核对三元组与 shim 路径逐字一致（v1 家族已退役，**不**重建第二份
        v1 定义——那正是本批要消灭的漂移面）。
        """
        handler = delegation_handler()
        shim = V2DelegationToolAsRegistered(SpawnAgentTool(handler=handler))
        arguments = {"task": "   "}
        gate, v1_result, pipeline_result = await _production_observable(
            shim, tool_name="spawn_agent", arguments=arguments
        )
        self.assertIsNone(gate, "纯空白 task 应当穿过 schema 门、走到工具体")
        self.assertEqual(EXPECTED_FAILURES["task 纯空格"], v1_result)
        self.assertEqual(v1_result, pipeline_result)

        call = _production_tool_call("spawn_agent", arguments)
        outcome = await handler.spawn_child(_tool_execution_request(call))
        self.assertEqual(
            v1_result,
            (
                outcome.status.value,
                outcome.diagnostic.code,
                outcome.diagnostic.safe_message,
            ),
            "shim 路径与直接调生产 handler 的三元组不一致",
        )

    async def test_the_v2_only_pre_checks_are_unreachable_on_the_production_path(
        self,
    ) -> None:
        """证实控制器的分析：v2 多出来的两条前置校验在生产路径上到不了。

        v2 ``execute`` 在调 handler 之前会发 ``invalid_expected_output_schema``
        （``expectedOutputSchema`` 不是 Mapping）与 ``invalid_delegation_timeout``
        （``timeoutSeconds`` 越界/非数字/布尔），而 handler 里没有这两个错误码。
        但生产路径的第一步是协调器的 schema 门（读 ``tool.definition.input_schema``），
        它先把这些入参挡掉，所以工具层那两条校验不可达。**注意口径**：不可达的
        **只有这两条**——v2 侧的 ``invalid_child_run_id`` / ``invalid_cancel_reason`` /
        ``invalid_steer_message`` 是**可达**的（纯空白串过 schema 门），它们把 5 行
        入参的码精化（见 ``VALIDATION_REFINEMENT``）；矩阵里唯一两族同码同文案的
        工具体行是 ``task 纯空白``（``invalid_delegation_task``）。
        """
        tools = self._shim_tools()
        unreachable = {
            "invalid_expected_output_schema",
            "invalid_delegation_timeout",
        }
        expected_reachable = {
            "invalid_tool_arguments",
            "invalid_delegation_task",
            "invalid_child_run_id",
            "invalid_cancel_reason",
            "invalid_steer_message",
        }
        observed_codes: set[str] = set()
        for label, tool_name, arguments in ILLEGAL_ARGUMENT_MATRIX:
            with self.subTest(row=label):
                gate, v1_result, pipeline_result = await _production_observable(
                    tools[tool_name], tool_name=tool_name, arguments=arguments
                )
                if gate is not None:
                    observed_codes.add(gate[2])
                    continue
                observed_codes.add(v1_result[1])
                observed_codes.add(pipeline_result[1])
        self.assertEqual(
            set(),
            observed_codes & unreachable,
            f"生产路径上出现了本该不可达的 v2 前置校验错误码：{sorted(observed_codes & unreachable)}",
        )
        self.assertEqual(expected_reachable, observed_codes)

        # 反证（本断言不空转）：绕过 schema 门直接 execute，v2 侧那两条校验确实会
        # 出现——它们只是到不了生产路径。
        bypassed = {}
        for label, arguments in (
            ("schema", {"task": "x", "expectedOutputSchema": "no"}),
            ("timeout", {"task": "x", "timeoutSeconds": -1}),
        ):
            result = await tools["spawn_agent"].execute(
                _production_tool_call("spawn_agent", arguments), CancellationToken()
            )
            bypassed[label] = result.error.code if result.error else None
        self.assertEqual("invalid_expected_output_schema", bypassed["schema"])
        self.assertEqual("invalid_delegation_timeout", bypassed["timeout"])

    async def test_illegal_arguments_end_to_end_agree_with_the_production_path(
        self,
    ) -> None:
        """端到端锚点：真 HTTP 生产路径的可观测三元组与本文件的 harness 一致。

        没有这条，``_production_observable`` 里那句 schema 门文案就只是测试里的
        常量；这里用真 app + 脚本化 provider 跑两行矩阵，核对
        ``toolStates`` 的 status/errorCode/safeMessage。
        """
        with tempfile.TemporaryDirectory() as directory:
            provider = _ScriptedProvider(
                (
                    (
                        ProviderToolCall(
                            id="call_1",
                            name="spawn_agent",
                            arguments={"task": "x", "expectedOutputSchema": "no"},
                        ),
                        ProviderCompleted("tool_calls"),
                    ),
                    (
                        ProviderTextDelta("好的。"),
                        ProviderCompleted("stop"),
                    ),
                    (
                        ProviderToolCall(
                            id="call_2",
                            name="spawn_agent",
                            arguments={"task": "   "},
                        ),
                        ProviderCompleted("tool_calls"),
                    ),
                    (
                        ProviderTextDelta("好的。"),
                        ProviderCompleted("stop"),
                    ),
                )
            )
            async with production_app(
                Path(directory) / "matrix.db", provider
            ) as (client, app):
                container = app.state.container
                conversation_id = (await create_bound_conversation(client))["id"]
                tools = self._shim_tools()
                expected_rows = (
                    (
                        "expectedOutputSchema 非 Mapping",
                        {"task": "x", "expectedOutputSchema": "no"},
                    ),
                    ("task 纯空格", {"task": "   "}),
                )
                for ordinal, (label, arguments) in enumerate(expected_rows, start=1):
                    with self.subTest(row=label):
                        handle = await send_message(
                            client,
                            conversation_id,
                            "试试",
                            idempotency_key=f"request-{ordinal}",
                        )
                        await wait_for_run_terminal(container, handle["runId"])
                        snapshot = await run_snapshot(client, conversation_id)
                        states = snapshot["toolStates"]
                        self.assertEqual(1, len(states))
                        harness = await _production_observable(
                            tools["spawn_agent"],
                            tool_name="spawn_agent",
                            arguments=arguments,
                        )
                        gate, v1_result, _pipeline = harness
                        if gate is not None:
                            expected = (gate[1], gate[2], gate[3])
                        else:
                            expected = (
                                v1_result[0],
                                v1_result[1],
                                v1_result[2],
                            )
                        self.assertEqual(
                            expected,
                            (
                                states[0]["status"],
                                states[0]["errorCode"],
                                states[0]["safeMessage"],
                            ),
                            f"{label}：生产路径与 harness 不一致",
                        )


if __name__ == "__main__":
    unittest.main()
