from __future__ import annotations

from dataclasses import dataclass


class FileError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, status_code: int = 400) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code


@dataclass(frozen=True)
class UploadedTextFile:
    id: str
    conversation_id: str
    original_name: str
    media_type: str
    byte_size: int
    sha256: str
    created_at: str


@dataclass(frozen=True)
class StoredTextFile:
    metadata: UploadedTextFile
    content: str
