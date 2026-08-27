"""R5.8 向量仓储：embeddings 表读写。向量以 float32 BLOB 存放，检索在读时暴力计算。"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from .database import Database
from .sqlite_chat_repository import utc_now

Clock = Callable[[], str]


class SqliteEmbeddingRepository:
    def __init__(self, database: Database, *, clock: Clock = utc_now) -> None:
        self._database = database
        self._clock = clock

    def upsert(
        self,
        scope: str,
        ref_id: str,
        model: str,
        vector: bytes,
        dim: int,
    ) -> None:
        if dim <= 0 or not vector:
            raise ValueError("Vector payload is required.")
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO embeddings (
                    scope, ref_id, model, dim, vector, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, ref_id, model) DO UPDATE SET
                    dim = excluded.dim,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                """,
                (scope, ref_id, model, dim, vector, now, now),
            )

    def get_blob(
        self, scope: str, ref_id: str, model: str
    ) -> Optional[bytes]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT vector FROM embeddings "
                "WHERE scope = ? AND ref_id = ? AND model = ?",
                (scope, ref_id, model),
            ).fetchone()
        return row["vector"] if row is not None else None

    def fetch_blobs(
        self, scope: str, ref_ids: Sequence[str], model: str
    ) -> Dict[str, bytes]:
        if not ref_ids:
            return {}
        result: Dict[str, bytes] = {}
        # SQLite 变量上限保守分片，个人规模通常单片即可。
        chunk_size = 500
        with self._database.connect() as connection:
            for start in range(0, len(ref_ids), chunk_size):
                chunk = list(ref_ids[start : start + chunk_size])
                placeholders = ", ".join(["?"] * len(chunk))
                rows = connection.execute(
                    f"SELECT ref_id, vector FROM embeddings "  # noqa: S608
                    f"WHERE scope = ? AND model = ? AND ref_id IN ({placeholders})",
                    (scope, model, *chunk),
                ).fetchall()
                for row in rows:
                    result[row["ref_id"]] = row["vector"]
        return result

    def delete_ref(self, scope: str, ref_id: str) -> int:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM embeddings WHERE scope = ? AND ref_id = ?",
                (scope, ref_id),
            )
            return cursor.rowcount or 0

    def delete_model(self, model: str) -> int:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM embeddings WHERE model = ?", (model,)
            )
            return cursor.rowcount or 0

    def count_by_scope(self, model: Optional[str] = None) -> Dict[str, int]:
        query = "SELECT scope, COUNT(*) AS total FROM embeddings"
        params: tuple = ()
        if model is not None:
            query += " WHERE model = ?"
            params = (model,)
        query += " GROUP BY scope"
        with self._database.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return {row["scope"]: row["total"] for row in rows}

    def list_models(self) -> List[str]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT model FROM embeddings ORDER BY model"
            ).fetchall()
        return [row["model"] for row in rows]
