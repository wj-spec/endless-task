"""R5.9 长文分块：file 知识源按段落/小节切分为检索单元（纯函数，无外部依赖）。

纯函数、确定性：段落（空行分隔）为最小累积单位，Markdown 标题行开启新块；
超长段落按上限硬切。记忆/对话/笔记源维持现有粒度（不走分块）。
"""

from __future__ import annotations

from typing import List

DEFAULT_CHUNK_MAX_CHARS = 800


def chunk_text(content: str, max_chars: int = DEFAULT_CHUNK_MAX_CHARS) -> List[str]:
    cleaned = (content or "").strip()
    if not cleaned:
        return []
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    chunks: List[str] = []
    current_parts: List[str] = []
    current_length = 0

    def flush() -> None:
        nonlocal current_parts, current_length
        if current_parts:
            chunks.append("\n\n".join(current_parts))
            current_parts = []
            current_length = 0

    for paragraph in _paragraphs(cleaned):
        is_heading = paragraph.lstrip().startswith("#")
        if is_heading and current_parts:
            flush()
        for piece in _split_long_paragraph(paragraph, max_chars):
            piece_length = len(piece)
            if current_parts and current_length + piece_length + 2 > max_chars:
                flush()
            current_parts.append(piece)
            current_length += piece_length + 2
            if current_length >= max_chars:
                flush()
    flush()
    return chunks


def _paragraphs(content: str) -> List[str]:
    parts: List[str] = []
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.strip():
            parts.append(line)
        elif parts and parts[-1] != "":
            parts.append("")
    paragraphs: List[str] = []
    buffer: List[str] = []
    for part in parts:
        if part == "":
            if buffer:
                paragraphs.append("\n".join(buffer))
                buffer = []
        else:
            buffer.append(part)
    if buffer:
        paragraphs.append("\n".join(buffer))
    return [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]


def _split_long_paragraph(paragraph: str, max_chars: int) -> List[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    pieces: List[str] = []
    remaining = paragraph
    while len(remaining) > max_chars:
        pieces.append(remaining[:max_chars])
        remaining = remaining[max_chars:]
    if remaining:
        pieces.append(remaining)
    return pieces
