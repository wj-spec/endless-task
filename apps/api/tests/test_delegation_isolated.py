"""M4B slice P1: isolated child scratch workspace + binding (06 §30.3 P1).

Proof that a write-enabled child conversation bound to the scratch root:

- writes land only inside the scratch dir (main workspace zero change);
- escaping the scratch root is rejected (containment);
- main-workspace run checkpoints skip the delegation scratch dir;
- a child-run checkpoint rooted at the scratch restores it (rollback base).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.delegation.isolated_workspace import (
    prepare_isolated_child_workspace,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime import WorkspaceBinding, WorkspaceResolver
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import WriteWorkspaceFileTool


class IsolatedChildWorkspaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.database = Database(self.base / "iso.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)
        self.workspace_repository = SqliteWorkspaceRepository(self.database)
        self.runtime_v2_repository = SqliteRuntimeV2Repository(self.database)
        self.resolver = WorkspaceResolver(
            self.chat_repository, self.workspace_repository
        )
        self.parent_root = self.base / "main-ws"
        self.parent_root.mkdir()
        (self.parent_root / "keep.txt").write_text("main", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _bind_main(self) -> str:
        workspace = self.workspace_repository.create_workspace(name="主工作区")
        return self.workspace_repository.bind_root_path(
            workspace.id, str(self.parent_root)
        ).id

    def _write_call(self, conversation_id: str, **arguments) -> ToolCall:
        return ToolCall(
            id="call_1",
            conversation_id=conversation_id,
            turn_id="turn_1",
            response_variant_id="run_child",
            tool_name="write_workspace_file",
            arguments=arguments,
            status=ToolCallStatus.CREATED,
            created_at="2026-09-05T00:00:00.000Z",
        )

    async def test_child_writes_land_only_in_scratch(self) -> None:
        self._bind_main()
        scratch_ws_id = prepare_isolated_child_workspace(
            workspace_repository=self.workspace_repository,
            parent_workspace_root=self.parent_root,
            child_run_id="child_1",
        )
        conversation = self.chat_repository.create_delegation_conversation(
            "child_1", workspace_id=scratch_ws_id
        )
        binding = self.resolver.require_binding(conversation.id)
        self.assertTrue(
            str(binding.root).endswith(
                f".endless-task/delegation/child_1"
            )
        )
        self.assertNotEqual(binding.root, self.parent_root.resolve())

        tool = WriteWorkspaceFileTool(
            resolver=_ConversationResolver(self.resolver),
            effect_log=EffectLog(self.base / "logs"),
        )
        await tool.execute(
            self._write_call(conversation.id, path="notes.md", content="child content"),
            __import__("endless_task.runtime.cancellation", fromlist=["CancellationToken"]).CancellationToken(),
        )

        self.assertEqual(
            "child content",
            (binding.root / "notes.md").read_text(encoding="utf-8"),
        )
        # Main workspace untouched (only the hidden scratch tree appeared).
        self.assertEqual(
            "main", (self.parent_root / "keep.txt").read_text(encoding="utf-8")
        )
        self.assertFalse((self.parent_root / "notes.md").exists())

    async def test_escaping_scratch_is_rejected(self) -> None:
        scratch_ws_id = prepare_isolated_child_workspace(
            workspace_repository=self.workspace_repository,
            parent_workspace_root=self.parent_root,
            child_run_id="child_2",
        )
        conversation = self.chat_repository.create_delegation_conversation(
            "child_2", workspace_id=scratch_ws_id
        )
        tool = WriteWorkspaceFileTool(
            resolver=_ConversationResolver(self.resolver),
            effect_log=EffectLog(self.base / "logs"),
        )
        with self.assertRaises(ToolError):
            await tool.execute(
                self._write_call(conversation.id, path="../escape.txt", content="nope"),
                __import__("endless_task.runtime.cancellation", fromlist=["CancellationToken"]).CancellationToken(),
            )
        self.assertFalse((self.parent_root / "escape.txt").exists())

    async def test_main_checkpoint_skips_scratch_and_child_restores(self) -> None:
        self._bind_main()
        scratch_ws_id = prepare_isolated_child_workspace(
            workspace_repository=self.workspace_repository,
            parent_workspace_root=self.parent_root,
            child_run_id="child_3",
        )
        conversation = self.chat_repository.create_delegation_conversation(
            "child_3", workspace_id=scratch_ws_id
        )
        binding = self.resolver.require_binding(conversation.id)
        ledger = FileMutationLedger(self.base / "ledger.jsonl")
        coordinator = RunCheckpointCoordinator(
            store_root=self.base / "checkpoints",
            ledger=ledger,
        )

        # A main-run checkpoint over the whole workspace must not snapshot the
        # delegation scratch subtree.
        main_ref = coordinator.ensure_checkpoint(
            run_id="run_main", workspace_root=str(self.parent_root)
        )
        main_manifest = coordinator._load_manifest(main_ref)
        paths = {relative for relative, _ in main_manifest.entries}
        self.assertIn("keep.txt", paths)
        self.assertFalse(
            any(p.startswith(".endless-task/delegation") for p in paths),
            f"scratch leaked into main snapshot: {sorted(paths)}",
        )

        # Child-run checkpoint over the scratch root restores it (rollback base).
        import hashlib

        (binding.root / "notes.md").write_text("draft", encoding="utf-8")
        child_ref = coordinator.ensure_checkpoint(
            run_id="run_child", workspace_root=str(binding.root)
        )
        self.assertIsNotNone(child_ref)
        (binding.root / "notes.md").write_text("draft-v2", encoding="utf-8")
        coordinator.record_effect(
            run_id="run_child",
            effect_id="fx_child_v2",
            path="notes.md",
            operation="file_write",
            before_hash=hashlib.sha256(b"draft").hexdigest(),
            after_hash=hashlib.sha256(b"draft-v2").hexdigest(),
        )
        restored, _ = coordinator.apply_restore(
            run_id="run_child", workspace_root=str(binding.root)
        )
        self.assertIn("notes.md", restored)
        self.assertEqual(
            "draft", (binding.root / "notes.md").read_text(encoding="utf-8")
        )


class _ConversationResolver:
    def __init__(self, resolver: WorkspaceResolver) -> None:
        self._resolver = resolver

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        return self._resolver.require_binding(conversation_id)


if __name__ == "__main__":
    unittest.main()
