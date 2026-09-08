"""R5.8-sqlite-vec：可选的本地向量 KNN 检索（默认关闭，不改变主链路）。

现有 `SqliteEmbeddingRepository` 把向量存成 float32 BLOB，检索时在内存暴力算距离。
本模块在 sqlite-vec 可加载时，提供一个 `vec0` 虚拟表 + `vec_index` 整数 id 映射，
用 SQLite 原生 KNN（`MATCH ... ORDER BY distance`）做近似检索；不可用时返回空，
由调用方回退到现有暴力计算。
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence, Tuple

from .database import Database
from .sqlite_chat_repository import utc_now


class SqliteVecSearch:
    _INDEX_TABLE = "vec_index"
    _VEC_TABLE = "vec_embeddings"

    def __init__(self, database: Database) -> None:
        self._database = database
        self._available: Optional[bool] = None

    # ---- 扩展加载 ----
    def _extension_path(self) -> Optional[str]:
        try:
            import sqlite_vec  # type: ignore

            return sqlite_vec.loadable_path()
        except Exception:
            return None

    def _load_extension(self, connection: sqlite3.Connection) -> None:
        path = self._extension_path()
        if path is None:
            raise RuntimeError("sqlite-vec is not installed")
        connection.enable_load_extension(True)
        connection.load_extension(path)

    def available(self) -> bool:
        if self._available is None:
            try:
                with self._database.connect() as connection:
                    self._load_extension(connection)
                self._available = True
            except Exception:
                self._available = False
        return self._available

    # ---- 建表 ----
    def _ensure_schema(
        self,
        connection: sqlite3.Connection,
        dim: int,
    ) -> None:
        self._load_extension(connection)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {self._INDEX_TABLE} ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "scope TEXT NOT NULL, ref_id TEXT NOT NULL, model TEXT NOT NULL, "
            "UNIQUE (scope, ref_id, model))"
        )
        connection.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._VEC_TABLE} "
            f"USING vec0(embedding float[{dim}])"
        )

    # ---- 写入 ----
    def upsert(
        self,
        scope: str,
        ref_id: str,
        model: str,
        dim: int,
        vector_blob: bytes,
    ) -> None:
        if not self.available() or dim <= 0 or not vector_blob:
            return
        with self._database.transaction() as connection:
            self._ensure_schema(connection, dim)
            row = connection.execute(
                f"SELECT id FROM {self._INDEX_TABLE} "
                "WHERE scope = ? AND ref_id = ? AND model = ?",
                (scope, ref_id, model),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    f"INSERT INTO {self._INDEX_TABLE}(scope, ref_id, model) "
                    "VALUES (?, ?, ?)",
                    (scope, ref_id, model),
                )
                vector_id = cursor.lastrowid
            else:
                vector_id = row["id"]
                connection.execute(
                    f"DELETE FROM {self._VEC_TABLE} WHERE rowid = ?",
                    (vector_id,),
                )
            connection.execute(
                f"INSERT INTO {self._VEC_TABLE}(rowid, embedding) VALUES (?, ?)",
                (vector_id, vector_blob),
            )

    # ---- 查询 ----
    def knn(
        self,
        scope: str,
        model: str,
        dim: int,
        query_blob: bytes,
        k: int = 5,
    ) -> List[Tuple[str, float]]:
        if not self.available() or not query_blob:
            return []
        with self._database.connect() as connection:
            self._ensure_schema(connection, dim)
            rows = connection.execute(
                f"SELECT i.ref_id, v.distance FROM ("
                f"SELECT rowid, distance FROM {self._VEC_TABLE} "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
                f") v JOIN {self._INDEX_TABLE} i ON i.id = v.rowid "
                "WHERE i.scope = ? AND i.model = ? ORDER BY v.distance",
                (query_blob, k, scope, model),
            ).fetchall()
        return [(row["ref_id"], float(row["distance"])) for row in rows]

    # ---- 重建（从 embeddings 表回填） ----
    def rebuild_from_embeddings(
        self,
        scope: str,
        model: str,
        dim: int,
        blobs: Sequence[Tuple[str, bytes]],
    ) -> int:
        if not self.available():
            return 0
        count = 0
        for ref_id, vector in blobs:
            self.upsert(scope, ref_id, model, dim, vector)
            count += 1
        return count
