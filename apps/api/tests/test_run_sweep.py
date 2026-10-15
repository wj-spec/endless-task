"""切片 10 T2 Step 3：v2 run 的启动清扫（死 run 不许再假装在跑）。

覆盖验收 ③ 的三件事：

1. **清扫真的清**：上一个进程遗留的活动态行（跑了半截的 / 从未开始的）在启动
   清扫后落终态，并留下如实的 `run_failed` 痕迹与错误码；
2. **不误伤在跑的 run**：双向守卫——边界之后产生的行不被清（把边界挪到更早，
   同一条行确实会被清，证明"没被清"是**边界**在起作用而不是清扫根本没跑）；
   结构性守卫——全仓只有**一处**调用点，它在 lifespan 里、在 `yield` 之前，
   没有任何周期性调用者；以及 API 启动后的真实在跑 run 不受影响；
3. **前端据此不再无限重连**的后端依据：清扫后快照的 `runningRunId` 为 None
   （`runtimeController` 的重连循环以 `snapshot.runningRunId` 为退出条件），
   并且 `runState.status/errorCode/safeMessage` 如实说明这次运行是被进程重启中断的。
"""

from __future__ import annotations

import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

import endless_task
from endless_task.api import AppSettings, create_app
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.fake_provider import FakeProvider
from endless_task.runtime.provider import ProviderRequest, ProviderStreamEvent
from endless_task.runtime_v2 import (
    Actor,
    RunStatus,
    RuntimeV2SessionGateway,
    TranscriptEntryType,
)
from endless_task.runtime_v2.run_sweep import (
    SWEEP_ERROR_CODE,
    SWEEP_NEVER_STARTED_MESSAGE,
    SWEEP_SAFE_MESSAGE,
    sweep_interrupted_v2_runs,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
)
from endless_task.tooling import ToolRegistry


class _SnapshotOnlyProvider:
    """只做快照的网关用不到 provider。"""

    name = "unused"

    async def stream(
        self,
        request: ProviderRequest,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ProviderStreamEvent]:
        del request, cancellation_token
        raise AssertionError("Snapshot-only gateway must not call the provider")
        yield  # pragma: no cover - 让函数体成为生成器


class _TickClock:
    """可控时钟（`SqliteRuntimeV2Repository(clock=...)` 是公开构造参数）。"""

    def __init__(self) -> None:
        self.value = "2026-01-01T00:00:00.000Z"

    def set(self, value: str) -> None:
        self.value = value

    def now(self) -> str:
        return self.value


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("Condition was not met before timeout")


def _create_run(
    chat_repository: SqliteChatRepository,
    repository: SqliteRuntimeV2Repository,
    *,
    start: bool,
):
    conversation = chat_repository.create_conversation()
    lane = repository.create_lane(conversation_id=conversation.id)
    trigger = repository.append_entry(
        conversation_id=conversation.id,
        lane_id=lane.id,
        type=TranscriptEntryType.USER_MESSAGE,
        actor=Actor.USER,
        payload={"content": "任务"},
        context_policy={"include_in_llm": True, "transform": "full"},
    )
    run = repository.create_run(
        conversation_id=conversation.id,
        lane_id=lane.id,
        trigger_entry_id=trigger.id,
    )
    repository.set_conversation_pointer(
        conversation_id=conversation.id,
        active_lane_id=lane.id,
    )
    if start:
        run = repository.start_run(run.id)
    return conversation, run


