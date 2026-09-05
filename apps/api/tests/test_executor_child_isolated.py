"""M4B executor-level child e2e + deadlock probe (06 §30.5).

A real AgentRunExecutor runs a child conversation bound to the isolated
scratch: the model emits write_workspace_file, the tool writes into the
scratch only, the main workspace stays untouched. Runs inside unittest (the
stable channel — ad-hoc repros were environment-flaky).

If the tool-call finalization deadlock is the same-thread nested-_write
class, the reentrancy guard now surfaces `nested_database_write` instead of
hanging; any such finding goes back to the 30.5 debugging plan.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.delegation.isolated_workspace import (
    prepare_isolated_child_workspace,
)
from endless_task.execution_env.ledger import FileMutationLedger
from endless_task.runtime.cancellation import CancellationToken
from endless_task.runtime.provider import (
    ProviderCompleted,
    ProviderError,
    ProviderTextDelta,
    ProviderToolCall,
)
from endless_task.runtime_v2 import (
    Actor,
    AgentRunExecutor,
    RunStatus,
    TranscriptEntryType,
    UnattendedToolApprovalGate,
)
from endless_task.runtime_v2.run_checkpoint import RunCheckpointCoordinator
from endless_task.runtime_v2.run_restore import (
    RunFailureAutoRestoreObserver,
    default_run_status_reader,
)
from endless_task.storage import (
    Database,
    SqliteChatRepository,
    SqliteRuntimeV2Repository,
    SqliteWorkspaceRepository,
)
from endless_task.tooling import ToolRegistry
from endless_task.workspace_runtime import WorkspaceResolver
from endless_task.workspace_runtime.effect_log import EffectLog
from endless_task.workspace_runtime.fs_tools import WriteWorkspaceFileTool


class _WriteOnceProvider:
    name = "scripted-child-writer"

    def __init__(self, path: str, content: str, *, fail_after: bool = False):
        self._path = path
        self._content = content
        self._fail_after = fail_after
        self.requests = 0

    async def stream(self, request, cancellation_token):
        self.requests += 1
        if self.requests == 1:
            yield ProviderToolCall(
                id="child_call_write",
                name="write_workspace_file",
                arguments={"path": self._path, "content": self._content},
            )
            yield ProviderCompleted(finish_reason="tool_calls")
            return
        if self._fail_after:
            raise ProviderError(
                "provider_upstream_error", "注入故障", retryable=False
            )
        yield ProviderTextDelta("完成。")
        yield ProviderCompleted(finish_reason="stop")


class ExecutorChildIsolatedE2ETest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.database = Database(self.base / "child.db")
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

    def _bind_scratch_conversation(self, child_run_id: str):
        workspace_id = prepare_isolated_child_workspace(
            workspace_repository=self.workspace_repository,
            parent_workspace_root=self.main,
            child_run_id=child_run_id,
        )
        conversation = self.chat_repository.create_delegation_conversation(
            child_run_id, workspace_id=workspace_id
        )
        return conversation, workspace_id

    def _executor(self, provider, *, trace_observer=None, coordinator=None):
        registry = ToolRegistry()
        registry.register(
            WriteWorkspaceFileTool(
                self.resolver,
                EffectLog(self.base / "logs"),
                checkpoint_coordinator=coordinator,
            )
        )
        return AgentRunExecutor(
            repository=self.repository,
            provider=provider,
            tool_registry=registry,
            model="scripted-model",
            approval_gate=UnattendedToolApprovalGate(),
            v2_pipeline_enabled=True,
            trace_observer=trace_observer,
        )

    def _run_for(self, conversation):
        lane = self.repository.create_lane(conversation_id=conversation.id)
        trigger = self.repository.append_entry(
            conversation_id=conversation.id,
            lane_id=lane.id,
            type=TranscriptEntryType.USER_MESSAGE,
            actor=Actor.USER,
            payload={"content": "写文件"},
            context_policy={"include_in_llm": True, "transform": "full"},
        )
        return self.repository.create_run(
            conversation_id=conversation.id,
            lane_id=lane.id,
            trigger_entry_id=trigger.id,
        )

    async def test_child_executor_writes_scratch_only(self) -> None:
        conversation, _ = self._bind_scratch_conversation("child_x")
        binding = self.resolver.require_binding(conversation.id)
        run = self._run_for(conversation)
        provider = _WriteOnceProvider("output.md", "# 子代理产出")
        executor = self._executor(provider)
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        self.assertEqual(RunStatus.COMPLETED, result.status)
        self.assertEqual(
            "# 子代理产出",
            (binding.root / "output.md").read_text(encoding="utf-8"),
        )
        self.assertFalse((self.main / "output.md").exists())
        self.assertEqual(
            "main-original",
            (self.main / "keep.txt").read_text(encoding="utf-8"),
        )

    async def test_child_executor_failure_restores_scratch(self) -> None:
        conversation, _ = self._bind_scratch_conversation("child_y")
        binding = self.resolver.require_binding(conversation.id)
        (binding.root / "output.md").write_text("pre-run", encoding="utf-8")
        run = self._run_for(conversation)
        ledger = FileMutationLedger(self.base / "ledger.jsonl")
        coordinator = RunCheckpointCoordinator(
            store_root=self.base / "checkpoints",
            ledger=ledger,
        )
        observer = RunFailureAutoRestoreObserver(
            run_reader=default_run_status_reader(self.repository),
            coordinator=coordinator,
        )
        provider = _WriteOnceProvider("output.md", "draft", fail_after=True)
        executor = self._executor(
            provider, trace_observer=observer, coordinator=coordinator
        )
        result = await executor.execute(
            run.id, cancellation_token=CancellationToken()
        )
        self.assertEqual(RunStatus.FAILED, result.status)
        self.assertEqual(
            "pre-run",
            (binding.root / "output.md").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
