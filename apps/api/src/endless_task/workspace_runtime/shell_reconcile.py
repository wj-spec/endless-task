"""S8 shell 变更对账：把 shell 造成的文件改动变成可撤销、可按路径审计的记录。

shell 命令是黑盒（见 06 立场稿 §3），无法逐条解析它会改哪些文件。这里用
"命令前后扫描工作区"的办法补齐可观测性：

- 扫描只记录 ``(mtime_ns, size)``，命中变化才继续读内容；
- 变更前内容从 run checkpoint 的内容寻址 blob 读取（执行前已快照）；
- 结果供上层写 undo journal（可撤销）与 effect log（可按路径审计）。

扫描有预算（文件数上限）；超出预算时放弃对账而不是拖慢命令——放弃会显式
写进工具结果，不静默。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

# 与 checkpoint 的跳过集合保持一致：这些目录既不进快照，也不进对账。
SKIP_PREFIXES: tuple[str, ...] = (
    ".git",
    ".next",
    ".endless-task",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
)

DEFAULT_MAX_FILES = 5_000
MAX_RECONCILE_TEXT_BYTES = 512_000

ChangeKind = Literal["created", "modified", "deleted"]


@dataclass(frozen=True)
class FileState:
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ReconciledChange:
    relative: str
    kind: ChangeKind


def _skipped(relative: str, skip_prefixes: tuple[str, ...]) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in skip_prefixes
    )


def scan_workspace(
    root: Path,
    *,
    skip_prefixes: tuple[str, ...] = SKIP_PREFIXES,
    max_files: int = DEFAULT_MAX_FILES,
) -> Optional[dict[str, FileState]]:
    """扫描工作区文件状态；超过 ``max_files`` 返回 None（放弃对账）。"""
    base = Path(root).expanduser()
    if not base.is_dir():
        return None
    states: dict[str, FileState] = {}
    for path in base.rglob("*"):
        if len(states) >= max_files:
            return None
        try:
            if not path.is_file():
                continue
            relative = path.relative_to(base).as_posix()
            if _skipped(relative, skip_prefixes):
                continue
            stat_result = path.stat()
        except OSError:
            continue
        states[relative] = FileState(
            mtime_ns=stat_result.st_mtime_ns,
            size=stat_result.st_size,
        )
    return states


def changed_paths(
    before: Optional[dict[str, FileState]],
    after: Optional[dict[str, FileState]],
) -> list[ReconciledChange]:
    """比较两次扫描结果；任一侧缺失（超预算/不可用）时返回空列表。"""
    if before is None or after is None:
        return []
    changes: list[ReconciledChange] = []
    for relative, state in after.items():
        previous = before.get(relative)
        if previous is None:
            changes.append(ReconciledChange(relative, "created"))
        elif previous != state:
            changes.append(ReconciledChange(relative, "modified"))
    for relative in before:
        if relative not in after:
            changes.append(ReconciledChange(relative, "deleted"))
    changes.sort(key=lambda item: item.relative)
    return changes


def read_text_bounded(path: Path) -> Optional[str]:
    """读取 UTF-8 文本；不存在/二进制/超限 → None。"""
    try:
        if not path.is_file() or path.stat().st_size > MAX_RECONCILE_TEXT_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
