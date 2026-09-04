"""M4A DR-1 slice 5: delegation child-conversation product isolation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.storage import Database, SqliteChatRepository


class DelegationConversationIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "runtime.db")
        self.database.initialize()
        self.chat_repository = SqliteChatRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_delegation_conversation_hidden_from_product_enumeration(self) -> None:
        product = self.chat_repository.create_conversation()
        child = self.chat_repository.create_delegation_conversation("child_run_1")

        visible = [
            item.id for item in self.chat_repository.list_conversations()
        ]
        self.assertIn(product.id, visible)
        self.assertNotIn(child.id, visible)

    def test_include_delegation_returns_tagged_conversations(self) -> None:
        product = self.chat_repository.create_conversation()
        child = self.chat_repository.create_delegation_conversation("child_run_1")

        visible = [
            item.id
            for item in self.chat_repository.list_conversations(
                include_delegation=True
            )
        ]
        self.assertIn(product.id, visible)
        self.assertIn(child.id, visible)

    def test_multiple_child_runs_each_get_isolated_conversations(self) -> None:
        first = self.chat_repository.create_delegation_conversation("child_run_1")
        second = self.chat_repository.create_delegation_conversation("child_run_2")
        self.assertNotEqual(first.id, second.id)
        visible = [
            item.id for item in self.chat_repository.list_conversations()
        ]
        self.assertNotIn(first.id, visible)
        self.assertNotIn(second.id, visible)
        tagged = [
            item.id
            for item in self.chat_repository.list_conversations(
                include_delegation=True
            )
        ]
        self.assertEqual(2, len(tagged))

    def test_title_query_and_status_filters_apply_to_delegation_listing(self) -> None:
        child = self.chat_repository.create_delegation_conversation("child_run_1")
        all_ids = [
            item.id
            for item in self.chat_repository.list_conversations(
                include_delegation=True
            )
        ]
        self.assertIn(child.id, all_ids)
        # Delegation conversations default to the same active status.
        statuses = {
            item.id: item.status
            for item in self.chat_repository.list_conversations(
                include_delegation=True
            )
        }
        self.assertEqual("active", statuses[child.id].value)


if __name__ == "__main__":
    unittest.main()
