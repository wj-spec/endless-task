"""切片 9 第四批 Task 3：v2 定义 ↔ 准入策略表的奇偶守卫 + 面字段的冻结字面快照。

Task 1（``ea2a315``）把生产注册对象换成了薄 shim；Task 3 退役了 v1 的五个
``*LegacyTool`` 类（``delegation/legacy_tools.py``）。从此委派五件的定义只剩
**两个真相源**：

* **v2 定义**（``delegation/agent_tools.py``）：name / description / input_schema /
  effect / approval / timeout / max_output，**外加** ``execution_mode`` /
  ``idempotency`` / ``required_capabilities``；
* **准入策略表**（``tool_platform/legacy_policy.py`` 的
  ``builtin_legacy_tool_policy(name)``）：**准入**（调度器的 admission、表面授权、
  重试判定）实际消费的那三个字段的唯一源——v1 ``ToolDefinition`` 装不下它们，
  所以 shim 不提供、生产 adapter 按名从表里取。

本模块因此断言两件事：

1. **仅存的两个真相源之间**：v2 定义的三个准入字段与表**逐值相等**
   （:meth:`…test_v2_definition_agrees_with_the_policy_table_on_admission_fields`）。
   改表里一个值 → 红；改 v2 定义里的同名字段 → 同样红（值级漂移）。
   「改 v2 定义**不会**改准入」这条**故意不对称**的观测点在
   ``tests/test_delegation_v2_switch_guards.py::AdmissionMetadataAsymmetryGuardTest``
   （它盯的是管线算出的准入模式），本模块只做值级奇偶。
2. **面字段的冻结字面快照**：v1 副本退役后，name/description/input_schema/effect/
   approval/timeout/max_output 已经没有第二个家族可比 → 这里内联成期望值，
   于是"有人改了描述/schema/超时/输出上限"仍然是**显式 diff**，而不是静默漂移。
"""

from __future__ import annotations

import unittest

from endless_task.agent_platform import plain_json
from endless_task.delegation.agent_tools import (
    CANCEL_AGENT_NAME,
    LIST_AGENTS_NAME,
    QUERY_AGENT_NAME,
    SPAWN_AGENT_NAME,
    STEER_AGENT_NAME,
    CancelAgentTool,
    ListAgentsTool,
    QueryAgentTool,
    SpawnAgentTool,
    SteerAgentTool,
)
from endless_task.tool_platform.legacy_policy import builtin_legacy_tool_policy

#: 在册的五个委派工具名；顺序即断言顺序。
DELEGATION_TOOL_NAMES = (
    "spawn_agent",
    "query_agent",
    "cancel_agent",
    "list_agents",
    "steer_agent",
)

#: v2 侧五个 ``AgentToolV2``；handler 关键字参数。
V2_TOOL_CLASSES = {
    SPAWN_AGENT_NAME: SpawnAgentTool,
    QUERY_AGENT_NAME: QueryAgentTool,
    CANCEL_AGENT_NAME: CancelAgentTool,
    LIST_AGENTS_NAME: ListAgentsTool,
    STEER_AGENT_NAME: SteerAgentTool,
}

