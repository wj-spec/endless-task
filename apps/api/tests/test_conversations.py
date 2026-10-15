"""会话路由：删除父会话时收束其委派子运行、归档只通知不起轮（切片 9 M2 Task 5）。

两条行为的分工是刻意的（见 task-5 侦察事实）：

* **归档**只改会话的 status/archived_at，不动 runs/entries —— 子运行继续跑完，
  拦截发生在回流侧（唤醒服务的 `conversation_is_active` 回调）；所以这里钉的是
  装配根把那个回调接到了**真库**上，而不是恒真的桩。
* **删除**沿 `parent_conversation_id` 级联，够不到身上挂着
  `delegation_parent_run_id` 的委派子会话 —— 谁来收子运行必须由删除路径自己
  在下发，且必须在会话真的被删掉之前（删完就没有父会话上下文可枚举了）。
"""

from __future__ import annotations

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.domain.models import FinishReason
from endless_task.domain.repositories import NotFoundError
from endless_task.runtime import FakeProvider
from tests.fixtures.workspace_client import (
    create_bound_conversation,
    create_bound_workspace,
)


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
async def local_client(directory: str, *, delegation: bool):
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
    store, child_run_id: str, *, parent_run_id: str, conversation_id: str
):
    return store.reserve_child_run(
        child_run_id=child_run_id,
        parent_run_id=parent_run_id,
        parent_tool_call_id=f"call_{child_run_id}",
        parent_conversation_id=conversation_id,
        depth=1,
        capability_profile="subagent_readonly",
        workspace_mode="read_only_shared",
        timeout_seconds=120.0,
        task=f"任务 {child_run_id}",
    )


class _RecordingOwnerCoordinator:
    """记录"收到哪些取消"的假拥有者协调器。

    路由测试不该驱动真内核（那需要让子运行真跑一轮 v2），所以这里替换掉
    handler 的协调器缓存，只验两件事：取消被下发给了**持有该子运行**的协调器，
    且下发的时刻会话还在（即删除路径的顺序正确）。
    """

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


