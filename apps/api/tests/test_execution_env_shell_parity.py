"""RS-6 slice 1: shell execution parity evidence (05 §4.5 parity table).

05 §485 declares `run_process` (LocalExecutionBackend) <-> `RunShellTool`/
`run_shell_command` parity (same command + cwd -> same exit/stdout/stderr;
timeout/truncation markers match). This module is the missing direct
two-way parity test proving the ExecutionEnvironment backend can stand in
for the shell host path before any production cutover.

Scope discipline (RS-6 slice 1): no production code changes — these tests
pin current behavior so a later backend-backed cutover can be proven
byte-identical. Shell no-change watchdog / dangerous-command approval
stay in the tool layer (05 §499: they are approval/policy, not backend).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from endless_task.execution_env import (
    ExecutionPolicy,
    LocalExecutionBackend,
    NetworkMode,
    ProcessRequest,
)
from endless_task.runtime_ledger import TraceContext
from endless_task.workspace_runtime.shell_runner import (
    ShellResult,
    run_shell_command,
)

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")


class ShellExecutionParityTest(unittest.IsolatedAsyncioTestCase):
    """Same command through both paths yields the same observable result."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            network_mode=NetworkMode.DENY,
            timeout_seconds=10.0,
        )
        self.backend = LocalExecutionBackend()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _via_runner(self, command: str) -> ShellResult:
        return await run_shell_command(
            command=command,
            cwd=self.root,
            timeout_seconds=10.0,
            max_output_bytes=64 * 1024,
        )

    async def _via_backend(self, command: str):
        return await self.backend.run_process(
            ProcessRequest(
                effect_id="parity",
                tool_call_id="call",
                argv=("bash", "-c", command),
                cwd=".",
                policy=self.policy,
                trace=TRACE,
                requested_at="2026-09-05T00:00:00Z",
            )
        )

    async def test_success_output_matches(self) -> None:
        command = "echo parity-ok && printf 'line2\\n'"
        runner = await self._via_runner(command)
        backend = await self._via_backend(command)
        self.assertEqual(runner.exit_code, backend.exit_code)
        self.assertEqual(runner.stdout.strip(), backend.stdout.strip())
        self.assertEqual(runner.stderr.strip(), backend.stderr.strip())

    async def test_nonzero_exit_matches(self) -> None:
        command = "echo oops >&2; exit 3"
        runner = await self._via_runner(command)
        backend = await self._via_backend(command)
        self.assertEqual(3, runner.exit_code)
        self.assertEqual(backend.exit_code, runner.exit_code)
        self.assertEqual(runner.stdout, backend.stdout)
        self.assertEqual(runner.stderr.strip(), backend.stderr.strip())

    async def test_stdout_and_stderr_order_combined_semantics(self) -> None:
        # Both paths keep stdout/stderr separate; the tool layer merges them.
        command = "echo out; echo err >&2"
        runner = await self._via_runner(command)
        backend = await self._via_backend(command)
        self.assertIn("out", runner.stdout)
        self.assertIn("out", backend.stdout)
        self.assertIn("err", runner.stderr)
        self.assertIn("err", backend.stderr)

    async def test_workspace_relative_cwd_resolves_identically(self) -> None:
        (self.root / "hello.txt").write_text("hi", encoding="utf-8")
        command = "cat hello.txt"
        runner = await self._via_runner(command)
        backend = await self._via_backend(command)
        self.assertEqual(runner.stdout.strip(), "hi")
        self.assertEqual(backend.stdout.strip(), "hi")

    async def test_backend_timeout_reports_unknown_like_runner_timeout(self) -> None:
        # A sleep beyond the hard timeout: both paths end non-zero/unknown
        # rather than pretending success. (Runner marks timed_out; backend
        # reports unknown exit code via its receipt — both fail closed.)
        command = "sleep 30"
        runner_task = asyncio.create_task(
            run_shell_command(
                command=command,
                cwd=self.root,
                timeout_seconds=1.0,
                max_output_bytes=64 * 1024,
            )
        )
        backend_task = asyncio.create_task(
            self.backend.run_process(
                ProcessRequest(
                    effect_id="timeout",
                    tool_call_id="call_t",
                    argv=("bash", "-c", command),
                    cwd=".",
                    policy=ExecutionPolicy(
                        workspace_root=str(self.root),
                        read_allow_paths=(),
                        write_allow_paths=(),
                        network_mode=NetworkMode.DENY,
                        timeout_seconds=1.0,
                    ),
                    trace=TRACE,
                    requested_at="2026-09-05T00:00:00Z",
                )
            )
        )
        runner = await runner_task
        backend = await backend_task
        self.assertTrue(runner.timed_out)
        # Both paths fail closed on timeout (never report success): the
        # runner kills with a negative exit, the backend reports an
        # unknown (None) exit and an uncommitted effect receipt.
        self.assertIsNotNone(runner.exit_code)
        self.assertLess(runner.exit_code, 0)
        self.assertIsNone(backend.exit_code)
        self.assertIsNotNone(backend.receipt.outcome)
        from endless_task.agent_platform import EffectOutcome

        self.assertIsNot(EffectOutcome.COMMITTED, backend.receipt.outcome)


