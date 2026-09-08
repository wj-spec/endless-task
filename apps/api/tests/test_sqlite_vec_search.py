import struct
import tempfile
import unittest
from pathlib import Path

from endless_task.storage.database import Database
from endless_task.storage.sqlite_vec_search import SqliteVecSearch


def blob(*values: float) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


class SqliteVecSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self._temporary_directory.name) / "vec.db")
        self.database.initialize()
        self.search = SqliteVecSearch(self.database)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_available_when_extension_present(self) -> None:
        self.assertTrue(self.search.available())

    def test_upsert_and_knn(self) -> None:
        self.search.upsert("knowledge", "ref_a", "bge-small-zh", 4, blob(1, 0, 0, 0))
        self.search.upsert("knowledge", "ref_b", "bge-small-zh", 4, blob(0, 1, 0, 0))
        self.search.upsert("knowledge", "ref_c", "bge-small-zh", 4, blob(0, 0, 1, 0))
        top = self.search.knn("knowledge", "bge-small-zh", 4, blob(0.9, 0.1, 0, 0), k=3)
        self.assertEqual("ref_a", top[0][0])
        self.assertLess(top[0][1], top[1][1])

    def test_upsert_is_idempotent(self) -> None:
        self.search.upsert("knowledge", "ref_a", "m", 4, blob(1, 0, 0, 0))
        self.search.upsert("knowledge", "ref_a", "m", 4, blob(0, 1, 0, 0))
        hits = self.search.knn("knowledge", "m", 4, blob(0.9, 0.1, 0, 0), k=2)
        self.assertEqual(1, len(hits))  # 只有一条（更新后）
        self.assertEqual("ref_a", hits[0][0])

    def test_rebuild_from_embeddings(self) -> None:
        blobs = [("r1", blob(1, 0, 0)), ("r2", blob(0, 1, 0))]
        count = self.search.rebuild_from_embeddings("kb", "m", 3, blobs)
        self.assertEqual(2, count)
        top = self.search.knn("kb", "m", 3, blob(0.9, 0, 0), k=2)
        self.assertEqual("r1", top[0][0])

    def test_scoped_query(self) -> None:
        self.search.upsert("a", "x", "m", 4, blob(1, 0, 0, 0))
        self.search.upsert("b", "y", "m", 4, blob(1, 0, 0, 0))
        self.assertEqual(["x"], [r[0] for r in self.search.knn("a", "m", 4, blob(1, 0, 0, 0), k=2)])


if __name__ == "__main__":
    unittest.main()
