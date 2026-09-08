import asyncio
import unittest

from endless_task.runtime_v2.execution import ToolApprovalDecision
from endless_task.runtime_v2.gateway import (
    GatewayToolApprovalGate,
    PendingApproval,
)


class _FakeRepo:
    pass


class ApprovalModifyTest(unittest.IsolatedAsyncioTestCase):
    def _gate(self) -> GatewayToolApprovalGate:
        pending_approvals = {
            "a1": PendingApproval(
                approval_id="a1",
                run_id="r1",
                model_turn_id="mt1",
                tool_execution_id="te1",
                tool_name="write_workspace_file",
                summary="执行写入？",
                reason="会修改本机数据。",
                metadata={},
            )
        }
        gate = GatewayToolApprovalGate(
            repository=_FakeRepo(),  # type: ignore[arg-type]
            pending_approvals=pending_approvals,
            lock=asyncio.Lock(),
            metrics=None,
            approval_timeout_seconds=None,
        )
        gate._waiters["a1"] = asyncio.get_running_loop().create_future()
        return gate

    async def test_resolve_modify_stores_arguments(self) -> None:
        gate = self._gate()
        result = await gate.resolve(
            "a1",
            ToolApprovalDecision.MODIFY,
            modified_arguments={"path": "docs/a.md", "content": "hi"},
        )
        self.assertTrue(result)
        self.assertEqual(
            {"path": "docs/a.md", "content": "hi"},
            gate.modified_arguments_for("a1"),
        )
        self.assertEqual(
            ToolApprovalDecision.MODIFY,
            gate._waiters["a1"].result(),
        )

    async def test_resolve_without_arguments_leaves_none(self) -> None:
        gate = self._gate()
        await gate.resolve("a1", ToolApprovalDecision.APPROVE)
        self.assertIsNone(gate.modified_arguments_for("a1"))

    async def test_modified_arguments_for_missing_returns_none(self) -> None:
        gate = self._gate()
        self.assertIsNone(gate.modified_arguments_for("nope"))


if __name__ == "__main__":
    unittest.main()
