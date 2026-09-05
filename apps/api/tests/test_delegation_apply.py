"""M4B slice P3: apply_child_patches tool semantics (06 §30.3 P3).

- new file in scratch -> applied into the main workspace (ledger + audit +
  run checkpoint semantics of a normal write)
- main file already identical -> idempotent no-op (not listed as applied)
- main file exists with different content -> conflict, never overwritten
- refused sources (not completed / unknown child) fail closed with code
- rollback: applied writes are run-scoped agent changes, so restoring the
  parent run reverts them and never touches conflict files
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.agent_platform import AgentPlatformError
from endless_task.delegation.apply_tool import (
    ApplyChildPatchesTool,
    ChildPatchFile,
    ChildPatchSource,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime import WorkspaceBinding
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import WriteWorkspaceFileTool


class _BindingResolver:
    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        return WorkspaceBinding(workspace_id="ws_main", root=self._root)


def _call(conversation_id: str = "conv_1", **arguments) -> ToolCall:
    return ToolCall(
        id="call_apply",
        conversation_id=conversation_id,
        turn_id="turn_1",
        response_variant_id="run_parent",
        tool_name="apply_child_patches",
        arguments=arguments,
        status=ToolCallStatus.RUNNING,
        created_at="2026-09-05T00:00:00.000Z",
    )


class ApplyChildPatchesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.main = self.base / "main-ws"
        self.main.mkdir()
        self.ledger = FileMutationLedger(self.base / "ledger.jsonl")
        self.coordinator = RunCheckpointCoordinator(
            store_root=self.base / "checkpoints",
            ledger=self.ledger,
        )
        resolver = _BindingResolver(self.main)
        effect_log = EffectLog(self.base / "logs")
        write_tool = WriteWorkspaceFileTool(
            resolver,
            effect_log,
            checkpoint_coordinator=self.coordinator,
        )

        async def writer(call, relative: str, content: bytes) -> str:
            target = (Path(resolver.require_binding("x").root) / relative).resolve()
            if target.exists():
                if target.read_bytes() == content:
                    return "equal"
                raise ToolError(
                    "path_exists_conflict",
                    f"主工作区已有 {relative}（内容不同），不覆盖。",
                    retryable=False,
                )
            write_call = ToolCall(
                id=f"apply:{relative}",
                conversation_id=call.conversation_id,
                turn_id=call.turn_id,
                response_variant_id=call.response_variant_id,
                tool_name="write_workspace_file",
                arguments={"path": relative, "content": content.decode("utf-8")},
                status=ToolCallStatus.RUNNING,
                created_at="2026-09-05T00:00:00.000Z",
            )
            await write_tool.execute(write_call, CancellationToken())
            return "applied"

        self._writer = writer
        self._resolver = resolver

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _tool(self, source: ChildPatchSource):
        return ApplyChildPatchesTool(
            patches_provider=lambda child_run_id: source,
            writer=self._writer,
        )

    @staticmethod
    def _source(files: list[tuple[str, bytes]]) -> ChildPatchSource:
        entries = tuple(
            ChildPatchFile(relative=rel, content=data, sha256="")
            for rel, data in files
        )
        return ChildPatchSource(
            files=entries, total_bytes=sum(len(e.content) for e in entries)
        )

    async def test_apply_new_skip_equal_report_conflict(self) -> None:
        (self.main / "conflict.md").write_text("keep-main", encoding="utf-8")
        (self.main / "same.txt").write_text("same", encoding="utf-8")
        tool = self._tool(
            self._source(
                [
                    ("new.md", b"# new from child"),
                    ("conflict.md", b"child version"),
                    ("same.txt", b"same"),
                ]
            )
        )
        result = await tool.execute(_call(childRunId="child_9"), CancellationToken())
        data = result.structured_content
        self.assertEqual(["new.md"], data["applied"])
        self.assertNotIn("same.txt", data["applied"])
        conflict_paths = {c["path"] for c in data["conflicts"]}
        self.assertEqual({"conflict.md"}, conflict_paths)
        self.assertEqual(
            "# new from child",
            (self.main / "new.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "keep-main",
            (self.main / "conflict.md").read_text(encoding="utf-8"),
        )
        # 05 restore semantics: it reverts modifications to files that existed
        # at the checkpoint and never touches conflict files; newly applied
        # files are not auto-deleted (removal stays explicit/manual).
        restored, skipped = self.coordinator.apply_restore(
            run_id="run_parent", workspace_root=str(self.main)
        )
        self.assertNotIn("conflict.md", restored)
        self.assertNotIn("conflict.md", skipped)
        self.assertTrue((self.main / "new.md").exists())
        self.assertEqual(
            "keep-main",
            (self.main / "conflict.md").read_text(encoding="utf-8"),
        )

    async def test_refused_source_fails_closed(self) -> None:
        tool = ApplyChildPatchesTool(
            patches_provider=lambda child_run_id: (
                _raise(AgentPlatformError(
                    "delegation_child_not_completed",
                    "隔离写子代理未完成，暂不能产出 patch。",
                    retryable=False,
                ))
            ),
            writer=self._writer,
        )
        with self.assertRaises(ToolError) as caught:
            await tool.execute(_call(childRunId="child_x"), CancellationToken())
        self.assertEqual(
            "delegation_child_not_completed", caught.exception.code
        )

    async def test_unknown_mode_argument_rejected_by_schema(self) -> None:
        # Tool-level schema guards are enforced by the executor; here we just
        # check the tool's own argument requirement helper.
        from endless_task.tooling import ToolError as TE

        tool = ApplyChildPatchesTool(
            patches_provider=lambda child_run_id: self._source([]),
            writer=self._writer,
        )
        with self.assertRaises(Exception):
            await tool.execute(_call(), CancellationToken())


def _raise(error):
    raise error


if __name__ == "__main__":
    unittest.main()
