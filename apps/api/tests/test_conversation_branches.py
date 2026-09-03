"""P4.5 会话分支与临时会话验收（设计 §9 不变量）。

v1 HTTP 分支 API（``POST /conversations/{id}/branches`` 等）操作的是 v1 对话
轮次。v2 是唯一运行时，v2 消息写入 v2 条目（entries）而非 v1 turns，因此 v1
分支路由在 v2 下不再产生有意义的结果（分支前无 turn 会被拒绝）。那些依赖 v1
HTTP 分支路由/快照的验收测试已随 v1 运行时一并移除。

这里保留的 ``test_repository_lineage_order_and_depth_limit`` 直接测试仍然存在
的 ``SqliteChatRepository`` 分支/谱系（lineage）仓储逻辑，不依赖已删除的 v1
HTTP 路由。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import ConversationKind, FinishReason
from endless_task.domain.repositories import InvalidStateError
from endless_task.storage import Database, SqliteChatRepository


class ConversationBranchGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self._temporary_directory.name) / "branch.db"

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_repository_lineage_order_and_depth_limit(self) -> None:
        database = Database(self.database_path)
        database.initialize()
        repository = SqliteChatRepository(database)

        root = repository.create_conversation()

        def finish(snapshot, content: str):
            variant_id = snapshot.response_variants[0].variant.id
            repository.mark_response_running(
                turn_id=snapshot.turn.id, variant_id=variant_id
            )
            return repository.complete_response(
                turn_id=snapshot.turn.id,
                variant_id=variant_id,
                content=content,
                finish_reason=FinishReason.STOP,
            )

        first = finish(
            repository.create_turn(
                conversation_id=root.id, client_request_id="r1", content="第一轮"
            ),
            "第一轮的回答",
        )
        second = finish(
            repository.create_turn(
                conversation_id=root.id, client_request_id="r2", content="第二轮"
            ),
            "第二轮的回答",
        )

        branch = repository.create_branch(parent_conversation_id=root.id)
        self.assertEqual(ConversationKind.EPHEMERAL, branch.kind)
        self.assertEqual(second.turn.id, branch.fork_turn_id)

        lineage = repository.list_lineage_turns(branch.id)
        self.assertEqual(
            (first.turn.id, second.turn.id),
            tuple(item.turn.id for item in lineage),
        )

        mid_branch = repository.create_branch(
            parent_conversation_id=root.id, fork_turn_id=first.turn.id
        )
        mid_lineage = repository.list_lineage_turns(mid_branch.id)
        self.assertEqual((first.turn.id,), tuple(item.turn.id for item in mid_lineage))

        branch_turn = finish(
            repository.create_turn(
                conversation_id=branch.id,
                client_request_id="b1",
                content="分支里的一轮",
            ),
            "分支里的回答",
        )
        grandchild = repository.create_branch(parent_conversation_id=branch.id)
        grand_lineage = repository.list_lineage_turns(grandchild.id)
        self.assertEqual(
            (first.turn.id, second.turn.id, branch_turn.turn.id),
            tuple(item.turn.id for item in grand_lineage),
        )

        self.assertEqual((), tuple(repository.list_lineage_turns(root.id)))

        cursor = grandchild
        for index in range(6):
            finish(
                repository.create_turn(
                    conversation_id=cursor.id,
                    client_request_id=f"depth-{index}",
                    content="再深一层",
                ),
                "再深一层的回答",
            )
            cursor = repository.create_branch(parent_conversation_id=cursor.id)
        with self.assertRaises(InvalidStateError):
            finish(
                repository.create_turn(
                    conversation_id=cursor.id,
                    client_request_id="depth-final",
                    content="最深一层",
                ),
                "最深一层的回答",
            )
            repository.create_branch(parent_conversation_id=cursor.id)


if __name__ == "__main__":
    unittest.main()
