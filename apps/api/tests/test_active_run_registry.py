"""切片 10 T3 Step 3 / 验收 ④：在飞句柄必须按 **run id** 可寻址，不能被新 run 顶掉。

**生产入口约束**：`POST /api/v2/conversations/{id}/messages`
的第二条提交**今天已经被存储层挡住**（`storage/v2/transcript.py` 的
`ConflictError("Conversation already has an active v2 run")` → 409），所以"绕过前端
再发一条消息就覆盖句柄"这条路径**不成立**；`v2_runs` 上还有
`idx_v2_runs_one_active_per_lane`（每泳道只许一条活动 run）作为第二道结构闸。

但 `_active_runs` **每会话只有一个槽位**这一点仍然是真陷阱：`resolve_recovery(retry=True)`
是唯一**不**调用 `_require_no_active_runs` 的起跑路径（`gateway_impl/runs.py`），
它在同一会话的**另一条泳道**上起新 run 时会把旧 run 的句柄从会话槽位里挤掉 →
`/api/v2/runs/{旧}/cancel` 对一条**真的还在跑**的 run 回 `accepted:false`。

本模块就用这条真实可达的路径做两向用例：
- 旧 run 在会话已起更新的 run 之后**仍可取消**（修之前：`accepted:false` → 能红）；
- 新 run **不受影响**（照旧跑完），且两条句柄在终态后被干净释放（无泄漏）。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import ProviderCompleted, ProviderTextDelta
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.domain import (
    Actor,
    LaneKind,
    TranscriptEntryType,
)
from tests.fixtures.workspace_client import create_bound_conversation

ANSWER = "已完成这一轮。"
TERMINAL = {"completed", "failed", "cancelled"}


class GateProvider:
    """第一个请求会被闸住（旧 run 一直活着），其余请求立刻完成。"""

    name = "active-run-registry"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.requests: list[object] = []

    async def stream(self, request, cancellation_token: CancellationToken):
        index = len(self.requests)
        self.requests.append(request)
        if index == 0:
            while not self.gate.is_set():
                cancellation_token.raise_if_cancelled()
                await asyncio.sleep(0.01)
        cancellation_token.raise_if_cancelled()
        yield ProviderTextDelta(ANSWER)
        cancellation_token.raise_if_cancelled()
        yield ProviderCompleted()


async def _wait_until(predicate, *, timeout: float = 8.0, interval: float = 0.005):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not reached in time")


@asynccontextmanager
async def local_client(database_path: Path, provider):
    app = create_app(
        settings=AppSettings(
            database_path=database_path,
            memory_proposals_enabled=False,
            artifact_proposals_enabled=False,
            scheduler_enabled=False,
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
        provider.gate.set()
        await lifespan.__aexit__(None, None, None)


def seed_leftover_active_run(repository, conversation_id: str, lane_id: str) -> str:
    """在**另一条泳道**上造一条活跃 run，且它没有网关句柄。

    形状与真实世界一致：`task_runs`/提醒的 run 由 worker 直接经 v2 仓储提交
    （`tasks/worker.py:_execute_task_turn`），从不注册进网关的 `_active_runs`；
    `resolve_recovery` 因此认为它"不是本进程在跑的"。

    必须在**线程**里调用：run A 正挂在 provider 上，事件循环线程里同步写库会与
    执行器持有的写事务互等（`storage/v2/base.py` 记录过这个同线程死锁）。
    """
    base_entry = repository.append_entry(
        conversation_id=conversation_id,
        lane_id=lane_id,
        type=TranscriptEntryType.USER_MESSAGE,
        actor=Actor.USER,
        payload={"content": "分支基线"},
        context_policy={"include_in_llm": True, "transform": "full"},
    )
    second_lane = repository.create_lane(
        conversation_id=conversation_id,
        kind=LaneKind.PERSISTENT_BRANCH,
        base_entry_id=base_entry.id,
    )
    trigger_entry = repository.append_entry(
        conversation_id=conversation_id,
        lane_id=second_lane.id,
        type=TranscriptEntryType.USER_MESSAGE,
        actor=Actor.USER,
        payload={"content": "另一条泳道上的遗留一轮"},
        context_policy={"include_in_llm": True, "transform": "full"},
    )
    leftover = repository.create_run(
        conversation_id=conversation_id,
        lane_id=second_lane.id,
        trigger_entry_id=trigger_entry.id,
    )
    repository.start_run(leftover.id)
    return leftover.id


class ActiveRunRegistryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "registry.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def _send(self, client, conversation_id: str, key: str) -> str:
        response = await client.post(
            f"/api/v2/conversations/{conversation_id}/messages",
            headers={"Idempotency-Key": key},
            json={"content": key},
        )
        self.assertEqual(202, response.status_code, response.text)
        return response.json()["runId"]

    async def _wait_terminal(self, repository, run_id: str):
        def done():
            record = repository.get_run(run_id)
            return record if record.status.value in TERMINAL else None

        return await _wait_until(done)

    # ---- 验收 ④：旧 run 仍可取消，新 run 不受影响 --------------------------
    async def test_older_run_stays_cancelable_after_a_newer_run_starts(self) -> None:
        provider = GateProvider()
        async with local_client(self.database_path, provider) as (client, app):
            repository = app.state.container.runtime_v2_repository
            gateway = app.state.container.runtime_v2_gateway
            conversation_id = (await create_bound_conversation(client))["id"]

            old_run_id = await self._send(client, conversation_id, "old")
            await _wait_until(lambda: len(provider.requests) == 1)
            old_record = repository.get_run(old_run_id)
            self.assertEqual("running", old_record.status.value)
            self.assertEqual(
                old_run_id, gateway._active_runs[conversation_id].run_id
            )
            # 先给证据：普通第二次提交今天就被存储层挡住（不会覆盖句柄）
            blocked = await client.post(
                f"/api/v2/conversations/{conversation_id}/messages",
                headers={"Idempotency-Key": "blocked"},
                json={"content": "second"},
            )
            self.assertEqual(409, blocked.status_code)
            self.assertIn("already has an active v2 run", blocked.text)

            # 真实可达的"同会话起更新的 run"路径：另一条泳道上的活跃 run 走恢复重试
            leftover_id = await asyncio.to_thread(
                seed_leftover_active_run,
                repository,
                conversation_id,
                old_record.lane_id,
            )
            recovered = await client.post(
                f"/api/v2/runs/{leftover_id}/recovery", json={"action": "retry"}
            )
            self.assertEqual(200, recovered.status_code, recovered.text)
            new_run_id = recovered.json()["newRunId"]
            self.assertNotEqual(old_run_id, new_run_id)
            # 会话槽位现在是新 run：旧 run 的"每个会话一个句柄"确实被顶掉了
            self.assertEqual(
                new_run_id, gateway._active_runs[conversation_id].run_id
            )
            # 但旧 run 仍在跑，按 run id 必须**依然可取消**
            self.assertIsNotNone(gateway._find_active_run(old_run_id))
            cancelled = await client.post(f"/api/v2/runs/{old_run_id}/cancel")
            self.assertEqual(200, cancelled.status_code)
            self.assertEqual(
                {"runId": old_run_id, "accepted": True}, cancelled.json()
            )
            # 反向：新 run 不受影响（照旧跑完，不是被连带取消）
            new_record = await self._wait_terminal(repository, new_run_id)
            self.assertEqual("completed", new_record.status.value)

            provider.gate.set()
            old_final = await self._wait_terminal(repository, old_run_id)
            self.assertEqual("cancelled", old_final.status.value)

    # ---- 句柄不泄漏 ---------------------------------------------------------
    async def test_gateway_releases_both_handle_indexes(self) -> None:
        provider = GateProvider()
        provider.gate.set()  # 这一条不需要闸门
        async with local_client(self.database_path, provider) as (client, app):
            gateway = app.state.container.runtime_v2_gateway
            repository = app.state.container.runtime_v2_repository
            conversation_id = (await create_bound_conversation(client))["id"]
            run_id = await self._send(client, conversation_id, "solo")
            await self._wait_terminal(repository, run_id)
            await _wait_until(lambda: not gateway.has_active_run(conversation_id))
            self.assertEqual({}, gateway._active_runs)
            self.assertEqual({}, gateway._active_runs_by_id)
            self.assertIsNone(gateway._find_active_run(run_id))


if __name__ == "__main__":
    unittest.main()
