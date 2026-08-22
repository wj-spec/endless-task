from __future__ import annotations

from typing import Protocol, Sequence

from .models import StoredTextFile, UploadedTextFile


class TextFileRepository(Protocol):
    def create_file(
        self,
        *,
        conversation_id: str,
        original_name: str,
        media_type: str,
        content: bytes,
    ) -> UploadedTextFile:
        ...

    def list_files(self, conversation_id: str) -> Sequence[UploadedTextFile]:
        ...

    def get_file(self, *, conversation_id: str, file_id: str) -> StoredTextFile:
        ...

    def delete_file(self, *, conversation_id: str, file_id: str) -> None:
        ...
