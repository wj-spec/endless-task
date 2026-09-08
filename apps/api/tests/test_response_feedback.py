import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import FeedbackRating, ResponseFeedback
from endless_task.storage.database import Database
from endless_task.storage.sqlite_response_feedback_repository import (
    SqliteResponseFeedbackRepository,
)


class ResponseFeedbackRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "feedback.db"
        )
        self.database.initialize()
        self.repo = SqliteResponseFeedbackRepository(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_upsert_inserts(self) -> None:
        record = self.repo.upsert(
            conversation_id="c1",
            turn_id="run1",
            variant_id="v1",
            rating=FeedbackRating.DOWN,
            reason="太啰嗦",
            note="",
        )
        self.assertIsInstance(record, ResponseFeedback)
        self.assertEqual("down", record.rating.value)
        self.assertEqual("太啰嗦", record.reason)
        self.assertEqual("run1", record.turn_id)

    def test_upsert_is_idempotent_and_updates(self) -> None:
        first = self.repo.upsert(
            conversation_id="c1",
            turn_id="run1",
            variant_id="v1",
            rating=FeedbackRating.DOWN,
            reason="太啰嗦",
        )
        second = self.repo.upsert(
            conversation_id="c1",
            turn_id="run1",
            variant_id="v1",
            rating=FeedbackRating.UP,
            reason="改主意",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual("up", second.rating.value)
        self.assertEqual("改主意", second.reason)
        self.assertEqual(1, len(self.repo.list_for_conversation("c1")))

    def test_upsert_handles_null_variant(self) -> None:
        self.repo.upsert(
            conversation_id="c1", turn_id="run1", variant_id=None,
            rating=FeedbackRating.UP,
        )
        self.repo.upsert(
            conversation_id="c1", turn_id="run1", variant_id=None,
            rating=FeedbackRating.DOWN,
        )
        # 无 variant 时按 turn 唯一，后写覆盖前写
        self.assertEqual(1, len(self.repo.list_for_conversation("c1")))
        self.assertEqual("down", self.repo.get_for_turn("run1").rating.value)

    def test_get_for_turn_and_list(self) -> None:
        self.repo.upsert(
            conversation_id="c1", turn_id="runA", variant_id="v1",
            rating=FeedbackRating.UP,
        )
        self.repo.upsert(
            conversation_id="c1", turn_id="runB", variant_id="v2",
            rating=FeedbackRating.DOWN,
        )
        self.assertEqual("up", self.repo.get_for_turn("runA").rating.value)
        self.assertIsNone(self.repo.get_for_turn("missing"))
        self.assertEqual(2, len(self.repo.list_for_conversation("c1")))
        self.assertEqual(0, len(self.repo.list_for_conversation("other")))


if __name__ == "__main__":
    unittest.main()
