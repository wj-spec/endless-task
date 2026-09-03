"""AP-301: LocalExecutionBackend conformance, parity and fault tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import AgentPlatformError, EffectOutcome
from endless_task.execution_env import (
    ExecutionPolicy,
    FileMutationOperation,
    FileMutationRequest,
    LocalExecutionBackend,
    ProcessRequest,
    ReadFileRequest,
    assert_execution_environment_conformance,
)
from endless_task.runtime_ledger import TraceContext
from endless_task.workspace_runtime import WorkspaceBinding
from endless_task.workspace_runtime.fs_tools import ReadWorkspaceFileTool

TRACE = TraceContext(trace_id="trace_1", run_id="run_1", correlation_id="corr_1")


class BindingResolver:
    def __init__(self, binding: WorkspaceBinding) -> None:
        self._binding = binding

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        return self._binding


class LocalBackendCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            timeout_seconds=2.0,
            max_output_characters=1000,
        )
        self.backend = LocalExecutionBackend()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _read(self, path: str) -> ReadFileRequest:
        return ReadFileRequest(path=path, policy=self.policy, trace=TRACE)

    def _mutate(
        self,
        path: str,
        *,
        operation: FileMutationOperation,
        content: str | None = None,
        effect_id: str = "effect_1",
        expected_before_hash: str | None = None,
    ) -> FileMutationRequest:
        return FileMutationRequest(
            effect_id=effect_id,
            tool_call_id="call_1",
            path=path,
            operation=operation,
            policy=self.policy,
            trace=TRACE,
            requested_at="2026-09-03T00:00:00Z",
            content=content,
            expected_before_hash=expected_before_hash,
        )

    def _process(self, argv, *, timeout: float | None = None, cwd: str | None = None) -> ProcessRequest:
        policy = self.policy
        if timeout is not None:
            policy = ExecutionPolicy(
                workspace_root=str(self.root),
                read_allow_paths=(),
                write_allow_paths=(),
                timeout_seconds=timeout,
                max_output_characters=policy.max_output_characters,
            )
        return ProcessRequest(
            effect_id="effect_p",
            tool_call_id="call_p",
            argv=tuple(argv),
            cwd=cwd or ".",
            policy=policy,
            trace=TRACE,
            requested_at="2026-09-03T00:00:00Z",
        )


class ConformanceAndFilesTest(LocalBackendCase):
    async def test_conformance_harness_passes(self) -> None:
        (self.root / "seed.txt").write_text("seed", encoding="utf-8")
        receipt = await assert_execution_environment_conformance(
            self.backend,
            read_request=self._read("seed.txt"),
            mutation_request=self._mutate("out.txt", operation=FileMutationOperation.WRITE, content="x"),
        )
        self.assertEqual("effect_1", receipt.effect_id)
        self.assertEqual("call_1", receipt.tool_call_id)
        self.assertTrue((self.root / "out.txt").exists())

    async def test_write_then_read_round_trip(self) -> None:
        receipt = await self.backend.mutate_file(
            self._mutate(
                "nested/file.txt",
                operation=FileMutationOperation.WRITE,
                content="hello",
            )
        )
        self.assertEqual(EffectOutcome.COMMITTED, receipt.outcome)
        self.assertEqual("file_write", receipt.effect_type)
        self.assertTrue((self.root / "nested" / "file.txt").exists())
        result = await self.backend.read_file(self._read("nested/file.txt"))
        self.assertEqual("hello", result.content)
        self.assertFalse(result.truncated)

    async def test_read_errors_match_legacy_codes(self) -> None:
        resolver = BindingResolver(
            WorkspaceBinding(workspace_id="w1", root=self.root)
        )
        legacy = ReadWorkspaceFileTool(resolver)
        for path, code in (
            ("missing.txt", "path_not_found"),
            ("a_directory", "path_is_directory"),
            ("escape/../secret.txt", "path_escape"),
            ("/etc/hosts", "path_escape"),
        ):
            with self.subTest(path=path):
                if path == "a_directory":
                    (self.root / "a_directory").mkdir()
                with self.assertRaises(AgentPlatformError) as backend_error:
                    await self.backend.read_file(self._read(path))
                self.assertEqual(code, backend_error.exception.code)
                with self.assertRaises(Exception) as legacy_error:
                    from endless_task.tooling import ToolCall, ToolCallStatus
                    from endless_task.runtime.cancellation import CancellationToken

                    await legacy.execute(
                        ToolCall(
                            id="c1",
                            conversation_id="conv",
                            turn_id="t",
                            response_variant_id="v",
                            tool_name="read_workspace_file",
                            arguments={"path": path},
                            status=ToolCallStatus.CREATED,
                            created_at="2026-09-03T00:00:00Z",
                        ),
                        CancellationToken(),
                    )
                self.assertEqual(code, getattr(legacy_error.exception, "code", None))

    async def test_binary_content_rejected(self) -> None:
        (self.root / "binary.bin").write_bytes(b"\x00\xff\x01")
        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.read_file(self._read("binary.bin"))
        self.assertEqual("binary_content", caught.exception.code)

    async def test_delete_effects_and_double_delete_error(self) -> None:
        (self.root / "gone.txt").write_text("x", encoding="utf-8")
        receipt = await self.backend.mutate_file(
            self._mutate("gone.txt", operation=FileMutationOperation.DELETE)
        )
        self.assertEqual("file_delete", receipt.effect_type)
        self.assertFalse((self.root / "gone.txt").exists())
        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.mutate_file(
                self._mutate("gone.txt", operation=FileMutationOperation.DELETE)
            )
        self.assertEqual("path_not_found", caught.exception.code)

    async def test_expected_before_hash_conflict_fails_closed(self) -> None:
        (self.root / "guarded.txt").write_text("a", encoding="utf-8")
        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.mutate_file(
                self._mutate(
                    "guarded.txt",
                    operation=FileMutationOperation.WRITE,
                    content="b",
                    expected_before_hash="0" * 64,
                )
            )
        self.assertEqual("mutation_conflict", caught.exception.code)


class ProcessTest(LocalBackendCase):
    async def test_process_runs_with_absolute_interpreter(self) -> None:
        result = await self.backend.run_process(
            self._process(["/bin/sh", "-c", "printf hi"])
        )
        self.assertEqual(0, result.exit_code)
        self.assertEqual("hi", result.stdout)
        self.assertEqual(EffectOutcome.COMMITTED, result.receipt.outcome)

    async def test_process_timeout_reports_unknown(self) -> None:
        result = await self.backend.run_process(
            self._process(["/bin/sh", "-c", "sleep 1"], timeout=0.05)
        )
        self.assertIsNone(result.exit_code)
        self.assertEqual(EffectOutcome.UNKNOWN, result.receipt.outcome)

    async def test_process_output_truncation_flag(self) -> None:
        policy = ExecutionPolicy(
            workspace_root=str(self.root),
            read_allow_paths=(),
            write_allow_paths=(),
            timeout_seconds=2.0,
            max_output_characters=8,
        )
        request = ProcessRequest(
            effect_id="effect_p",
            tool_call_id="call_p",
            argv=("/bin/sh", "-c", "printf abcdefghijklmnop"),
            cwd=".",
            policy=policy,
            trace=TRACE,
            requested_at="2026-09-03T00:00:00Z",
        )
        result = await self.backend.run_process(request)
        self.assertTrue(result.truncated)
        self.assertIn("截断", result.stdout)

    async def test_process_cwd_escape_rejected(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.run_process(
                self._process(["/bin/sh", "-c", "pwd"], cwd="/etc")
            )
        self.assertEqual("path_escape", caught.exception.code)


class ReceiptSinkTest(LocalBackendCase):
    async def test_sink_failure_reports_unknown_outcome(self) -> None:
        def failing_sink(receipt) -> None:
            raise RuntimeError("ledger down")

        backend = LocalExecutionBackend(receipt_sink=failing_sink)
        receipt = await backend.mutate_file(
            self._mutate("logged.txt", operation=FileMutationOperation.WRITE, content="x")
        )
        # Side effect happened but the ledger failed: outcome must be unknown,
        # never "not executed".
        self.assertEqual(EffectOutcome.UNKNOWN, receipt.outcome)
        self.assertTrue((self.root / "logged.txt").exists())

    async def test_receipt_sink_receives_committed_receipt(self) -> None:
        seen: list = []

        def recording_sink(receipt) -> None:
            seen.append(receipt)

        backend = LocalExecutionBackend(receipt_sink=recording_sink)
        receipt = await backend.mutate_file(
            self._mutate("ok.txt", operation=FileMutationOperation.WRITE, content="x")
        )
        self.assertEqual(EffectOutcome.COMMITTED, receipt.outcome)
        self.assertEqual(1, len(seen))
        self.assertEqual(receipt.effect_id, seen[0].effect_id)


class NotImplementedSliceTest(LocalBackendCase):
    async def test_checkpoint_and_restore_fail_closed(self) -> None:
        from endless_task.execution_env import (
            CheckpointRequest,
            CheckpointRef,
            RestoreRequest,
        )

        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.checkpoint(
                CheckpointRequest(
                    run_id="run_1",
                    policy=self.policy,
                    trace=TRACE,
                    created_at="2026-09-03T00:00:00Z",
                )
            )
        self.assertEqual("not_implemented_in_slice", caught.exception.code)
        ref = CheckpointRef(
            checkpoint_id="cp_1",
            run_id="run_1",
            backend="local",
            root_fingerprint="0" * 64,
            manifest_ref="manifest",
            created_at="2026-09-03T00:00:00Z",
        )
        with self.assertRaises(AgentPlatformError) as caught:
            await self.backend.restore(
                RestoreRequest(checkpoint=ref, policy=self.policy, trace=TRACE)
            )
        self.assertEqual("not_implemented_in_slice", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
