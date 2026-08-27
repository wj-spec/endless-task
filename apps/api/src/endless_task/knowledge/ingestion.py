"""R5.7 知识文件摄入健壮性：API 侧解析上传字节。

策略（产品定位：本地工作台，读取必须健壮、失败必须显式）：
- 编码：UTF-8 严格 → GB18030 回退，仍失败明确拒绝。
- 二进制：按 NUL 字节比例判定，拒绝。
- 大小：原文 > 5MB 拒绝；提取文本 > 20 万字符截断并提示。
- 格式：白名单 txt/md/markdown/csv/tsv/json/jsonl/log。
- 溯源：记录 size 与 sha256。
- 空内容拒绝。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

#: 原文字节上限（5MB）。
MAX_FILE_BYTES = 5 * 1000 * 1000
#: 提取文本字符上限（20 万字符），超限截断。
MAX_EXTRACTED_CHARS = 200_000
#: NUL 字节比例超过该阈值判为二进制。
BINARY_NUL_RATIO = 0.01
#: 首期格式白名单（小写扩展名，不含点）。
ALLOWED_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "log"}
)

# 各编码回退链：严格 UTF-8 优先，其次 GB18030（兼容 GBK/GB2312）。
_DECODERS = ("utf-8", "gb18030")


class IngestionError(Exception):
    """摄入失败；`code` 供 API 映射，`safe_message` 面向用户。"""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True)
class IngestedFile:
    text: str
    encoding: str
    truncated: bool
    size: int
    sha256: str
    extension: str


def split_extension(file_name: str) -> str:
    name = (file_name or "").strip()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].strip().lower()


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    return sample.count(0) / len(sample) > BINARY_NUL_RATIO


def ingest_file_bytes(raw: bytes, *, file_name: str) -> IngestedFile:
    """把上传字节解析为可入库文本；失败抛 IngestionError。"""
    extension = split_extension(file_name)
    if extension not in ALLOWED_EXTENSIONS:
        allowed = "、".join(sorted(ALLOWED_EXTENSIONS))
        raise IngestionError(
            "unsupported_format",
            f"暂只支持这些格式的文件：{allowed}。",
        )
    size = len(raw)
    if size == 0:
        raise IngestionError("empty_file", "文件是空的，没有可读取的内容。")
    if size > MAX_FILE_BYTES:
        raise IngestionError(
            "file_too_large",
            "文件超过 5MB 上限，请拆分或精简后再添加。",
        )
    if _looks_binary(raw[:8192]):
        raise IngestionError(
            "binary_file", "这个文件看起来是二进制内容，暂只支持纯文本文件。"
        )
    text: Optional[str] = None
    used_encoding = ""
    for decoder in _DECODERS:
        try:
            text = raw.decode(decoder)
            used_encoding = decoder
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise IngestionError(
            "undecodable",
            "无法识别文件编码（请按 UTF-8 或 GB18030 保存后重试）。",
        )
    truncated = False
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS]
        truncated = True
    stripped = text.strip()
    if not stripped:
        raise IngestionError("empty_file", "文件内容为空，没有可保存的文本。")
    return IngestedFile(
        text=text,
        encoding=used_encoding,
        truncated=truncated,
        size=size,
        sha256=hashlib.sha256(raw).hexdigest(),
        extension=extension,
    )