class _SweepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.database = Database(Path(self._tmp.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)

    def _run(self, *, start: bool, repository=None, chat_repository=None):
        return _create_run(
            chat_repository or self.chat_repository,
            repository or self.repository,
            start=start,
        )


class SweepSemanticsTest(_SweepTestCase):
    def test_sweep_finalizes_runs_left_over_from_a_previous_process(self) -> None:
        conversation, running = self._run(start=True)
        _, never_started = self._run(start=False)
        self.assertIs(RunStatus.RUNNING, running.status)
        self.assertIs(RunStatus.CREATED, never_started.status)

        swept = sweep_interrupted_v2_runs(self.repository)
        self.assertEqual(
            sorted([running.id, never_started.id]),
            sorted(run.id for run in swept),
        )

        # 跑了半截的：走既有"结束本次运行"语义落 FAILED，起点与文案都如实。
        swept_running = self.repository.get_run(running.id)
        self.assertIs(RunStatus.FAILED, swept_running.status)
        self.assertEqual(SWEEP_ERROR_CODE, swept_running.error_code)
        self.assertEqual(SWEEP_SAFE_MESSAGE, swept_running.safe_message)
        self.assertEqual(running.started_at, swept_running.started_at)
        self.assertIsNotNone(swept_running.finished_at)
        self.assertTrue(
            any(
                event.event_type == "run_failed"
                for event in self.repository.list_runtime_events(running.id)
            ),
            "清扫必须留下如实的 run_failed 痕迹",
        )

        # 从未开始的：落 FAILED，但 started_at **保持 NULL**——绝不用"现在"伪造
        # 一个开始时刻（否则快照会把这个 run 说成"刚刚开始过"）。
        swept_never = self.repository.get_run(never_started.id)
        self.assertIs(RunStatus.FAILED, swept_never.status)
        self.assertIsNone(never_started.started_at)
        self.assertIsNone(swept_never.started_at)
        self.assertEqual(SWEEP_ERROR_CODE, swept_never.error_code)
        self.assertEqual(SWEEP_NEVER_STARTED_MESSAGE, swept_never.safe_message)
        self.assertTrue(
            any(
                event.event_type == "run_failed"
                for event in self.repository.list_runtime_events(never_started.id)
            )
        )

        # 幂等：终态行不在清扫范围里，第二次调用是空操作。
        self.assertEqual((), sweep_interrupted_v2_runs(self.repository))

        # 终态行不再是"在飞运行"：恢复卡片不再假装它还等着用户决定。
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=_SnapshotOnlyProvider(),
            tool_registry=ToolRegistry(),
            model="scripted-model",
            max_output_tokens=128,
        )
        self.assertEqual(
            (),
            gateway.snapshot(conversation.id)["interruptedRuns"],
        )

    def test_sweep_boundary_protects_runs_started_after_the_process(self) -> None:
        """守卫（双向）：保护来自**时间边界**，而边界之外的行照样会被清。"""
        ticks = _TickClock()
        repository = SqliteRuntimeV2Repository(self.database, clock=ticks.now)
        _, leftover = self._run(start=True, repository=repository)  # 上一进程的遗物
        # "本进程启动"：时钟往前拨 10 秒。
        ticks.set("2026-01-01T00:00:10.000Z")
        process_start = ticks.now()
        _, live = self._run(start=True, repository=repository)  # 本进程开始的 run

        swept = sweep_interrupted_v2_runs(
            repository, started_before=process_start
        )
        self.assertEqual(
            [leftover.id],
            [run.id for run in swept],
            "边界之前的遗物要清，边界之后的在跑 run 一个都不能碰",
        )
        self.assertIs(RunStatus.RUNNING, repository.get_run(live.id).status)

        # 反向：把边界挪到这条 run **之后**（= 站到未来某次重启的视角），同一条行
        # 确实会被清——说明上一轮它没被清是**边界**在起作用，而不是清扫没生效。
        swept_again = sweep_interrupted_v2_runs(
            repository, started_before="2030-01-01T00:00:00.000Z"
        )
        self.assertEqual([live.id], [run.id for run in swept_again])


class SweepSnapshotTest(_SweepTestCase):
    def test_snapshot_stops_reporting_running_after_the_sweep(self) -> None:
        """验收 ③ 的后端依据：清扫后 `runningRunId` 为 None（前端据此退出重连）。

        清扫前快照说"还在跑"（`runningRunId` 由数据库状态推导，与进程内是否真有
        执行者无关）→ `runtimeController` 每 800ms 重连；清扫后它落终态，
        重连循环的退出条件成立，且 `runState` 如实说明中断原因。
        """
        conversation, run = self._run(start=True)
        gateway = RuntimeV2SessionGateway(
            chat_repository=self.chat_repository,
            repository=self.repository,
            provider=_SnapshotOnlyProvider(),
            tool_registry=ToolRegistry(),
            model="scripted-model",
            max_output_tokens=128,
        )
        before = gateway.snapshot(conversation.id)
        self.assertEqual(run.id, before["runningRunId"])

        self.assertEqual(
            [run.id],
            [item.id for item in sweep_interrupted_v2_runs(self.repository)],
        )

        after = gateway.snapshot(conversation.id)
        self.assertIsNone(
            after["runningRunId"],
            "清扫后快照不许再报 running，否则前端会继续无限重连",
        )
        self.assertEqual("failed", after["runState"]["status"])
        self.assertEqual(SWEEP_ERROR_CODE, after["runState"]["errorCode"])
        self.assertEqual(SWEEP_SAFE_MESSAGE, after["runState"]["safeMessage"])
        self.assertEqual((), after["interruptedRuns"])