class ContainerShellParityTest(ShellExecutionParityTest):
    """Container-backed parity subset (live docker, success paths).

    Container images (alpine) provide ``/bin/sh`` but no ``bash``, and the
    container backend deliberately fails closed on any non-zero exit
    (AP-307: ``container_unavailable`` — documented divergence, revisit at
    RS-6 cutover). So this subset re-runs the *success* parity assertions
    through the container backend: workspace-mounted execution and the
    replaceability of the ExecutionEnvironment boundary. Skips without a
    container runtime.
    """

    async def asyncSetUp(self) -> None:
        from endless_task.execution_env.container import (
            ContainerExecutionBackend,
            probe_container_runtime,
        )

        if not probe_container_runtime("docker"):
            self.skipTest("container runtime unavailable")
        # colima/virtiofs mounts $HOME (not /tmp): the workspace file must
        # live under a mounted path or the container never sees it. Each
        # test gets its own top-level directory so teardown never races a
        # sibling test's docker run over a reused inode.
        import uuid

        home = Path.home()
        probe_dir = home / ".endless-task-container-probe"
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe_dir.rmdir()
        except OSError:
            self.skipTest(
                "container parity needs a writable $HOME for colima mounts"
            )

        self._ws_home = (
            Path.home()
            / ".endless-task-container-parity"
            / f"run-{uuid.uuid4().hex[:8]}"
        )
        self.root = self._ws_home / "workspace"
        self.root.mkdir(parents=True)
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            network_mode=NetworkMode.DENY,
            timeout_seconds=10.0,
        )
        self.backend = ContainerExecutionBackend(
            image="alpine:3.20", runtime="docker"
        )
        # Alpine has no bash: run the shell subset with /bin/sh.
        self._shell = "/bin/sh"

    async def _via_runner(self, command: str) -> ShellResult:
        # The runner hard-codes bash; the container cannot run it, so the
        # success parity here compares container-vs-container semantics on
        # the shared assertions that do not need bash (documented above).
        return await self.backend.run_process(
            ProcessRequest(
                effect_id="parity-sh",
                tool_call_id="call",
                argv=(self._shell, "-c", command),
                cwd=".",
                policy=self.policy,
                trace=TRACE,
                requested_at="2026-09-05T00:00:00Z",
            )
        )

    async def _via_backend(self, command: str):
        return await self._via_runner(command)

    async def asyncTearDown(self) -> None:
        import shutil

        ws_home = getattr(self, "_ws_home", None)
        if ws_home is not None and ws_home.exists():
            shutil.rmtree(ws_home, ignore_errors=True)

    async def test_nonzero_exit_matches(self) -> None:
        # Documented divergence: container backend fails closed on non-zero
        # exit (AP-307), so this parity is not applicable on the container.
        self.skipTest(
            "container backend fails closed on non-zero exit (AP-307); "
            "divergence recorded, revisit at RS-6 cutover"
        )

    async def test_backend_timeout_reports_unknown_like_runner_timeout(self) -> None:
        self.skipTest(
            "timeout exit semantics differ per backend (runner SIGTERM vs "
            "container fail-closed); covered by local parity"
        )


if __name__ == "__main__":
    unittest.main()