class ConversationDeleteDelegationTest(unittest.IsolatedAsyncioTestCase):
    async def test_delete_conversation_cancels_its_delegated_children(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=True) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                self.assertIsNotNone(handler)
                parent = await create_bound_conversation(client)
                parent_id = str(parent["id"])
                other = await create_bound_conversation(client)
                other_id = str(other["id"])
                store = container.delegation_child_run_repository
                _reserve_child(
                    store,
                    "child_parent",
                    parent_run_id="run_parent",
                    conversation_id=parent_id,
                )
                _reserve_child(
                    store,
                    "child_other",
                    parent_run_id="run_other",
                    conversation_id=other_id,
                )

                cancelled: list[tuple[str, str]] = []
                seen: list[str] = []

                def record_parent_cancel(child_run_id: str, reason: str) -> None:
                    cancelled.append((child_run_id, reason))
                    # 删除之后就没有父会话上下文了：下发必须发生在删除**之前**，
                    # 所以此刻会话一定还在（否则这里抛 NotFoundError，取消就丢了）。
                    seen.append(
                        container.chat_repository.get_conversation(parent_id).id
                    )

                handler._coordinators["run_parent"] = _RecordingOwnerCoordinator(
                    parent_id, record_parent_cancel
                )
                handler._coordinators["run_other"] = _RecordingOwnerCoordinator(
                    other_id, lambda child_run_id, reason: cancelled.append(
                        ("other", child_run_id)
                    )
                )

                deleted = await client.delete(f"/conversations/{parent_id}")

                self.assertEqual(204, deleted.status_code)
                self.assertEqual([("child_parent", "父会话已删除")], cancelled)
                self.assertEqual([parent_id], seen)
                # 父会话真的删掉了；别的会话与它的子运行原封不动。
                with self.assertRaises(NotFoundError):
                    container.chat_repository.get_conversation(parent_id)
                self.assertEqual(
                    other_id,
                    container.chat_repository.get_conversation(other_id).id,
                )
                (row_other,) = store.list_child_runs_for_conversation(other_id)
                self.assertFalse(row_other.terminal)

    async def test_delete_conversation_cancels_children_of_descendant_branches(
        self,
    ) -> None:
        """后代分支会话的子运行也必须被收（本会话删除路径此前的缺口）。

        删除只显式删根行，后代靠 `parent_conversation_id` 的外键级联消失；
        而委派子运行挂在**分支会话**身上（`delegation_parent_run_id` 是子运行
        id，不在级联链上）。取消若不逐后代会话下发，分支连同它的子运行一起
        失联——正是 `DELETE /conversations/{id}` 与工作区路径同类的那条缺口。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=True) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                self.assertIsNotNone(handler)
                store = container.delegation_child_run_repository

                parent = await create_bound_conversation(client)
                parent_id = str(parent["id"])
                elsewhere_workspace = await create_bound_workspace(client)
                branch_id = _seed_branch(
                    container.chat_repository, parent_id, elsewhere_workspace
                )
                # 前置：分支确实是后代（删除时随根会话级联消失的那一个）。
                self.assertIn(
                    branch_id,
                    container.chat_repository.list_descendant_ids(parent_id),
                )

                _reserve_child(
                    store,
                    "child_parent",
                    parent_run_id="run_parent",
                    conversation_id=parent_id,
                )
                _reserve_child(
                    store,
                    "child_branch",
                    parent_run_id="run_branch",
                    conversation_id=branch_id,
                )

                cancelled: list[tuple[str, str]] = []
                handler._coordinators["run_parent"] = _RecordingOwnerCoordinator(
                    parent_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )
                handler._coordinators["run_branch"] = _RecordingOwnerCoordinator(
                    branch_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )

                deleted = await client.delete(f"/conversations/{parent_id}")

                self.assertEqual(204, deleted.status_code)
                # 根会话与后代分支的子运行都收到取消（分支那条是本用例的判据）。
                self.assertEqual(
                    sorted(
                        [
                            ("child_branch", "父会话已删除"),
                            ("child_parent", "父会话已删除"),
                        ]
                    ),
                    sorted(cancelled),
                )
                with self.assertRaises(NotFoundError):
                    container.chat_repository.get_conversation(parent_id)
                with self.assertRaises(NotFoundError):
                    container.chat_repository.get_conversation(branch_id)

    async def test_delete_conversation_without_delegation_still_deletes(self) -> None:
        """装配没开委派时删除路径照常工作（清理调用被 `is not None` 挡住）。"""
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=False) as (client, app):
                container = app.state.container
                self.assertIsNone(container.delegation_handler)
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                deleted = await client.delete(f"/conversations/{conversation_id}")

                self.assertEqual(204, deleted.status_code)
                with self.assertRaises(NotFoundError):
                    container.chat_repository.get_conversation(conversation_id)


def _seed_branch(chat_repository, parent_id: str, workspace_id: str) -> str:
    """给会话补一轮 v1 轮次后开一个后代分支，并把分支迁到别的工作区。

    这样分支就**不在**父会话所属工作区的会话名册里，只能靠
    `list_descendant_ids` 的后代展开够到——正是"级联取消必须落在逐条
    `delete_id` 的准备循环里"才覆盖得到的那条路。
    """
    snapshot = chat_repository.create_turn(
        conversation_id=parent_id, client_request_id="branch-seed", content="第一轮"
    )
    variant_id = snapshot.response_variants[0].variant.id
    chat_repository.mark_response_running(
        turn_id=snapshot.turn.id, variant_id=variant_id
    )
    chat_repository.complete_response(
        turn_id=snapshot.turn.id,
        variant_id=variant_id,
        content="第一轮的回答",
        finish_reason=FinishReason.STOP,
    )
    branch = chat_repository.create_branch(parent_conversation_id=parent_id)
    chat_repository.set_conversation_workspace(branch.id, workspace_id)
    return branch.id


class WorkspaceDeleteDelegationTest(unittest.IsolatedAsyncioTestCase):
    """`DELETE /workspaces/{id}` 是第二条删会话的路，同样必须收束子运行。

    它与 `DELETE /conversations/{id}` 是同一个缺口：工作区级联只沿
    `parent_conversation_id` 清理，够不到身上挂着 `delegation_parent_run_id`
    的委派子运行。测试用假拥有者协调器记录"收到哪些取消"，不驱动真内核
    （理由同 `_RecordingOwnerCoordinator`）。
    """

    async def _new_workspace_conversation(self, client) -> tuple[str, str]:
        workspace_id = await create_bound_workspace(client)
        response = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        response.raise_for_status()
        return workspace_id, str(response.json()["id"])

    async def test_delete_workspace_cancels_children_of_its_conversations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=True) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                self.assertIsNotNone(handler)
                store = container.delegation_child_run_repository

                target_workspace, target_id = await self._new_workspace_conversation(
                    client
                )
                _, other_id = await self._new_workspace_conversation(client)
                _reserve_child(
                    store,
                    "child_target",
                    parent_run_id="run_target",
                    conversation_id=target_id,
                )
                _reserve_child(
                    store,
                    "child_other",
                    parent_run_id="run_other",
                    conversation_id=other_id,
                )

                cancelled: list[tuple[str, str]] = []
                seen: list[str] = []

                def record_target_cancel(child_run_id: str, reason: str) -> None:
                    cancelled.append((child_run_id, reason))
                    # 下发必须在会话真的被删掉之前（删完就没有父会话上下文了），
                    # 所以此刻还能取到它，否则这里抛 NotFoundError。
                    seen.append(
                        container.chat_repository.get_conversation(target_id).id
                    )

                handler._coordinators["run_target"] = _RecordingOwnerCoordinator(
                    target_id, record_target_cancel
                )
                handler._coordinators["run_other"] = _RecordingOwnerCoordinator(
                    other_id,
                    lambda child_run_id, reason: cancelled.append(
                        ("other", child_run_id)
                    ),
                )

                deleted = await client.delete(f"/workspaces/{target_workspace}")

                self.assertEqual(204, deleted.status_code)
                self.assertEqual([("child_target", "工作区已删除")], cancelled)
                self.assertEqual([target_id], seen)
                # 工作区下的会话真的删掉了；别的工作区与它的子运行原封不动。
                with self.assertRaises(NotFoundError):
                    container.chat_repository.get_conversation(target_id)
                self.assertEqual(
                    other_id,
                    container.chat_repository.get_conversation(other_id).id,
                )
                (row_other,) = store.list_child_runs_for_conversation(other_id)
                self.assertFalse(row_other.terminal)

    async def test_delete_workspace_cancels_children_of_descendant_conversations(
        self,
    ) -> None:
        """后代分支已迁到别的工作区：名册够不到它，只有后代展开够得到。"""
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=True) as (client, app):
                container = app.state.container
                handler = container.delegation_handler
                self.assertIsNotNone(handler)
                store = container.delegation_child_run_repository

                target_workspace, parent_id = await self._new_workspace_conversation(
                    client
                )
                elsewhere_workspace, _ = await self._new_workspace_conversation(client)
                branch_id = _seed_branch(
                    container.chat_repository, parent_id, elsewhere_workspace
                )

                # 前置：分支不在工作区名册里，只在父会话的后代里。
                self.assertNotIn(
                    branch_id,
                    container.chat_repository.list_workspace_conversation_ids(
                        target_workspace
                    ),
                )
                self.assertIn(
                    branch_id,
                    container.chat_repository.list_descendant_ids(parent_id),
                )

                _reserve_child(
                    store,
                    "child_parent",
                    parent_run_id="run_ws_parent",
                    conversation_id=parent_id,
                )
                _reserve_child(
                    store,
                    "child_branch",
                    parent_run_id="run_ws_branch",
                    conversation_id=branch_id,
                )

                cancelled: list[tuple[str, str]] = []
                handler._coordinators["run_ws_parent"] = _RecordingOwnerCoordinator(
                    parent_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )
                handler._coordinators["run_ws_branch"] = _RecordingOwnerCoordinator(
                    branch_id,
                    lambda child_run_id, reason: cancelled.append(
                        (child_run_id, reason)
                    ),
                )

                deleted = await client.delete(f"/workspaces/{target_workspace}")

                self.assertEqual(204, deleted.status_code)
                self.assertEqual(
                    sorted(
                        [
                            ("child_branch", "工作区已删除"),
                            ("child_parent", "工作区已删除"),
                        ]
                    ),
                    sorted(cancelled),
                )


class ConversationArchiveWakeupTest(unittest.IsolatedAsyncioTestCase):
    async def test_archived_conversation_is_not_active_for_wakeup(self) -> None:
        """归档的拦截点是唤醒服务的活跃判定，且必须读真库。

        会话不存在时让 `NotFoundError` 冒泡（唤醒服务据此"只记日志、不通知"），
        与 `send` 抛 `NotFoundError` 是同一条语义。
        """
        with tempfile.TemporaryDirectory() as directory:
            async with local_client(directory, delegation=True) as (client, app):
                container = app.state.container
                wakeup = container.child_wakeup_service
                self.assertIsNotNone(wakeup)
                conversation = await create_bound_conversation(client)
                conversation_id = str(conversation["id"])

                self.assertTrue(wakeup._conversation_is_active(conversation_id))

                archived = await client.patch(
                    f"/conversations/{conversation_id}",
                    json={"status": "archived"},
                )

                self.assertEqual(200, archived.status_code)
                self.assertEqual("archived", archived.json()["status"])
                self.assertIsNotNone(
                    container.chat_repository.get_conversation(
                        conversation_id
                    ).archived_at
                )
                self.assertFalse(wakeup._conversation_is_active(conversation_id))

                with self.assertRaises(NotFoundError):
                    wakeup._conversation_is_active("conv_missing")


if __name__ == "__main__":
    unittest.main()