class SweepCallSiteTest(unittest.TestCase):
    """切片 10 终审修复 W-H：调用点守卫必须认**属性调用**与**导入别名**。

    起点 `ec997c5` 只匹配 `ast.Name`（裸名调用）：`run_sweep.sweep_interrupted_v2_runs(...)`
    这种属性调用、或 `from ... import sweep_interrupted_v2_runs as sweep` 之后用别名
    调用，都会被漏检 → 守卫会在"实际有两处调用点"时**假绿**。
    """

    @staticmethod
    def _target_names(tree: ast.Module) -> set[str]:
        """本模块对目标函数的可见名字：原函数名 + 可能的导入别名。"""
        names = {"sweep_interrupted_v2_runs"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "sweep_interrupted_v2_runs":
                        names.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(".run_sweep"):
                        names.add(f"{alias.asname or 'run_sweep'}.sweep_interrupted_v2_runs")
        return names

    @classmethod
    def _find_call_sites(cls, tree: ast.Module) -> list[ast.Call]:
        """找出对目标函数的所有调用：裸名、别名、以及任何属性调用。

        属性调用**不看接收者**：`run_sweep.sweep_interrupted_v2_runs(...)`、
        `x.sweep_interrupted_v2_runs(...)`、`self.sweep_interrupted_v2_runs(...)`
        一律算命中——保守方向（宁可误报也不能漏报），因为漏报会让"只有一处调用点"
        这条安全前提悄悄失效。
        """
        names = cls._target_names(tree)
        found: list[ast.Call] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in names:
                found.append(node)
            elif (
                isinstance(func, ast.Attribute)
                and func.attr == "sweep_interrupted_v2_runs"
            ):
                found.append(node)
        return found

    def test_matcher_catches_attribute_calls_and_aliases(self) -> None:
        """守卫本身先能被证伪：三种写法都必须被认出来（否则守卫是假绿）。"""
        module = ast.parse(
            "import endless_task.runtime_v2.run_sweep as rs\n"
            "from endless_task.runtime_v2.run_sweep import (\n"
            "    sweep_interrupted_v2_runs as sweep,\n"
            ")\n"
            "def a():\n"
            "    sweep_interrupted_v2_runs(repository)\n"
            "def b():\n"
            "    rs.sweep_interrupted_v2_runs(repository)\n"
            "def c():\n"
            "    sweep(repository)\n"
        )
        self.assertEqual(3, len(self._find_call_sites(module)))
        # 只是**提到**函数名（不是调用）不算调用点
        aside = ast.parse("from x import sweep_interrupted_v2_runs\nalias = sweep_interrupted_v2_runs\n")
        self.assertEqual([], self._find_call_sites(aside))

    def test_sweep_is_only_called_from_the_api_lifespan_startup(self) -> None:
        package_root = Path(endless_task.__file__).parent
        call_sites: list[tuple[Path, ast.Module, ast.Call]] = []
        for path in sorted(package_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in self._find_call_sites(tree):
                call_sites.append((path, tree, node))
        self.assertEqual(
            1,
            len(call_sites),
            "清扫必须有且只有一处调用点（有周期调用者就会清掉在跑的 run）",
        )
        path, tree, call = call_sites[0]
        self.assertEqual("api/app.py", str(path.relative_to(package_root)))
        lifespan = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        )
        yield_lines = [
            node.lineno
            for node in ast.walk(lifespan)
            if isinstance(node, (ast.Yield, ast.YieldFrom))
        ]
        self.assertTrue(yield_lines)
        self.assertLess(lifespan.lineno, call.lineno)
        self.assertLess(
            call.lineno,
            min(yield_lines),
            "清扫必须在 yield（= 开始服务请求）之前，且不在任何周期任务里",
        )
        # 它是 lifespan 的**直接语句** → 不可能嵌在任何循环/周期任务体里。
        self.assertTrue(
            any(
                isinstance(statement, ast.Expr) and statement.value is call
                for statement in lifespan.body
            ),
            "清扫必须是 lifespan 的直接语句，不能嵌进循环或后台任务体里",
        )

    def test_api_process_is_single_worker(self) -> None:
        """切片 10 终审修复 W-I：清扫的"单进程"前提必须被守卫住。

        前提：整条"本进程不可能持有"的推理只在本 API 进程**独占**这张库时成立。
        多 worker（或多实例共享同一个 SQLite 文件）会让每个 worker 启动各清一次，
        把**别的 worker 正在跑的 run** 当成"上个进程的遗物"落 FAILED。
        这里钉住"启动段没有配置多 worker/多进程"，并顺便确认代码里那份机器可读声明
        还在（免得有人把前提当注释删掉）。
        """
        package_root = Path(endless_task.__file__).parent
        main_path = package_root / "api" / "main.py"
        tree = ast.parse(main_path.read_text(encoding="utf-8"))
        worker_values: list[object] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "run" and name != "uvicorn":
                # `uvicorn.run(...)` / `run(...)`
                if not (name == "run"):
                    continue
            for keyword in node.keywords:
                if keyword.arg == "workers":
                    worker_values.append(ast.literal_eval(keyword.value))
        self.assertEqual(
            [],
            [value for value in worker_values if value not in (None, 1)],
            "启动清扫的单进程前提被破坏了：uvicorn 不许配置 workers>1（见 run_sweep 模块 docstring）",
        )
        sweep_source = (
            package_root / "runtime_v2" / "run_sweep.py"
        ).read_text(encoding="utf-8")
        self.assertIn("**前提：单进程**", sweep_source)
        from endless_task.runtime_v2.run_sweep import SINGLE_PROCESS_ASSUMPTION

        self.assertIn("多 worker", SINGLE_PROCESS_ASSUMPTION)


class StartupSweepIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """在**真的 lifespan** 里验证：启动清遗物，进程活着时在跑的 run 不受影响。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # LIFO：先注册 = 最后执行 → 先 await 掉应用，再删临时目录。
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "api.db"
        database = Database(self.db_path)
        database.initialize()
        chat_repository = SqliteChatRepository(database)
        repository = SqliteRuntimeV2Repository(database)
        # "上一个进程"跑到一半被杀：活动态的 run 行留在库里。
        self.conversation, self.leftover = _create_run(
            chat_repository, repository, start=True
        )

    async def _client(self, provider):
        settings = AppSettings(
            database_path=self.db_path,
            memory_proposals_enabled=False,
            knowledge_proposals_enabled=False,
            artifact_proposals_enabled=False,
            scheduler_enabled=False,
            knowledge_decay_enabled=False,
            notifications_enabled=False,
            task_run_review_enabled=False,
            heartbeat_seconds=0.05,
        )
        app = create_app(settings=settings, provider=provider)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        self.addAsyncCleanup(client.aclose)
        return client, app

    async def test_startup_sweeps_leftover_but_leaves_a_live_run_alone(self) -> None:
        provider = FakeProvider(chunks=("你好", "世界"), pause_after_chunks=0)
        client, app = await self._client(provider)
        repository = app.state.container.runtime_v2_repository

        # 1) 启动清扫：上一个进程的遗物已落终态、如实带原因。
        leftover = repository.get_run(self.leftover.id)
        self.assertIs(RunStatus.FAILED, leftover.status)
        self.assertEqual(SWEEP_ERROR_CODE, leftover.error_code)
        self.assertEqual(SWEEP_SAFE_MESSAGE, leftover.safe_message)
        snapshot = (
            await client.get(
                f"/api/v2/conversations/{self.conversation.id}/snapshot"
            )
        ).json()
        self.assertIsNone(snapshot["runningRunId"])

        # 2) 进程活着时开始的 run：不会被任何清扫碰（本进程之后的行不在范围里，
        #    且清扫只在启动段调用过一次）。
        response = await client.post(
            f"/api/v2/conversations/{self.conversation.id}/messages",
            headers={"Idempotency-Key": "t2-live-run"},
            json={"content": "长活"},
        )
        self.assertEqual(202, response.status_code)
        run_id = response.json()["runId"]
        await asyncio.wait_for(provider.paused.wait(), 3.0)
        # 让"任何周期性清扫"都有机会出现（有的话这里必红）。
        await asyncio.sleep(0.3)
        self.assertIs(RunStatus.RUNNING, repository.get_run(run_id).status)
        live_snapshot = (
            await client.get(
                f"/api/v2/conversations/{self.conversation.id}/snapshot"
            )
        ).json()
        self.assertEqual(run_id, live_snapshot["runningRunId"])
        # 3) 让它自己跑完（证明它真的是活的，不是被清扫冻住的尸体）。
        provider.resume()
        await _wait_until(
            lambda: repository.get_run(run_id).status is RunStatus.COMPLETED
        )


if __name__ == "__main__":
    unittest.main()
