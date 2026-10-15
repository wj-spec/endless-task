"""子运行的 HTTP 面（列表 + 取消）：按会话限定、状态词表固定（切片 9 M2 Task 6）。

这一层钉的是前端页签要的三件事：

* **列表**：某个会话派过的子运行（最近创建的在前，上限 50），字段刚好够页签
  渲染——任务摘要截断 200 字、summary 截断 2000 字，不泄漏内部 id 之外的东西。
* **状态词表**：`queued | running | completed | failed | cancelled | interrupted`，
  其中 `queued` = 库行 running 且 `started_at IS NULL`（还在并发闸门外）。派生
  与工具面**同一份实现**（`delegation.runtime_handler`），不是这里另造一套。
* **取消**：按会话限定——子运行不属于该会话一律 404（不泄漏存在性）；终态返回
  `already_terminal`（良性 no-op，不是错误），真正下发了才回 `cancelling`。

口径由 Task 3 的控制器裁定固定：`elapsedSeconds` 在终态行按 `terminal_at` 收口
（不随时间增长），排队中为 None。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.agent_platform import AgentPlatformError
from endless_task.api import AppSettings, create_app
from endless_task.delegation import (
    ChildContextPolicy,
    ChildModelPolicy,
    ChildOutputSchema,
    ChildRunState,
    InProcessChildCoordinator,
    SpawnSpec,
    WorkspaceMode,
)
from endless_task.runtime import FakeProvider
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime_ledger import TraceContext
from endless_task.storage.sqlite_delegation_child_run_repository import (
    SqliteDelegationChildRunRepository,
)
from tests.fixtures.workspace_client import create_bound_conversation

#: 页签每项的字段集合（多一个少一个都算接口变了）。
ITEM_FIELDS = {
    "childRunId",
    "status",
    "task",
    "createdAt",
    "startedAt",
    "terminalAt",
    "elapsedSeconds",
    "summary",
    "errorCode",
    "safeMessage",
    "workspaceMode",
}

#: 控制器裁定的线上状态词表。
WIRE_STATUSES = {
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
}


def _settings(directory: str, *, delegation: bool) -> AppSettings:
    return AppSettings(
        database_path=Path(directory) / "api.db",
        memory_proposals_enabled=False,
        knowledge_proposals_enabled=False,
        artifact_proposals_enabled=False,
        task_proposals_enabled=False,
        tool_platform_v2_enabled=True,
        delegation_mode=("readonly" if delegation else "0"),
    )


@asynccontextmanager
async def local_client(directory: str, *, delegation: bool = True):
    app = create_app(
        settings=_settings(directory, delegation=delegation),
        provider=FakeProvider(chunks=("ok",)),
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


def _reserve_child(
    store,
    child_run_id: str,
    *,
    conversation_id: str,
    task: str = "任务",
    workspace_mode: str = "read_only_shared",
    parent_run_id: str | None = None,
):
    """落一行子运行（父运行 id 默认按 child_run_id 派生，便于注入假拥有者）。"""
    return store.reserve_child_run(
        child_run_id=child_run_id,
        parent_run_id=parent_run_id or f"run_{child_run_id}",
        parent_tool_call_id=f"call_{child_run_id}",
        parent_conversation_id=conversation_id,
        depth=1,
        capability_profile="subagent_readonly",
        workspace_mode=workspace_mode,
        timeout_seconds=120.0,
        task=task,
    )


class _RecordingOwnerCoordinator:
    """记录"收到哪些取消"的假拥有者协调器（真内核在这里跑不起来）。"""

    def __init__(self, conversation_id: str, on_cancel) -> None:
        self._conversation_id = conversation_id
        self._on_cancel = on_cancel

    def covers_conversation(self, conversation_id) -> bool:
        if not conversation_id or not self._conversation_id:
            return True
        return conversation_id == self._conversation_id

    async def cancel(self, child_run_id: str, reason: str) -> None:
        self._on_cancel(child_run_id, reason)

    def finalize_cancelled_children(self) -> None:
        # 关停路径会遍历协调器缓存收尾；假协调器没有托管的后台任务。
        pass


class DelegatedChildrenListRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_lists_this_conversation_children_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                parent = await create_bound_conversation(client)
                parent_id = str(parent["id"])
                other = await create_bound_conversation(client)
                other_id = str(other["id"])
                _reserve_child(store, "child_a", conversation_id=parent_id, task="任务 A")
                _reserve_child(
                    store,
                    "child_b",
                    conversation_id=parent_id,
                    task="任务 B",
                    workspace_mode="isolated_snapshot",
                )
                _reserve_child(store, "child_other", conversation_id=other_id)

                response = await client.get(
                    f"/api/v2/conversations/{parent_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                payload = response.json()
                self.assertEqual(2, payload["total"])
                self.assertEqual(
                    ["child_b", "child_a"],  # 最近创建的在前
                    [item["childRunId"] for item in payload["items"]],
                )
                first = payload["items"][0]
                self.assertEqual(ITEM_FIELDS, set(first))
                self.assertEqual("child_b", first["childRunId"])
                self.assertEqual("任务 B", first["task"])
                self.assertEqual("isolated_snapshot", first["workspaceMode"])
                self.assertIsNotNone(first["createdAt"])
                # 还没进内核（started_at 为空）= 仍在并发闸门外排队。
                self.assertEqual("queued", first["status"])
                self.assertIsNone(first["startedAt"])
                self.assertIsNone(first["terminalAt"])
                self.assertIsNone(first["elapsedSeconds"])
                self.assertIsNone(first["summary"])
                self.assertIsNone(first["errorCode"])
                self.assertIsNone(first["safeMessage"])

    async def test_does_not_list_another_conversations_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                parent = await create_bound_conversation(client)
                parent_id = str(parent["id"])
                other = await create_bound_conversation(client)
                other_id = str(other["id"])
                _reserve_child(store, "child_other", conversation_id=other_id)

                response = await client.get(
                    f"/api/v2/conversations/{parent_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual({"items": [], "total": 0}, response.json())
                # 别的会话自己的那一条照样看得见。
                own = await client.get(
                    f"/api/v2/conversations/{other_id}/delegated-children"
                )
                self.assertEqual(["child_other"], _item_ids(own.json()))

    async def test_empty_listing_is_a_normal_two_hundred(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual({"items": [], "total": 0}, response.json())

    async def test_reports_every_wire_status_of_the_fixed_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                _reserve_child(store, "child_queued", conversation_id=conversation_id)
                _reserve_child(store, "child_running", conversation_id=conversation_id)
                store.mark_child_started("child_running")
                for child_run_id, state in (
                    ("child_completed", ChildRunState.COMPLETED),
                    ("child_failed", ChildRunState.FAILED),
                    ("child_cancelled", ChildRunState.CANCELLED),
                    ("child_interrupted", ChildRunState.INTERRUPTED),
                ):
                    _reserve_child(store, child_run_id, conversation_id=conversation_id)
                    store.mark_child_started(child_run_id)
                    store.mark_child_terminal(child_run_id, state=state, summary="收尾")

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                by_id = {
                    item["childRunId"]: item["status"]
                    for item in response.json()["items"]
                }
                self.assertEqual(
                    {
                        "child_queued": "queued",
                        "child_running": "running",
                        "child_completed": "completed",
                        "child_failed": "failed",
                        "child_cancelled": "cancelled",
                        "child_interrupted": "interrupted",
                    },
                    by_id,
                )
                self.assertTrue(set(by_id.values()) <= WIRE_STATUSES)

    async def test_caps_the_listing_at_fifty(self) -> None:
        """上限 50 由 handler 把关（与工具面同一个数字），total = 返回条数。"""
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                for index in range(51):
                    _reserve_child(
                        store, f"child_{index:03d}", conversation_id=conversation_id
                    )

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                payload = response.json()
                self.assertEqual(50, payload["total"])
                self.assertEqual(50, len(payload["items"]))

    async def test_truncates_task_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                _reserve_child(
                    store, "child_long", conversation_id=conversation_id, task="x" * 300
                )
                store.mark_child_started("child_long")
                store.mark_child_terminal(
                    "child_long",
                    state=ChildRunState.FAILED,
                    summary="y" * 2500,
                    error_code="child_timeout",
                    safe_message="子运行超时。",
                )

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                item = response.json()["items"][0]
                self.assertEqual(201, len(item["task"]))
                self.assertTrue(item["task"].endswith("…"))
                self.assertEqual(2001, len(item["summary"]))
                self.assertTrue(item["summary"].endswith("…"))
                self.assertEqual("failed", item["status"])
                self.assertEqual("child_timeout", item["errorCode"])
                self.assertEqual("子运行超时。", item["safeMessage"])

    async def test_freezes_elapsed_at_terminal_time(self) -> None:
        """终态行的已用时不能随当前时间增长（Task 3 裁定：按 terminal_at 收口）。

        换成固定时钟的库来钉：`started_at == terminal_at` 时收口后的已用时恰好 0；
        若拿 now 计时，这里会是"从固定时刻到今天"的一大坨秒数——对已经结束的
        子运行就是假信息。排队中的行没有开始时刻，只能给 None。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                frozen = "2026-09-03T00:00:00.000Z"
                store = SqliteDelegationChildRunRepository(
                    container.database, clock=lambda: frozen
                )
                # 路由读 handler：把它的库换成固定时钟那一份（同库不同时钟）。
                container.delegation_handler._child_run_store = store
                _reserve_child(store, "child_done", conversation_id=conversation_id)
                store.mark_child_started("child_done")
                store.mark_child_terminal(
                    "child_done", state=ChildRunState.COMPLETED, summary="完成"
                )
                _reserve_child(store, "child_queued", conversation_id=conversation_id)

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                by_id = {
                    item["childRunId"]: item
                    for item in response.json()["items"]
                }
                done = by_id["child_done"]
                self.assertEqual(frozen, done["startedAt"])
                self.assertEqual(frozen, done["terminalAt"])
                self.assertEqual(0, done["elapsedSeconds"])
                self.assertEqual("完成", done["summary"])
                self.assertIsNone(by_id["child_queued"]["elapsedSeconds"])

    async def test_listing_without_delegation_is_an_empty_collection(self) -> None:
        """装配没开委派时列表回空集合：没有委派就不可能派过子运行。"""
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=False) as (client, app):
                self.assertIsNone(app.state.container.delegation_handler)
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                response = await client.get(
                    f"/api/v2/conversations/{conversation_id}/delegated-children"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual({"items": [], "total": 0}, response.json())


class _QueuedChildKernel:
    """内核里没有活动运行（子运行还卡在闸门外）的假内核。"""

    async def run(self, command):  # pragma: no cover - 本用例里子运行进不到内核
        raise AssertionError("排队中的子运行不该进内核")

    async def cancel(self, run_id, reason):
        # 与 `_GateKernel` 同形：内核活动表里没有它 —— 正是"闸门外排队"的形态。
        raise AgentPlatformError(
            "invalid_agent_kernel_value", "Cannot cancel an inactive child run"
        )

    async def steer(self, run_id, message):  # pragma: no cover
        raise AssertionError("本用例不纠偏")


def _queued_spawn_spec(*, parent_run_id: str) -> SpawnSpec:
    return SpawnSpec(
        task="排队中的子任务",
        expected_output=ChildOutputSchema(
            name="research_output", schema={"type": "object"}
        ),
        capability_profile="subagent_readonly",
        requested_capabilities=frozenset({"workspace.read"}),
        tool_allowlist=(),
        model_policy=ChildModelPolicy(),
        context_policy=ChildContextPolicy(),
        workspace_mode=WorkspaceMode.READ_ONLY_SHARED,
        timeout_seconds=120.0,
        parent_run_id=parent_run_id,
        parent_tool_call_id="call_queued",
    )


class DelegatedChildCancelRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancels_a_running_child_through_its_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                _reserve_child(
                    store,
                    "child_running",
                    conversation_id=conversation_id,
                    parent_run_id="run_owner",
                )
                store.mark_child_started("child_running")
                cancelled: list[tuple[str, str]] = []
                handler._coordinators["run_owner"] = _RecordingOwnerCoordinator(
                    conversation_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    "/delegated-children/child_running/cancel"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    {"childRunId": "child_running", "status": "cancelling"},
                    response.json(),
                )
                self.assertEqual([("child_running", "用户取消")], cancelled)

    async def test_cancels_a_queued_child_waiting_at_the_gate(self) -> None:
        """排队态（`running` ∧ `started_at IS NULL`）的子运行也能经 HTTP 取消。

        这条链路此前完全无用例：内核里没有它的活动运行（回
        `invalid_agent_kernel_value`）→ 落回协调器的 `_child_tasks` → `task.cancel()`
        → 补写 cancelled。取消必须**如实**回 `cancelling`，且不谎报成别的状态。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                coordinator = InProcessChildCoordinator(
                    _QueuedChildKernel(),
                    parent_run_id="run_owner",
                    parent_conversation_id=conversation_id,
                    parent_capabilities=frozenset({"workspace.read"}),
                    parent_trace=TraceContext(
                        trace_id="trace_queued",
                        run_id="run_owner",
                        correlation_id="corr_queued",
                    ),
                    child_run_store=store,
                    task_supervisor=BackgroundTaskSupervisor(name="test-queued-cancel"),
                )
                # 与真实拥有者同一形状：协调器代答本会话、库行 parent_run_id 指向它。
                handler._coordinators["run_owner"] = coordinator
                child_run_id = coordinator.start_child(
                    coordinator.prepare_spawn(
                        _queued_spawn_spec(parent_run_id="run_owner")
                    )
                )
                row = store.get_child_run(child_run_id)
                # 前置断言本身就是"排队态"的定义：running 且还没记 started_at。
                self.assertEqual(ChildRunState.RUNNING, row.state)
                self.assertIsNone(row.started_at)

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    f"/delegated-children/{child_run_id}/cancel"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    {"childRunId": child_run_id, "status": "cancelling"},
                    response.json(),
                )
                # 取消在返回前就落了终态：库里是 cancelled，而且没有谎报成别的。
                after = store.get_child_run(child_run_id)
                self.assertEqual(ChildRunState.CANCELLED, after.state)
                self.assertIsNotNone(after.terminal_at)
                self.assertIsNone(after.started_at)
                await coordinator.shutdown()

    async def test_terminal_child_is_a_benign_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                _reserve_child(store, "child_done", conversation_id=conversation_id)
                store.mark_child_started("child_done")
                store.mark_child_terminal(
                    "child_done", state=ChildRunState.COMPLETED, summary="完成"
                )

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    "/delegated-children/child_done/cancel"
                )

                self.assertEqual(200, response.status_code)
                self.assertEqual(
                    {"childRunId": "child_done", "status": "already_terminal"},
                    response.json(),
                )

    async def test_another_conversations_child_is_not_found(self) -> None:
        """跨会话一律 404（不泄漏存在性），即便拥有者协调器就在缓存里。"""
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                store = container.delegation_child_run_repository
                parent = await create_bound_conversation(client)
                parent_id = str(parent["id"])
                other = await create_bound_conversation(client)
                other_id = str(other["id"])
                _reserve_child(
                    store,
                    "child_other",
                    conversation_id=other_id,
                    parent_run_id="run_owner",
                )
                store.mark_child_started("child_other")
                cancelled: list[tuple[str, str]] = []
                handler._coordinators["run_owner"] = _RecordingOwnerCoordinator(
                    other_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )

                response = await client.post(
                    f"/api/v2/conversations/{parent_id}"
                    "/delegated-children/child_other/cancel"
                )

                self.assertEqual(404, response.status_code)
                self.assertEqual(
                    "delegation_unknown_child", response.json()["error"]["code"]
                )
                self.assertEqual([], cancelled)

    async def test_unknown_child_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    "/delegated-children/child_missing/cancel"
                )

                self.assertEqual(404, response.status_code)
                self.assertEqual(
                    "delegation_unknown_child", response.json()["error"]["code"]
                )
                # 路由错误载荷按项目惯例给中文文案（handler 里那句是给模型看的）。
                self.assertEqual(
                    "该会话下没有这个子运行。",
                    response.json()["error"]["message"],
                )

    async def test_running_child_without_a_reachable_owner_is_a_conflict(self) -> None:
        """非终态但拥有者不在本进程：绝不能谎报 cancelling。

        拥有者协调器被回收/换了进程时，库里还是 running，可是没有任何东西能真的
        对它下发取消。回 200 cancelling 会让页签显示"取消中"而子运行照跑——这里
        如实报冲突，等启动对账或重建拥有者。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory) as (client, app):
                container = app.state.container
                store = container.delegation_child_run_repository
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])
                _reserve_child(
                    store,
                    "child_orphan",
                    conversation_id=conversation_id,
                    parent_run_id="run_gone",
                )
                store.mark_child_started("child_orphan")

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    "/delegated-children/child_orphan/cancel"
                )

                self.assertEqual(409, response.status_code)
                self.assertEqual(
                    "delegation_child_owner_unavailable",
                    response.json()["error"]["code"],
                )

    async def test_cancel_without_delegation_is_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=False) as (client, app):
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                response = await client.post(
                    f"/api/v2/conversations/{conversation_id}"
                    "/delegated-children/child_running/cancel"
                )

                self.assertEqual(404, response.status_code)
                self.assertEqual(
                    "delegation_unknown_child", response.json()["error"]["code"]
                )


def _item_ids(payload: dict) -> list[str]:
    return [item["childRunId"] for item in payload["items"]]


if __name__ == "__main__":
    unittest.main()
