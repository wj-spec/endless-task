"""工作区内容搜索工具（P1: 17 号切片 ① workspace_search）。

ChatGPT 式文件检索：在锁定工作区根内按正则搜索文本内容（grep 语义），
返回结构化命中（relative path + 行号 + 命中行文本）。只读、无索引、
不依赖 shell（Python 逐行读 + re），来源即文件路径可溯源。

与 list_workspace_dir（结构列举）互补：search=内容、list=结构。
路径安全复用 path_safety.resolve_workspace_path（canonicalize + containment）。
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Optional

from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolActivityCopy,
    ToolApprovalMode,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolResult,
)

from .path_safety import resolve_workspace_path

MAX_RESULTS_DEFAULT = 50
MAX_RESULTS_CAP = 200
MAX_FILE_BYTES_DEFAULT = 1_000_000
MAX_HITS_PER_FILE = 200
MAX_OUTPUT_CHARACTERS = 24_000
SINGLE_LINE_CAP = 200
_DEFAULT_IGNORE_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".hg", ".svn"})


def _looks_binary(raw: bytes) -> bool:
    """启发式二进制探测：首 8KiB 含 NUL 视为二进制（跳过，不算错误）。"""
    return b"\x00" in raw[:8192]


def _decode_line(raw: bytes) -> Optional[str]:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _clip_line(text: str) -> str:
    return text if len(text) <= SINGLE_LINE_CAP else text[:SINGLE_LINE_CAP] + "…"


class WorkspaceSearchTool:
    definition = ToolDefinition(
        name="workspace_search",
        description=(
            "在当前工作区内按正则搜索文件内容（grep 语义）。path 为相对工作区根的"
            "目录或文件（省略=根目录递归）；pattern 为正则表达式；file_pattern 可"
            "限定文件名 glob（如 *.md）；返回命中文件相对路径+行号+命中行文本。"
            "只读操作，不执行 shell。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
                "pattern": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                },
                "file_pattern": {"type": "string", "minLength": 1, "maxLength": 200},
                "case_sensitive": {"type": "boolean"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS_CAP},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        effect=ToolEffect.READ_ONLY,
        approval_mode=ToolApprovalMode.AUTO,
        timeout_seconds=20.0,
        max_output_characters=MAX_OUTPUT_CHARACTERS,
    )

    def __init__(
        self,
        resolver,
        *,
        max_file_bytes: int = MAX_FILE_BYTES_DEFAULT,
        ignore_dirs: frozenset[str] = _DEFAULT_IGNORE_DIRS,
    ) -> None:
        self._resolver = resolver
        self._max_file_bytes = max_file_bytes
        self._ignore_dirs = ignore_dirs

    def activity_copy(self, call: ToolCall) -> ToolActivityCopy:
        return ToolActivityCopy(
            running="正在搜索工作区文件",
            completed="已完成工作区文件搜索",
            failed="工作区文件搜索失败",
        )

    async def execute(self, call: ToolCall, token: CancellationToken) -> ToolResult:
        token.raise_if_cancelled()
        binding = self._resolver.require_binding(call.conversation_id)
        pattern_text = call.require_argument("pattern", str)
        raw_path = call.optional_argument("path", str, ".")
        file_pattern = call.optional_argument("file_pattern", str, None)
        case_sensitive = call.optional_argument("case_sensitive", bool, False)
        max_results = call.optional_argument(
            "max_results", int, MAX_RESULTS_DEFAULT
        )

        resolved = resolve_workspace_path(binding.root, raw_path)
        if not resolved.canonical.exists():
            raise ToolError(
                "path_not_found",
                f"路径不存在：{resolved.original_raw}。",
                retryable=False,
            )
        if not resolved.canonical.is_dir() and not resolved.canonical.is_file():
            raise ToolError(
                "path_not_found",
                f"路径既不是文件也不是目录：{resolved.original_raw}。",
                retryable=False,
            )

        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(pattern_text, flags)
        except re.error as error:
            raise ToolError(
                "invalid_pattern",
                f"正则无效：{error}。",
                retryable=False,
            ) from error

        hits: list[dict[str, object]] = []
        scanned_files = 0
        total_files_with_hits = 0
        truncated = False

        def _search_file(path: Path, relative: str) -> None:
            nonlocal scanned_files, total_files_with_hits, truncated
            token.raise_if_cancelled()
            try:
                stat_result = path.stat()
            except OSError:
                return
            if stat_result.st_size > self._max_file_bytes:
                return  # 超限文件跳过（不报错，可改用 read 分段精读）
            try:
                raw = path.read_bytes()
            except OSError:
                return
            if _looks_binary(raw):
                return
            text = _decode_line(raw)
            if text is None:
                return
            scanned_files += 1
            file_hits: list[dict[str, object]] = []
            for line_index, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line) is None:
                    continue
                file_hits.append(
                    {
                        "line": line_index,
                        "text": _clip_line(line),
                    }
                )
                if len(file_hits) >= MAX_HITS_PER_FILE:
                    truncated = True
                    break
            if file_hits:
                hits.append(
                    {
                        "path": relative,
                        "fileHitCount": len(file_hits),
                        "matches": file_hits,
                    }
                )
                total_files_with_hits += 1

        def _search_dir(path: Path, relative: str) -> None:
            try:
                entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return
            for entry in entries:
                token.raise_if_cancelled()
                name = entry.name
                if entry.is_dir():
                    if name in self._ignore_dirs:
                        continue
                    _search_dir(entry, f"{relative}/{name}" if relative else name)
                elif entry.is_file():
                    if file_pattern is not None and not _glob_match(
                        name, file_pattern
                    ):
                        continue
                    rel = f"{relative}/{name}" if relative else name
                    _search_file(entry, rel)

        if resolved.canonical.is_file():
            workspace_root = binding.root.resolve()
            rel_file = _relative_label(workspace_root, resolved.canonical)
            if file_pattern is None or _glob_match(
                resolved.canonical.name, file_pattern
            ):
                _search_file(resolved.canonical, rel_file)
        else:
            # 相对路径基于工作区根（scope 内部用同根相对标签，保证可溯源一致）。
            workspace_root = binding.root.resolve()
            if resolved.canonical == workspace_root:
                scope_rel = ""
            else:
                scope_rel = _relative_label(workspace_root, resolved.canonical)
            _search_dir(resolved.canonical, scope_rel)

        token.raise_if_cancelled()
        capped = hits[:max_results]
        if len(hits) > max_results:
            truncated = True

        lines: list[str] = []
        if not capped:
            scope_label = resolved.original_raw or "."
            lines.append(
                f"未在 {scope_label} 中找到匹配 pattern={pattern_text!r}"
            )
        for hit in capped:
            lines.append(
                f"[工作区文件 {hit['path']}] {hit['fileHitCount']} 处命中"
            )
            for match in hit["matches"]:  # type: ignore[union-attr]
                lines.append(f"  L{match['line']}: {match['text']}")
        if truncated:
            lines.append("（命中过多，结果已截断；可用 path 收窄范围后重搜）")

        return ToolResult(
            tool_call_id=call.id,
            content="\n".join(lines),
            structured_content={
                "query": pattern_text,
                "scopePath": resolved.original_raw or ".",
                "zeroHit": not capped,
                "totalFilesWithHits": total_files_with_hits,
                "scannedFiles": scanned_files,
                "truncated": truncated,
                "hits": [
                    {
                        "path": hit["path"],
                        "fileHitCount": hit["fileHitCount"],
                        "matches": hit["matches"],
                    }
                    for hit in capped
                ],
            },
        )


def _glob_match(name: str, pattern: str) -> bool:
    """文件名 glob 匹配（* 与 ?；case-sensitive，用 fnmatchcase）。"""
    return fnmatch.fnmatchcase(name, pattern)


def _relative_label(root: Path, canonical: Path) -> str:
    try:
        return canonical.relative_to(root).as_posix()
    except ValueError:
        return canonical.name