#: 面字段的**冻结字面快照**（本文件的期望值，不再是"另一个家族的定义"）。
#: effect / approval 存枚举**成员名**：v1/v2 的 ``ToolEffect`` 是两个类、成员名相同，
#: 存名字才能把"影响面漂移"变成显式 diff；approval 存 v2 的 ``ApprovalPolicy`` 成员名
#: （shim 侧由 ``_v1_approval_from_v2`` 的显式 ``is`` 分支投影，语义一一对应）。
FROZEN_SURFACE = {
    SPAWN_AGENT_NAME: {
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
        "approval": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    QUERY_AGENT_NAME: {
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
        "approval": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    CANCEL_AGENT_NAME: {
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
        "approval": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 4_000,
    },
    LIST_AGENTS_NAME: {
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
        "approval": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 20_000,
    },
    STEER_AGENT_NAME: {
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
        "approval": "AUTO",
        "timeout_seconds": 30.0,
        "max_output_characters": 4_000,
    },
}


class _FakeHandler:
    """只钉定义奇偶的假 handler：不牵进真协调器（构造只校验方法存在）。"""

    async def spawn_child(self, request):
        raise AssertionError("parity test only inspects definitions")

    async def query_child(self, request):
        raise AssertionError("parity test only inspects definitions")

    async def cancel_child(self, request):
        raise AssertionError("parity test only inspects definitions")

    async def list_children(self, request):
        raise AssertionError("parity test only inspects definitions")

    async def steer_child(self, request):
        raise AssertionError("parity test only inspects definitions")


class DelegationToolParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = _FakeHandler()

    def _v2(self, tool_name: str):
        return V2_TOOL_CLASSES[tool_name](handler=self.handler)

    def test_the_five_names_cover_both_truth_sources(self) -> None:
        """五个名字同时覆盖 v2 类、资源快照与准入表——少一处就是静默失守。"""
        self.assertEqual(set(DELEGATION_TOOL_NAMES), set(V2_TOOL_CLASSES))
        self.assertEqual(set(DELEGATION_TOOL_NAMES), set(FROZEN_SURFACE))
        self.assertEqual(
            {
                SPAWN_AGENT_NAME,
                QUERY_AGENT_NAME,
                CANCEL_AGENT_NAME,
                LIST_AGENTS_NAME,
                STEER_AGENT_NAME,
            },
            set(DELEGATION_TOOL_NAMES),
        )
        for tool_name in DELEGATION_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                self.assertIsNotNone(
                    builtin_legacy_tool_policy(tool_name),
                    f"{tool_name}: 准入策略表里没有这一行",
                )

    def test_v2_definition_agrees_with_the_policy_table_on_admission_fields(self) -> None:
        """仅存的两个真相源：v2 定义 ↔ ``builtin_legacy_tool_policy(name)`` 表。

        三个字段**逐一比 value**：``execution_mode``（调度准入）、``idempotency``
        （``reliability/retry.py`` 消费）、``required_capabilities``（表面授权消费）。
        少比任何一个，守卫就会在两处真相已经漂移时给出假绿。
        """
        for tool_name in DELEGATION_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                definition = self._v2(tool_name).definition
                policy = builtin_legacy_tool_policy(tool_name)
                self.assertIsNotNone(policy, f"{tool_name}: 准入策略表里没有这一行")
                self.assertEqual(
                    policy.execution_mode,
                    definition.execution_mode,
                    f"{tool_name}.execution_mode 与准入表漂移",
                )
                self.assertEqual(
                    policy.idempotency,
                    definition.idempotency,
                    f"{tool_name}.idempotency 与准入表漂移",
                )
                self.assertEqual(
                    policy.required_capabilities,
                    definition.required_capabilities,
                    f"{tool_name}.required_capabilities 与准入表漂移",
                )

    def test_v2_definition_agrees_with_the_frozen_surface_snapshot(self) -> None:
        """面字段：内核字面快照（第二个家族已退役，漂移必须是显式 diff）。"""
        for tool_name in DELEGATION_TOOL_NAMES:
            with self.subTest(tool=tool_name):
                definition = self._v2(tool_name).definition
                expected = FROZEN_SURFACE[tool_name]
                self.assertEqual(
                    expected["name"], definition.name, f"{tool_name}.name 漂移"
                )
                # 描述逐字入表：assertIn 会放过漂移，所以这里必须整串。
                self.assertEqual(
                    expected["description"],
                    definition.description,
                    f"{tool_name}.description 漂移",
                )
                self.assertEqual(
                    expected["input_schema"],
                    plain_json(definition.input_schema),
                    f"{tool_name}.input_schema 漂移",
                )
                self.assertEqual(
                    expected["effect"],
                    definition.effect.name,
                    f"{tool_name}.effect 漂移",
                )
                self.assertEqual(
                    expected["approval"],
                    definition.approval.name,
                    f"{tool_name}.approval 漂移",
                )
                self.assertEqual(
                    expected["timeout_seconds"],
                    definition.timeout_seconds,
                    f"{tool_name}.timeout_seconds 漂移",
                )
                self.assertEqual(
                    expected["max_output_characters"],
                    definition.max_output_characters,
                    f"{tool_name}.max_output_characters 漂移",
                )

    def test_spawn_schema_exposes_mode_on_the_v2_definition(self) -> None:
        properties = self._v2(SPAWN_AGENT_NAME).definition.input_schema["properties"]
        self.assertEqual(["readonly", "isolated_write"], list(properties["mode"]["enum"]))
        self.assertEqual(
            plain_json(properties["mode"]),
            FROZEN_SURFACE[SPAWN_AGENT_NAME]["input_schema"]["properties"]["mode"],
        )


if __name__ == "__main__":
    unittest.main()
