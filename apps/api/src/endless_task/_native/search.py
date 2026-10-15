"""L2 适配层：检索内核的原生后端。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import _native as binding
from ..workspace_runtime.search_backend import (
    SearchHit,
    SearchOptions,
    SearchOutcome,
    SearchPatternError,
)

SEPARATOR = chr(31)


class NativeSearchBackend:
    "Rust 内核后端。扩展不可用时 raise（fail-closed，不静默回落）。"

    name = "rust-native"

    def search(
        self,
        root: Path,
        pattern: str,
        *,
        case_sensitive: bool = True,
        options: Optional[SearchOptions] = None,
    ) -> SearchOutcome:
        selected = options or SearchOptions()
        payload = binding.require()

        classified = str(payload.search_classify_unsupported(pattern))
        if classified:
            construct, _, needle = classified.partition(SEPARATOR)
            raise SearchPatternError(
                "unsupported_pattern_syntax",
                "unsupported_pattern_syntax: " + construct
                + " is not supported by the regex subset",
                construct=construct,
                needle=needle,
            )

        try:
            rows = payload.search_workspace(
                str(root),
                pattern,
                case_sensitive,
                int(selected.max_results),
                int(selected.max_file_bytes),
                int(selected.max_hits_per_file),
                list(selected.file_suffixes),
            )
        except ValueError as error:
            raise SearchPatternError(
                "invalid_pattern",
                "invalid_pattern: " + str(error),
            ) from error

        meta = str(rows[0]).split(SEPARATOR)
        hits = []
        for row in rows[1:]:
            path, line_number, line_text = str(row).split(SEPARATOR, 2)
            hits.append(SearchHit(path, int(line_number), line_text))
        return SearchOutcome(
            hits=tuple(hits),
            files_scanned=int(meta[1]),
            files_skipped=int(meta[2]),
            truncated=meta[3] == "true",
        )


    def search_tree(
        self,
        scope: Path,
        label_root: Path,
        pattern: str,
        *,
        case_sensitive: bool = True,
        max_files_with_hits: int = 50,
        max_hits_per_file: int = 200,
        max_file_bytes: int = 1_000_000,
        file_glob: Optional[str] = None,
    ) -> tuple:
        "树形检索：按文件分组；标签相对 label_root（对齐工具契约）。"
        payload = binding.require()
        classified = str(payload.search_classify_unsupported(pattern))
        if classified:
            construct, _, needle = classified.partition(SEPARATOR)
            raise SearchPatternError(
                "unsupported_pattern_syntax",
                "unsupported_pattern_syntax: " + construct
                + " is not supported by the regex subset",
                construct=construct,
                needle=needle,
            )
        try:
            rows = payload.search_tree_workspace(
                str(scope),
                str(label_root),
                pattern,
                case_sensitive,
                int(max_files_with_hits),
                int(max_file_bytes),
                int(max_hits_per_file),
                file_glob,
            )
        except ValueError as error:
            raise SearchPatternError(
                "invalid_pattern",
                "invalid_pattern: " + str(error),
            ) from error

        meta = str(rows[0]).split(SEPARATOR)
        files: list = []
        for row in rows[1:]:
            parts = str(row).split(SEPARATOR, 2)
            if parts[0] == "file":
                files.append((parts[1], []))
            elif parts[0] == "match" and files:
                files[-1][1].append((int(parts[1]), parts[2]))
        return files, int(meta[1]), meta[2] == "true"
