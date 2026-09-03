"""AP-307: container backend command building and (runtime-gated) execution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import AgentPlatformError
from endless_task.execution_env import (
    ContainerExecutionBackend,
    ExecutionPolicy,
    NetworkMode,
    build_container_command,
    probe_container_runtime,
)
from endless_task.runtime_ledger import TraceContext

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")
RUNTIME = "docker"


class CommandBuilderTest(unittest.TestCase):
    def test_command_mounts_workspace_and_disables_network_by_default(self) -> None:
        command = build_container_command(
            workspace_root="/tmp/ws",
            image="alpine:3.20",
            network_mode=NetworkMode.DENY,
        )
        self.assertEqual("run", command[0])
        self.assertIn("--network", command)
        self.assertEqual("none", command[command.index("--network") + 1])
        self.assertIn("-v", command)
        volume = command[command.index("-v") + 1]
        self.assertTrue(volume.startswith("/tmp/ws:"))
        self.assertIn("/workspace", volume)
        self.assertEqual("alpine:3.20", command[-1])

    def test_allow_all_maps_to_bridge_network(self) -> None:
        command = build_container_command(
            workspace_root="/tmp/ws",
            image="alpine:3.20",
            network_mode=NetworkMode.ALLOW_ALL,
        )
        self.assertEqual("bridge", command[command.index("--network") + 1])

    def test_relative_cwd_is_workspace_scoped(self) -> None:
        command = build_container_command(
            workspace_root="/tmp/ws",
            image="alpine",
            cwd="sub/dir",
        )
        self.assertTrue(
            command[command.index("-w") + 1].endswith("/workspace/sub/dir")
        )

    def test_invalid_network_mode_fails_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            build_container_command(workspace_root="/tmp/ws", image="alpine", network_mode="x")


class ContainerLiveTest(unittest.IsolatedAsyncioTestCase):
    """Live container checks; skipped when no container runtime is present."""

    def setUp(self) -> None:
        if not probe_container_runtime(RUNTIME):
            self.skipTest("container runtime unavailable in this environment")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            network_mode=NetworkMode.DENY,
            timeout_seconds=30.0,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_missing_runtime_fails_closed(self) -> None:
        backend = ContainerExecutionBackend(image="alpine:3.20", runtime="")
        from endless_task.execution_env import ProcessRequest

        with self.assertRaises(AgentPlatformError) as caught:
            await backend.run_process(
                ProcessRequest(
                    effect_id="e",
                    tool_call_id="c",
                    argv=("/bin/sh", "-c", "echo hi"),
                    cwd=".",
                    policy=self.policy,
                    trace=TRACE,
                    requested_at="2026-09-03T00:00:00Z",
                )
            )
        self.assertEqual("container_unavailable", caught.exception.code)


class MissingRuntimeConstructionTest(unittest.TestCase):
    def test_construction_requires_an_image(self) -> None:
        with self.assertRaises(AgentPlatformError):
            ContainerExecutionBackend(image="")


if __name__ == "__main__":
    unittest.main()
