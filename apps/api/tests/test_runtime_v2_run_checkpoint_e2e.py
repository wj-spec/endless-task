"""M3B run-level E2E: real v2 runs auto-checkpoint the workspace (方案 A).

A run whose fs tools write into a bound workspace must snapshot that
workspace before its first side effect (RunCheckpointCoordinator wired in
the API composition root) and ledger each mediated mutation, so a failed run
can be restored without ever overwriting user edits:

- ``test_run_write_auto_checkpoints_workspace_and_guarded_restore`` proves a
  run's ``write_workspace_file`` produces a run checkpoint + ledger row, and
  that restore classifies an intact agent change as revertible while a
  user-edited file (agent touched or not) is skipped.
- ``test_two_runs_share_workspace_independent_checkpoints`` proves each run
  snapshots independently and restoring the latest run reverts only its own
  intact agent change.

Restore is exercised through ``app.state.container.run_checkpoint_coordinator``
— the composition-root instance the tools actually used — so the test covers
the real wiring end to end.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.execution_env.checkpoint import read_manifest
from endless_task.runtime import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import RunStatus
from tests.fixtures.v2_client import (
    send_message,
    wait_for_run_terminal,
)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class SwappableProvider:
    """Delegates stream() to a current inner provider so one app can run
    several scripted runs (the app's provider is fixed at construction)."""

    name = "swappable"

    def __init__(self) -> None:
        self._inner = None

    def set(self, provider) -> None:
        self._inner = provider

    async def stream(self, request, cancellation_token):
        if self._inner is None:
            raise AssertionError("no inner provider configured")
        async for item in self._inner.stream(request, cancellation_token):
            yield item


class ScriptedWriteProvider:
    """First round: one write_workspace_file call; then wrap up on tool result."""

    name = "scripted-write"

    def __init__(self, *, path: str, content: str, follow_up: str = "已写入。") -> None:
        self._path = path
        self._content = content
        self._follow_up = follow_up
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_write_1",
                name="write_workspace_file",
                arguments={"path": self._path, "content": self._content},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        yield ProviderTextDelta(self._follow_up)
        yield ProviderCompleted(finish_reason="stop")


class FailAfterWriteProvider:
    """Writes once, then the provider errors on the next request: a real
    mid-run execution fault (run FAILED after side effects)."""

    name = "scripted-fail-after-write"

    def __init__(self, *, path: str, content: str) -> None:
        self._path = path
        self._content = content
        self.requests = []

    async def stream(self, request, cancellation_token):
        cancellation_token.raise_if_cancelled()
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ProviderToolCall(
                id="call_write_1",
                name="write_workspace_file",
                arguments={"path": self._path, "content": self._content},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        raise ProviderError(
            "provider_upstream_error",
            "上游服务暂时不可用（E2E 注入）。",
            retryable=False,
        )


class RunCheckpointRunE2ETest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._root = Path(self._tmp.name) / "ws"
        self._root.mkdir()

    async def asyncTearDown(self) -> None:
        for client, lifespan in getattr(self, "_apps", []):
            await client.aclose()
            await lifespan.__aexit__(None, None, None)
        self._tmp.cleanup()

    async def _app(
        self, provider=None, *, auto_restore: bool = True
    ) -> tuple[httpx.AsyncClient, object, str]:
        index = len(getattr(self, "_apps", []))
        swappable = SwappableProvider()
        if provider is not None:
            swappable.set(provider)
        self._swappable = swappable
        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / f"e2e-{index}.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                run_auto_restore_enabled=auto_restore,
            ),
            provider=swappable,
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        apps = getattr(self, "_apps", [])
        apps.append((client, lifespan))
        self._apps = apps
        return client, app, str(self._root)

    async def _bound_conversation(
        self, client: httpx.AsyncClient, *, workspace_id: str | None = None
    ) -> str:
        if workspace_id is None:
            created = await client.post(
                "/workspaces",
                json={"name": "E2E区", "rootPath": str(self._root)},
            )
            self.assertEqual(201, created.status_code)
            workspace_id = created.json()["workspace"]["id"]
        conversation = await client.post(
            "/conversations", json={"workspaceId": workspace_id}
        )
        return conversation.json()["id"]

    async def _run_once(
        self,
        client: httpx.AsyncClient,
        app: object,
        provider,
        *,
        key: str,
        workspace_id: str | None = None,
        expected: RunStatus = RunStatus.COMPLETED,
    ) -> tuple[str, str]:
        if provider is not None:
            self._swappable.set(provider)
        conversation_id = await self._bound_conversation(
            client, workspace_id=workspace_id
        )
        handle = await send_message(
            client, conversation_id, "开始", idempotency_key=key
        )
        run_id = handle["runId"]
        status = await wait_for_run_terminal(app.state.container, run_id)
        self.assertEqual(expected, status)
        return conversation_id, run_id

    def _ledger_path(self, app: object) -> Path:
        return (
            Path(app.state.container.settings.database_path).parent
            / "file-mutations.jsonl"
        )

    async def test_run_write_auto_checkpoints_workspace_and_guarded_restore(
        self,
    ) -> None:
        (self._root / "task.txt").write_text("user-seed", encoding="utf-8")
        (self._root / "keep.txt").write_text("keep-seed", encoding="utf-8")
        provider = ScriptedWriteProvider(path="task.txt", content="agent-v1")
        client, app, _ = await self._app(provider)
        _, run_id = await self._run_once(client, app, provider, key="k-cp")

        # The write landed and a run checkpoint + ledger row were produced.
        self.assertEqual(
            "agent-v1",
            (self._root / "task.txt").read_text(encoding="utf-8"),
        )
        coordinator = app.state.container.run_checkpoint_coordinator
        self.assertIsNotNone(coordinator)
        self.assertEqual((run_id,), coordinator.tracked_run_ids())
        ref = coordinator.checkpoint_for(
            run_id=run_id, workspace_root=str(self._root)
        )
        self.assertIsNotNone(ref)
        manifest = read_manifest(
            Path(app.state.container.settings.database_path).parent / "checkpoints",
            ref.checkpoint_id,
        )
        manifest_sha = dict(manifest.entries)
        self.assertEqual(
            _sha256_bytes(b"user-seed"),
            manifest_sha["task.txt"],  # pre-run state captured
        )
        ledger_text = self._ledger_path(app).read_text(encoding="utf-8")
        self.assertIn("task.txt", ledger_text)
        self.assertIn(_sha256_bytes(b"agent-v1"), ledger_text)

        # User edits both files after the run: restore must touch neither.
        (self._root / "task.txt").write_text("user-latest", encoding="utf-8")
        (self._root / "keep.txt").write_text("user-keep", encoding="utf-8")
        plan = coordinator.plan_restore(
            run_id=run_id, workspace_root=str(self._root)
        )
        self.assertEqual((), plan.would_apply)
        self.assertIn("task.txt", plan.user_modified_skipped)
        self.assertIn("keep.txt", plan.user_modified_skipped)
        restored, skipped = coordinator.apply_restore(
            run_id=run_id, workspace_root=str(self._root)
        )
        self.assertEqual((), restored)
        self.assertIn("task.txt", skipped)
        self.assertEqual(
            "user-latest",
            (self._root / "task.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "user-keep",
            (self._root / "keep.txt").read_text(encoding="utf-8"),
        )

    async def test_restore_reverts_intact_agent_change_after_run(self) -> None:
        (self._root / "base.txt").write_text("base-seed", encoding="utf-8")
        provider = ScriptedWriteProvider(path="base.txt", content="agent-r1")
        client, app, _ = await self._app(provider)
        _, run_id = await self._run_once(client, app, provider, key="k-revert")
        coordinator = app.state.container.run_checkpoint_coordinator

        restored, skipped = coordinator.apply_restore(
            run_id=run_id, workspace_root=str(self._root)
        )
        self.assertIn("base.txt", restored)
        self.assertNotIn("base.txt", skipped)
        self.assertEqual(
            "base-seed",
            (self._root / "base.txt").read_text(encoding="utf-8"),
        )
        # Restore is not idempotent-by-accident: current == checkpoint now.
        restored_again, _ = coordinator.apply_restore(
            run_id=run_id, workspace_root=str(self._root)
        )
        self.assertEqual((), restored_again)

    async def test_two_runs_share_workspace_independent_checkpoints(self) -> None:
        (self._root / "plan.txt").write_text("plan-seed", encoding="utf-8")
        (self._root / "todo.txt").write_text("todo-seed", encoding="utf-8")
        first = ScriptedWriteProvider(path="plan.txt", content="plan-r1")
        client, app, _ = await self._app(first)
        created = await client.post(
            "/workspaces",
            json={"name": "E2E区", "rootPath": str(self._root)},
        )
        self.assertEqual(201, created.status_code)
        workspace_id = created.json()["workspace"]["id"]
        _, run_1 = await self._run_once(
            client, app, first, key="k-r1", workspace_id=workspace_id
        )
        second = ScriptedWriteProvider(path="todo.txt", content="todo-r2")
        _, run_2 = await self._run_once(
            client, app, second, key="k-r2", workspace_id=workspace_id
        )
        self.assertNotEqual(run_1, run_2)
        self.assertEqual("plan-r1", (self._root / "plan.txt").read_text(encoding="utf-8"))
        self.assertEqual("todo-r2", (self._root / "todo.txt").read_text(encoding="utf-8"))

        coordinator = app.state.container.run_checkpoint_coordinator
        self.assertEqual({run_1, run_2}, set(coordinator.tracked_run_ids()))
        ref_1 = coordinator.checkpoint_for(
            run_id=run_1, workspace_root=str(self._root)
        )
        ref_2 = coordinator.checkpoint_for(
            run_id=run_2, workspace_root=str(self._root)
        )
        self.assertIsNotNone(ref_1)
        self.assertIsNotNone(ref_2)
        self.assertNotEqual(ref_1.checkpoint_id, ref_2.checkpoint_id)

        # Restoring the latest run reverts only its own intact agent change;
        # run-1's edit stays (its content matches run-2's checkpoint).
        restored, _ = coordinator.apply_restore(
            run_id=run_2, workspace_root=str(self._root)
        )
        self.assertIn("todo.txt", restored)
        self.assertNotIn("plan.txt", restored)
        self.assertEqual("plan-r1", (self._root / "plan.txt").read_text(encoding="utf-8"))
        self.assertEqual("todo-seed", (self._root / "todo.txt").read_text(encoding="utf-8"))


    async def test_failed_run_auto_restores_its_workspace(self) -> None:
        # 方案 A 兑现：run 写文件后 provider 故障 → run FAILED，工作区自动回滚
        # 到 checkpoint（写前状态），守卫语义不变。
        (self._root / "task.txt").write_text("user-seed", encoding="utf-8")
        (self._root / "keep.txt").write_text("keep-seed", encoding="utf-8")
        provider = FailAfterWriteProvider(path="task.txt", content="agent-v1")
        client, app, _ = await self._app(provider)
        _, run_id = await self._run_once(client, app, provider, key="k-fail", expected=RunStatus.FAILED)
        coordinator = app.state.container.run_checkpoint_coordinator
        self.assertIn(run_id, coordinator.tracked_run_ids())
        # Auto-restore reverted the agent write; user-owned file untouched.
        self.assertEqual(
            "user-seed",
            (self._root / "task.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "keep-seed",
            (self._root / "keep.txt").read_text(encoding="utf-8"),
        )
        # Audit: a run_auto_restored runtime event landed on the run.
        events = app.state.container.runtime_v2_repository.list_runtime_events(
            run_id
        )
        self.assertTrue(
            any(e.event_type == "run_auto_restored" for e in events),
            f"no run_auto_restored event in {[e.event_type for e in events]}",
        )

    async def test_failed_run_without_side_effects_is_noop(self) -> None:
        # run 尚未写过任何工作区文件就失败：无 checkpoint，自动恢复 no-op。
        class FailFirstProvider:
            name = "scripted-fail-first"

            async def stream(self, request, cancellation_token):
                cancellation_token.raise_if_cancelled()
                raise ProviderError(
                    "provider_upstream_error",
                    "上游服务暂时不可用（E2E 注入）。",
                    retryable=False,
                )

        (self._root / "task.txt").write_text("user-seed", encoding="utf-8")
        client, app, _ = await self._app(FailFirstProvider())
        _, run_id = await self._run_once(client, app, None, key="k-fail0", expected=RunStatus.FAILED)
        coordinator = app.state.container.run_checkpoint_coordinator
        self.assertNotIn(run_id, coordinator.tracked_run_ids())
        self.assertEqual(
            "user-seed",
            (self._root / "task.txt").read_text(encoding="utf-8"),
        )

    async def test_gate_off_keeps_agent_content_on_failure(self) -> None:
        # ENDLESS_TASK_RUN_AUTO_RESTORE=0：失败后保留部分状态（不自动回滚）。
        (self._root / "task.txt").write_text("user-seed", encoding="utf-8")
        provider = FailAfterWriteProvider(path="task.txt", content="agent-v1")
        client, app, _ = await self._app(provider, auto_restore=False)
        _, _ = await self._run_once(client, app, provider, key="k-gateoff", expected=RunStatus.FAILED)
        self.assertEqual(
            "agent-v1",
            (self._root / "task.txt").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
