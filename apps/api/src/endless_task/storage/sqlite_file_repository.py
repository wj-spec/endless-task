from __future__ import annotations

import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Callable, Sequence

from endless_task.domain.repositories import InvalidStateError, NotFoundError
from endless_task.files import FileError, StoredTextFile, UploadedTextFile

from .database import Database


Clock = Callable[[], str]
IdFactory = Callable[[str], str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00",
        "Z",
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class SqliteTextFileRepository:
    _extensions = {
        ".css": "text/css",
        ".csv": "text/csv",
        ".html": "text/html",
        ".js": "text/javascript",
        ".json": "application/json",
        ".jsx": "text/jsx",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".py": "text/x-python",
        ".toml": "text/toml",
        ".ts": "text/typescript",
        ".tsx": "text/tsx",
        ".tsv": "text/tab-separated-values",
        ".txt": "text/plain",
        ".yaml": "application/yaml",
        ".yml": "application/yaml",
    }

    def __init__(
        self,
        database: Database,
        *,
        max_file_bytes: int = 1_000_000,
        max_files_per_conversation: int = 10,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        if max_file_bytes <= 0 or max_files_per_conversation <= 0:
            raise ValueError("File limits must be positive")
        self._database = database
        self._max_file_bytes = max_file_bytes
        self._max_files_per_conversation = max_files_per_conversation
        self._clock = clock
        self._id_factory = id_factory

    def create_file(
        self,
        *,
        conversation_id: str,
        original_name: str,
        media_type: str,
        content: bytes,
    ) -> UploadedTextFile:
        safe_name = self._safe_name(original_name)
        suffix = PurePosixPath(safe_name).suffix.lower()
        normalized_media_type = self._extensions.get(suffix)
        if normalized_media_type is None:
            raise FileError(
                "unsupported_file_type",
                "当前仅支持常见 UTF-8 文本、Markdown、代码、JSON 和 CSV 文件。",
                status_code=415,
            )
        if len(content) > self._max_file_bytes:
            raise FileError(
                "file_too_large",
                f"文件超过 {self._max_file_bytes} 字节的本地限制。",
                status_code=413,
            )
        if not content:
            raise FileError("empty_file", "不能上传空文件。")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise FileError(
                "unsupported_file_encoding",
                "文件必须使用 UTF-8 编码。",
                status_code=415,
            ) from error
        if "\x00" in text:
            raise FileError(
                "unsupported_file_content",
                "文件包含不支持的二进制内容。",
                status_code=415,
            )
        normalized_content = text.replace("\r\n", "\n").replace("\r", "\n")
        file_id = self._id_factory("file")
        now = self._clock()
        digest = hashlib.sha256(content).hexdigest()

        with self._database.transaction() as connection:
            conversation = connection.execute(
                "SELECT status FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError("Conversation not found")
            if conversation["status"] != "active":
                raise InvalidStateError("Cannot add a file to an archived conversation")
            # 14 B1-iii-c：忙判定以 v2 run 为真源（v1 turns 不再写入）。
            active_run = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active_run:
                raise FileError(
                    "conversation_busy",
                    "请等待当前回答结束后再添加文件。",
                    status_code=409,
                )
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM uploaded_text_files WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["count"]
            if count >= self._max_files_per_conversation:
                raise FileError(
                    "too_many_files",
                    f"每个对话最多保留 {self._max_files_per_conversation} 个文件。",
                    status_code=409,
                )
            connection.execute(
                """
                INSERT INTO uploaded_text_files(
                    id, conversation_id, original_name, media_type, byte_size,
                    sha256, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    conversation_id,
                    safe_name,
                    normalized_media_type,
                    len(content),
                    digest,
                    normalized_content,
                    now,
                ),
            )
            row = self._get_row(connection, conversation_id, file_id)
        return self._metadata(row)

    def list_files(self, conversation_id: str) -> Sequence[UploadedTextFile]:
        with self._database.connect() as connection:
            conversation = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise NotFoundError("Conversation not found")
            rows = connection.execute(
                """
                SELECT * FROM uploaded_text_files
                WHERE conversation_id = ?
                ORDER BY created_at, id
                """,
                (conversation_id,),
            ).fetchall()
        return tuple(self._metadata(row) for row in rows)

    def get_file(self, *, conversation_id: str, file_id: str) -> StoredTextFile:
        with self._database.connect() as connection:
            try:
                row = self._get_row(connection, conversation_id, file_id)
            except NotFoundError as error:
                raise FileError(
                    "file_not_found",
                    "找不到当前会话中已授权的文件。",
                    status_code=404,
                ) from error
        return StoredTextFile(metadata=self._metadata(row), content=row["content"])

    def delete_file(self, *, conversation_id: str, file_id: str) -> None:
        with self._database.transaction() as connection:
            try:
                self._get_row(connection, conversation_id, file_id)
            except NotFoundError as error:
                raise FileError(
                    "file_not_found",
                    "找不到当前会话中已授权的文件。",
                    status_code=404,
                ) from error
            conversation = connection.execute(
                "SELECT status FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if conversation["status"] != "active":
                raise FileError(
                    "conversation_archived",
                    "归档对话中的文件不能修改。",
                    status_code=409,
                )
            # 14 B1-iii-c：忙判定以 v2 run 为真源（v1 turns 不再写入）。
            active_run = connection.execute(
                """
                SELECT 1 FROM v2_runs
                WHERE conversation_id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if active_run:
                raise FileError(
                    "conversation_busy",
                    "请等待当前回答结束后再移除文件。",
                    status_code=409,
                )
            connection.execute(
                "DELETE FROM uploaded_text_files WHERE id = ? AND conversation_id = ?",
                (file_id, conversation_id),
            )

    @staticmethod
    def _safe_name(original_name: str) -> str:
        if not isinstance(original_name, str):
            raise FileError("invalid_file_name", "文件名无效。")
        normalized = original_name.replace("\\", "/")
        if "/" in normalized:
            raise FileError("invalid_file_name", "文件名不能包含路径。")
        name = normalized.strip()
        if (
            not name
            or name in {".", ".."}
            or len(name) > 255
            or re.search(r"[\x00-\x1f\x7f]", name)
        ):
            raise FileError("invalid_file_name", "文件名无效。")
        return name

    @staticmethod
    def _get_row(
        connection: sqlite3.Connection,
        conversation_id: str,
        file_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM uploaded_text_files
            WHERE id = ? AND conversation_id = ?
            """,
            (file_id, conversation_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("File not found")
        return row

    @staticmethod
    def _metadata(row: sqlite3.Row) -> UploadedTextFile:
        return UploadedTextFile(
            id=row["id"],
            conversation_id=row["conversation_id"],
            original_name=row["original_name"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            created_at=row["created_at"],
        )
