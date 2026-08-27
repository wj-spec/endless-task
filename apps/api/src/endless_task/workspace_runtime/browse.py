"""工作区目录浏览（只读）：供前端目录选择器使用。

只列目录/文件元数据，不读文件内容；支持从任意绝对路径浏览
（选择器需要跨目录导航到目标项目），隐藏文件默认不展示。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from endless_task.domain.repositories import ValidationError

MAX_BROWSE_ITEMS = 200
_HIDDEN_PREFIXES = (".",)


@dataclass(frozen=True)
class BrowseItem:
    name: str
    path: str
    kind: str  # directory | file
    size: int = 0
    writable: bool = True
    is_hidden: bool = False


def _is_hidden(name: str) -> bool:
    return name.startswith(_HIDDEN_PREFIXES)


def browse_directory(
    raw_path: Optional[str],
    *,
    show_hidden: bool = False,
    home: Optional[Path] = None,
) -> tuple[str, Sequence[BrowseItem]]:
    base = Path(raw_path or "").expanduser() if raw_path else (home or Path.home())
    try:
        resolved = base.resolve(strict=True)
    except OSError as error:
        raise ValidationError(f"目录不存在或无法访问：{base}") from error
    if not resolved.is_dir():
        raise ValidationError("路径不是目录。")
    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as error:
        raise ValidationError(f"无法读取目录：{resolved}") from error
    items: list[BrowseItem] = []
    for entry in entries:
        if len(items) >= MAX_BROWSE_ITEMS:
            break
        name = entry.name
        hidden = _is_hidden(name)
        if hidden and not show_hidden:
            continue
        try:
            is_dir = entry.is_dir()
            size = 0 if is_dir else entry.stat().st_size
            writable = os.access(entry, os.W_OK)
        except OSError:
            continue
        items.append(
            BrowseItem(
                name=name,
                path=str(entry),
                kind="directory" if is_dir else "file",
                size=size,
                writable=writable,
                is_hidden=hidden,
            )
        )
    return str(resolved), tuple(items)
