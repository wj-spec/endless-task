"""M3B slice F: unattended-executor tool enforcement (execution backend seam).

Covers three layers:

1. Tool backend mode — Write/Delete with ``execution_backend`` route the
   mutation through ExecutionEnvironment while keeping run-level checkpoint,
   run-scoped ledger rows, audit log and ToolResult identical to the direct
   path; guarded restore still works on the enforced mutation.
2. Enforcement registry view — EnforcingToolRegistry swaps only configured
   names, delegates everything else, and is inert when disabled.
3. Composition boot — create_app with ENDLESS_TASK_EXECUTION_BACKEND values
   wires the unattended executor registry to enforced tools (mode local ==
   container for file mutations), and "" keeps the direct tools.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator

def _container_available() -> bool:
    from endless_task.execution_env import probe_container_runtime

    import shutil

    runtime = shutil.which("docker") or shutil.which("colima")
    return bool(runtime) and probe_container_runtime(runtime)


from endless_task.tooling import ToolCall, ToolCallStatus, ToolRegistry
from endless_task.workspace_runtime import WorkspaceBinding
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.enforcement import (
    EnforcingToolRegistry,
    ToolEnforcement,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.workspace_runtime.fs_tools import (
    DeleteWorkspaceFileTool,
    WriteWorkspaceFileTool,
)


class _BindingResolver:
    def __init__(self, root: Path) -> None:
        # Real bindings hold resolved roots; mirror that so relative_path
        # computations match production.
        self._root = Path(root).expanduser().resolve()

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        return WorkspaceBinding(workspace_id="ws_1", root=self._root)


def _call(**arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="run_enforced",
        tool_name="tool",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-05T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


class _DummyTool:
    def __init__(self, name: str) -> None:
        self._name = name
        self.definition = _definition(name)

    async def execute(self, call, token):  # pragma: no cover - not invoked
        raise AssertionError("not invoked")


def _definition(name: str):
    from endless_task.tooling import ToolDefinition

    return ToolDefinition(
        name=name,
        description=name,
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )


class ToolBackendModeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.effect_log = EffectLog(self.base / "logs")
        # Shared ledger: the enforcement backend appends its rows here and the
        # coordinator reads the same file for run-scoped restore attribution.
        self.ledger = FileMutationLedger(self.base / "ledger.jsonl")
        self.coordinator = RunCheckpointCoordinator(
            store_root=self.base / "checkpoints",
            ledger=self.ledger,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _backend(self):
        from endless_task.execution_env import LocalExecutionBackend

        return LocalExecutionBackend(ledger=self.ledger)

    async def test_enforced_write_matches_direct_semantics(self) -> None:
        resolver = _BindingResolver(self.root)
        direct = WriteWorkspaceFileTool(
            resolver, self.effect_log, checkpoint_coordinator=self.coordinator
        )
        enforced = WriteWorkspaceFileTool(
            resolver,
            self.effect_log,
            checkpoint_coordinator=self.coordinator,
            execution_backend=self._backend(),
        )
        (self.root / "a.txt").write_text("orig", encoding="utf-8")
        direct_result = await direct.execute(
            _call(path="a.txt", content="direct-v1"), _token()
        )
        enforced_result = await enforced.execute(
            _call(path="b.txt", content="enforced-v1"), _token()
        )
        self.assertEqual("direct-v1", (self.root / "a.txt").read_text(encoding="utf-8"))
        self.assertEqual("enforced-v1", (self.root / "b.txt").read_text(encoding="utf-8"))
        # Same message shape as the direct path (length differs by content).
        self.assertTrue(
            enforced_result.content.startswith("已写入工作区文件：b.txt（")
            and enforced_result.content.endswith("字符）。"),
            enforced_result.content,
        )
        import hashlib

        self.assertEqual(
            hashlib.sha256(b"enforced-v1").hexdigest(),
            enforced_result.structured_content["effect"]["sha256"],
        )
        # Ledger row carries the run id (run-scoped restore attribution).
        ledger_text = self.ledger._path.read_text(encoding="utf-8")
        self.assertIn("run_enforced", ledger_text)
        self.assertIn("b.txt", ledger_text)
        # Run checkpoint existed for the enforced write (same coordinator).
        ref = self.coordinator.checkpoint_for(
            run_id="run_enforced", workspace_root=str(self.root)
        )
        self.assertIsNotNone(ref)

    async def test_enforced_write_restores_like_direct(self) -> None:
        resolver = _BindingResolver(self.root)
        tool = WriteWorkspaceFileTool(
            resolver,
            self.effect_log,
            checkpoint_coordinator=self.coordinator,
            execution_backend=self._backend(),
        )
        (self.root / "a.txt").write_text("orig", encoding="utf-8")
        await tool.execute(_call(path="a.txt", content="agent-v1"), _token())
        self.assertEqual("agent-v1", (self.root / "a.txt").read_text(encoding="utf-8"))
        restored, skipped = self.coordinator.apply_restore(
            run_id="run_enforced", workspace_root=str(self.root)
        )
        self.assertIn("a.txt", restored)
        self.assertNotIn("a.txt", skipped)
        self.assertEqual("orig", (self.root / "a.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(
        _container_available(),
        "container runtime not available (colima/docker required)",
    )
    async def test_enforced_write_via_container_backend_live(self) -> None:
        # F2 live：operator 选 container 时，enforced 写经 ContainerExecutionBackend
        # （文件 mutation 委托 workspace 受限 local，进程隔离留给未来 shell 面）。
        from endless_task.execution_env import ContainerExecutionBackend

        resolver = _BindingResolver(self.root)
        tool = WriteWorkspaceFileTool(
            resolver,
            self.effect_log,
            checkpoint_coordinator=self.coordinator,
            execution_backend=ContainerExecutionBackend(
                image="alpine:3.20", ledger=self.ledger
            ),
        )
        (self.root / "a.txt").write_text("orig", encoding="utf-8")
        await tool.execute(_call(path="a.txt", content="container-v1"), _token())
        self.assertEqual(
            "container-v1", (self.root / "a.txt").read_text(encoding="utf-8")
        )
        restored, _ = self.coordinator.apply_restore(
            run_id="run_enforced", workspace_root=str(self.root)
        )
        self.assertIn("a.txt", restored)
        self.assertEqual("orig", (self.root / "a.txt").read_text(encoding="utf-8"))

    async def test_enforced_delete_removes_and_restores(self) -> None:
        resolver = _BindingResolver(self.root)
        tool = DeleteWorkspaceFileTool(
            resolver,
            self.effect_log,
            checkpoint_coordinator=self.coordinator,
            execution_backend=self._backend(),
        )
        (self.root / "gone.txt").write_text("content", encoding="utf-8")
        result = await tool.execute(_call(path="gone.txt"), _token())
        self.assertIn("gone.txt", result.content)
        self.assertFalse((self.root / "gone.txt").exists())
        restored, skipped = self.coordinator.apply_restore(
            run_id="run_enforced", workspace_root=str(self.root)
        )
        self.assertIn("gone.txt", restored)
        self.assertTrue((self.root / "gone.txt").exists())
        self.assertEqual(
            "content", (self.root / "gone.txt").read_text(encoding="utf-8")
        )


class EnforcingToolRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_swaps_only_configured_names(self) -> None:
        inner = ToolRegistry()
        inner.register(_DummyTool("keep"))
        inner.register(_DummyTool("swap_me"))
        enforcement = ToolEnforcement()
        view = EnforcingToolRegistry(inner, enforcement)

        # Disabled: everything falls back.
        self.assertIs(inner.resolve("swap_me"), view.resolve("swap_me"))

        replacement = _DummyTool("swap_me")
        enforcement.configure(replacements={"swap_me": replacement})
        self.assertIs(replacement, view.resolve("swap_me"))
        self.assertIs(inner.resolve("keep"), view.resolve("keep"))
        self.assertEqual(
            tuple(d.name for d in view.definitions()),
            tuple(d.name for d in inner.definitions()),
        )

    async def test_unknown_name_raises_like_inner(self) -> None:
        inner = ToolRegistry()
        view = EnforcingToolRegistry(inner, ToolEnforcement())
        with self.assertRaises(Exception):
            view.resolve("missing")

    async def test_inner_registrations_visible_through_view(self) -> None:
        inner = ToolRegistry()
        view = EnforcingToolRegistry(inner, ToolEnforcement())
        inner.register(_DummyTool("late"))
        self.assertEqual("late", view.resolve("late").definition.name)


class EnforcementCompositionTest(unittest.IsolatedAsyncioTestCase):
    """create_app boots in every mode; mode wires the unattended registry."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    async def _build(self, mode: str):
        import httpx

        from endless_task.api import AppSettings, create_app
        from endless_task.runtime import (
            ProviderCompleted,
            ProviderTextDelta,
        )

        class QuietProvider:
            name = "quiet"

            async def stream(self, request, cancellation_token):
                yield ProviderTextDelta("ok")
                yield ProviderCompleted(finish_reason="stop")

        app = create_app(
            settings=AppSettings(
                database_path=Path(self._tmp.name) / f"app-{mode or 'off'}.db",
                memory_proposals_enabled=False,
                knowledge_proposals_enabled=False,
                artifact_proposals_enabled=False,
                task_proposals_enabled=False,
                execution_backend_mode=mode,
            ),
            provider=QuietProvider(),
        )
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )
        return client, lifespan, app

    async def _close(self, client, lifespan) -> None:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)

    async def test_mode_off_keeps_direct_tools(self) -> None:
        client, lifespan, app = await self._build("")
        container = app.state.container
        self.assertEqual("", container.execution_backend_mode)
        self.assertIsNone(container.unattended_tool_registry)
        write_tool = container.tool_registry.resolve("write_workspace_file")
        self.assertIsNone(getattr(write_tool, "_execution_backend", None))
        await self._close(client, lifespan)

    async def test_mode_local_wires_enforced_unattended_tools(self) -> None:
        client, lifespan, app = await self._build("local")
        container = app.state.container
        self.assertEqual("local", container.execution_backend_mode)
        view = container.unattended_tool_registry
        self.assertIsNotNone(view)
        enforced_write = view.resolve("write_workspace_file")
        self.assertIsNotNone(getattr(enforced_write, "_execution_backend", None))
        enforced_delete = view.resolve("delete_workspace_file")
        self.assertIsNotNone(getattr(enforced_delete, "_execution_backend", None))
        # Interactive registry is untouched.
        interactive_write = container.tool_registry.resolve("write_workspace_file")
        self.assertIsNone(getattr(interactive_write, "_execution_backend", None))
        await self._close(client, lifespan)

    async def test_mode_container_wires_same_file_mutation_enforcement(self) -> None:
        # Container's isolation difference applies to process execution; file
        # mutations are workspace-confined in both backends (documented), so
        # "container" opts into the same enforcement seam as "local".
        client, lifespan, app = await self._build("container")
        container = app.state.container
        view = container.unattended_tool_registry
        self.assertIsNotNone(view)
        enforced_write = view.resolve("write_workspace_file")
        self.assertIsNotNone(getattr(enforced_write, "_execution_backend", None))
        await self._close(client, lifespan)

    async def test_default_mode_is_local_after_f2(self) -> None:
        from endless_task.api import AppSettings

        self.assertEqual(
            "local", AppSettings(database_path="x.db").execution_backend_mode
        )

    async def test_illegal_mode_fails_startup(self) -> None:
        # The strict gate lives in env parsing (startup path); illegal values
        # fail like the other strict parsers.
        from endless_task.api.app import _parse_execution_backend_mode

        for illegal in ("docker", "sandbox", "yes-please"):
            with self.assertRaises(ValueError):
                _parse_execution_backend_mode(illegal)
        self.assertEqual("", _parse_execution_backend_mode("0"))
        self.assertEqual("local", _parse_execution_backend_mode("1"))
        self.assertEqual("local", _parse_execution_backend_mode("local"))
        self.assertEqual("container", _parse_execution_backend_mode("container"))


if __name__ == "__main__":
    unittest.main()
