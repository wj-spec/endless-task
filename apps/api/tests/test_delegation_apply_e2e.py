"""M4B P3b e2e: isolated child writes its scratch, parent applies to main.

Chain under test (storage/kernel pieces; the executor-level child loop is
covered by the app E2E suites — this file focuses on scratch isolation +
conflict-protected apply):

- P1: scratch workspace under the main root, bound to the child conversation
- child Write tool resolves into the scratch (main workspace untouched)
- P3: apply_child_patches copies the scratch output into the main workspace;
  different-content conflicts are never overwritten
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.delegation.apply_tool import (
    ApplyChildPatchesTool,
    ChildPatchFile,
    ChildPatchSource,
)
from endless_task.delegation.isolated_workspace import (
    prepare_isolated_child_workspace,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime import WorkspaceResolver
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import WriteWorkspaceFileTool


class IsolatedChildApplyE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.database = Database(self.base / "e2e.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.workspace_repository = SqliteWorkspaceRepository(self.database)
        self.repository = SqliteRuntimeV2Repository(self.database)
        self.resolver = WorkspaceResolver(
            self.chat_repository, self.workspace_repository
        )
        self.main = self.base / "main-ws"
        self.main.mkdir()
        (self.main / "keep.txt").write_text("main-original", encoding="utf-8")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _bind_main_conversation(self) -> str:
        workspace = self.workspace_repository.create_workspace(name="主工作区")
        self.workspace_repository.bind_root_path(workspace.id, str(self.main))
        return self.chat_repository.create_conversation(
            workspace_id=workspace.id
        ).id

    async def _child_writes_scratch(
        self, child_run_id: str, path: str, content: str
    ) -> str:
        workspace_id = prepare_isolated_child_workspace(
            workspace_repository=self.workspace_repository,
            parent_workspace_root=self.main,
            child_run_id=child_run_id,
        )
        conversation = self.chat_repository.create_delegation_conversation(
            child_run_id, workspace_id=workspace_id
        )
        write_tool = WriteWorkspaceFileTool(
            _ResolverAdapter(self.resolver),
            EffectLog(self.base / "logs"),
        )
        child_call = ToolCall(
            id="child_write",
            conversation_id=conversation.id,
            turn_id="turn_child",
            response_variant_id=child_run_id,
            tool_name="write_workspace_file",
            arguments={"path": path, "content": content},
            status=ToolCallStatus.RUNNING,
            created_at="2026-09-05T00:00:00.000Z",
        )
        await write_tool.execute(child_call, CancellationToken())
        return workspace_id

    def _apply_tool(self):
        ledger = FileMutationLedger(self.base / "ledger.jsonl")
        coordinator = RunCheckpointCoordinator(
            store_root=self.base / "checkpoints",
            ledger=ledger,
        )
        write_tool = WriteWorkspaceFileTool(
            _ResolverAdapter(self.resolver),
            EffectLog(self.base / "logs"),
            checkpoint_coordinator=coordinator,
        )

        async def writer(call, relative: str, content: bytes) -> str:
            binding = self.resolver.require_binding(call.conversation_id)
            target = (binding.root / relative).resolve()
            if target.exists():
                if target.read_bytes() == content:
                    return "equal"
                raise ToolError(
                    "path_exists_conflict", "conflict", retryable=False
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

        def provider(child_run_id: str) -> ChildPatchSource:
            workspace = self.workspace_repository.get_workspace(child_run_id)
            root = Path(workspace.root_path).expanduser().resolve()
            files = []
            total = 0
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                data = path.read_bytes()
                files.append(
                    ChildPatchFile(
                        relative=path.relative_to(root).as_posix(),
                        content=data,
                        sha256="",
                    )
                )
                total += len(data)
            return ChildPatchSource(tuple(files), total)

        return ApplyChildPatchesTool(patches_provider=provider, writer=writer)

    async def test_child_writes_scratch_and_parent_applies(self) -> None:
        main_conversation_id = self._bind_main_conversation()
        child_ws = await self._child_writes_scratch(
            "child_e2e_1", "output.md", "# 子代理产出"
        )
        scratch = self.workspace_repository.get_workspace(child_ws)
        scratch_root = Path(scratch.root_path).expanduser().resolve()
        self.assertEqual(
            "# 子代理产出",
            (scratch_root / "output.md").read_text(encoding="utf-8"),
        )
        # Main workspace untouched by the child.
        self.assertFalse((self.main / "output.md").exists())
        self.assertEqual(
            "main-original",
            (self.main / "keep.txt").read_text(encoding="utf-8"),
        )

        tool = self._apply_tool()
        result = await tool.execute(
            _parent_call(main_conversation_id, childRunId=child_ws),
            CancellationToken(),
        )
        data = result.structured_content
        self.assertEqual(["output.md"], data["applied"])
        self.assertEqual(
            "# 子代理产出",
            (self.main / "output.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            "main-original",
            (self.main / "keep.txt").read_text(encoding="utf-8"),
        )

    async def test_conflict_is_never_overwritten(self) -> None:
        main_conversation_id = self._bind_main_conversation()
        (self.main / "conflict.md").write_text("main-version", encoding="utf-8")
        child_ws = await self._child_writes_scratch(
            "child_e2e_2", "conflict.md", "child-version"
        )
        tool = self._apply_tool()
        result = await tool.execute(
            _parent_call(main_conversation_id, childRunId=child_ws),
            CancellationToken(),
        )
        data = result.structured_content
        self.assertEqual([], data["applied"])
        self.assertEqual("conflict.md", data["conflicts"][0]["path"])
        self.assertEqual(
            "main-version",
            (self.main / "conflict.md").read_text(encoding="utf-8"),
        )


def _parent_call(conversation_id: str, **arguments) -> ToolCall:
    return ToolCall(
        id="call_apply_e2e",
        conversation_id=conversation_id,
        turn_id="turn_parent",
        response_variant_id="run_parent_e2e",
        tool_name="apply_child_patches",
        arguments=arguments,
        status=ToolCallStatus.RUNNING,
        created_at="2026-09-05T00:00:00.000Z",
    )


class _ResolverAdapter:
    def __init__(self, resolver: WorkspaceResolver) -> None:
        self._resolver = resolver

    def require_binding(self, conversation_id: str):
        return self._resolver.require_binding(conversation_id)


if __name__ == "__main__":
    unittest.main()
