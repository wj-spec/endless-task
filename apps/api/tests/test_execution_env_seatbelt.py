"""AP-306: Seatbelt profile builder and sandboxed process backend."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import AgentPlatformError, EffectOutcome
from endless_task.execution_env import (
    ExecutionPolicy,
    LocalExecutionBackend,
    NetworkMode,
    ProcessRequest,
    SeatbeltBackend,
    build_seatbelt_profile,
    probe_seatbelt,
)
from endless_task.runtime_ledger import TraceContext

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class ProfileBuilderTest(unittest.TestCase):
    def test_profile_denies_by_default_and_allows_workspace_writes(self) -> None:
        profile = build_seatbelt_profile(
            workspace_root="/tmp/ws",
            write_allow_paths=("/tmp/ws/out",),
            network_mode=NetworkMode.DENY,
        )
        self.assertIn("(deny default)", profile)
        self.assertIn('(allow file-write* (subpath "/tmp/ws"))', profile)
        self.assertIn('(allow file-write* (subpath "/tmp/ws/out"))', profile)
        self.assertIn("(deny network*)", profile)

    def test_network_modes_map_to_profile(self) -> None:
        allow = build_seatbelt_profile(
            workspace_root="/tmp/ws",
            network_mode=NetworkMode.ALLOW_ALL,
        )
        self.assertIn("(allow network*)", allow)
        self.assertNotIn("(deny network*)", allow)
        # ALLOW_HOSTS cannot be expressed: denied, never silently opened.
        hosts = build_seatbelt_profile(
            workspace_root="/tmp/ws",
            network_mode=NetworkMode.ALLOW_HOSTS,
        )
        self.assertIn("(deny network*)", hosts)

    def test_invalid_network_mode_fails_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            build_seatbelt_profile(workspace_root="/tmp/ws", network_mode="allow")


class SeatbeltLiveTest(unittest.IsolatedAsyncioTestCase):
    """Live sandbox checks; skipped when sandbox-exec cannot apply (macOS
    without the required permission, e.g. restricted hosts)."""

    def setUp(self) -> None:
        available = probe_seatbelt(SANDBOX_EXEC)
        if not available:
            self.skipTest("sandbox-exec cannot apply a sandbox in this environment")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            network_mode=NetworkMode.DENY,
            timeout_seconds=5.0,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _request(self, argv) -> ProcessRequest:
        return ProcessRequest(
            effect_id="effect_s",
            tool_call_id="call_s",
            argv=tuple(argv),
            cwd=".",
            policy=self.policy,
            trace=TRACE,
            requested_at="2026-09-03T00:00:00Z",
        )

    async def test_sandboxed_process_runs_inside_workspace(self) -> None:
        backend = SeatbeltBackend()
        result = await backend.run_process(
            self._request(["/bin/sh", "-c", "echo inside && touch inside.txt"])
        )
        self.assertEqual(0, result.exit_code)
        self.assertIn("inside", result.stdout)
        self.assertTrue((self.root / "inside.txt").exists())
        self.assertEqual(EffectOutcome.COMMITTED, result.receipt.outcome)
        self.assertEqual("local-seatbelt", result.receipt.backend)

    async def test_write_outside_workspace_is_denied(self) -> None:
        backend = SeatbeltBackend()
        result = await backend.run_process(
            self._request(["/bin/sh", "-c", "touch /tmp/seatbelt_escape_test_file 2>/dev/null; echo done"])
        )
        self.assertEqual(0, result.exit_code)
        self.assertFalse(Path("/tmp/seatbelt_escape_test_file").exists())

    async def test_unavailable_sandbox_fails_closed(self) -> None:
        backend = SeatbeltBackend(sandbox_exec="/nonexistent/sandbox-exec")
        with self.assertRaises(AgentPlatformError) as caught:
            await backend.run_process(self._request(["/bin/sh", "-c", "echo x"]))
        self.assertEqual("process_spawn_failed", caught.exception.code)

    async def test_backend_delegates_file_ops_to_local(self) -> None:
        from endless_task.execution_env import (
            FileMutationOperation,
            FileMutationRequest,
            ReadFileRequest,
        )

        backend = SeatbeltBackend()
        await backend.mutate_file(
            FileMutationRequest(
                effect_id="e1",
                tool_call_id="c1",
                path="note.txt",
                operation=FileMutationOperation.WRITE,
                policy=self.policy,
                trace=TRACE,
                requested_at="2026-09-03T00:00:00Z",
                content="hello seatbelt",
            )
        )
        result = await backend.read_file(
            ReadFileRequest(path="note.txt", policy=self.policy, trace=TRACE)
        )
        self.assertEqual("hello seatbelt", result.content)


class BackendConstructionTest(unittest.TestCase):
    def test_missing_binary_fails_closed_on_execution(self) -> None:
        backend = SeatbeltBackend(sandbox_exec="")
        self.assertFalse(backend._sandbox_exec)


if __name__ == "__main__":
    unittest.main()
