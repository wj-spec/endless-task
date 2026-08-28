"""工作区路径安全：canonicalize + containment，macOS 读取变体容错。

所有 fs 工具共用本模块，路径校验集中一处（参考 Pydantic AI 单根模式）。
写入分支不做变体猜测（避免写错文件名）；读取分支尝试 macOS 变体
（NFD 归一化、AM/PM 窄空格、弯引号），参考 pi path-utils。
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from endless_task.tooling import ToolError

NARROW_NO_BREAK_SPACE = "\u202F"
_AM_PM_RE = None  # 惰性编译见 _am_pm_variant


def _am_pm_variant(path: str) -> str:
    import re

    return re.sub(r" (AM|PM)\.", f"{NARROW_NO_BREAK_SPACE}\\1.", path)


@dataclass(frozen=True)
class ResolvedPath:
    canonical: Path
    original_raw: str
    variant: str = "exact"  # exact | nfd | ampm | curly | combined


def resolve_external_read_path(
    roots: Tuple[Path, ...], raw_path: str
) -> ResolvedPath:
    """在多个只读根中解析绝对路径；仅用于技能正文等受控读取。"""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError("invalid_path", "路径不能为空。", retryable=False)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ToolError(
            "path_escape", "只接受绝对路径。", retryable=False
        )
    canonical = candidate.resolve(strict=False)
    for root in roots:
        root = root.expanduser().resolve(strict=False)
        if _is_within(root, canonical):
            return ResolvedPath(canonical=canonical, original_raw=str(candidate))
    raise ToolError(
        "path_escape", "路径越出允许的只读目录，已拒绝。", retryable=False
    )


def _is_within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_workspace_path(root: Path, raw_path: str) -> ResolvedPath:
    """词法拒绝 + canonicalize + containment 校验（读写共用，严格分支）。

    参数为相对工作区根的路径；绝对路径与 `..` 片段直接拒绝。
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError(
            "invalid_path",
            "路径不能为空。",
            retryable=False,
        )
    normalized = raw_path.strip()
    if normalized.startswith("/") or normalized.startswith("\\"):
        raise ToolError(
            "path_escape",
            "只接受相对工作区的路径，不接受绝对路径。",
            retryable=False,
        )
    parts = [p for p in normalized.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ToolError(
            "path_escape",
            "路径不允许包含 .. 片段。",
            retryable=False,
        )
    root = root.resolve()
    candidate = root
    for part in parts:
        candidate = candidate / part
    canonical = candidate.resolve(strict=False)
    if not _is_within(root, canonical):
        raise ToolError(
            "path_escape",
            "路径越出工作区根目录，已拒绝。",
            retryable=False,
        )
    return ResolvedPath(canonical=canonical, original_raw=normalized)


def resolve_read_path_with_variants(root: Path, raw_path: str) -> ResolvedPath:
    """读取分支：先严格解析，文件不存在时尝试 macOS 变体容错。

    变体命中后在来源标签注明实际路径（由调用方读取 variant 字段）。
    """
    resolved = resolve_workspace_path(root, raw_path)
    if resolved.canonical.exists():
        return resolved
    candidate_variants = _macos_variants(resolved.original_raw)
    for label, candidate_raw in candidate_variants:
        try:
            candidate = resolve_workspace_path(root, candidate_raw)
        except ToolError:
            continue
        if candidate.canonical.exists():
            return ResolvedPath(
                canonical=candidate.canonical,
                original_raw=candidate_raw,
                variant=label,
            )
    return resolved


def _macos_variants(raw_path: str) -> list[Tuple[str, str]]:
    variants: list[Tuple[str, str]] = []
    ampm = _am_pm_variant(raw_path)
    if ampm != raw_path:
        variants.append(("ampm", ampm))
    nfd = unicodedata.normalize("NFD", raw_path)
    if nfd != raw_path:
        variants.append(("nfd", nfd))
    curly = raw_path.replace("'", "\u2019")
    if curly != raw_path:
        variants.append(("curly", curly))
    nfd_curly = unicodedata.normalize("NFD", raw_path).replace("'", "\u2019")
    if nfd_curly not in (raw_path, nfd, curly):
        variants.append(("combined", nfd_curly))
    return variants


def validate_bind_root(root: Path) -> Optional[str]:
    """工作区根绑定校验；返回 None 表示可绑定，否则返回用户可读原因。

    拒绑：不存在/不可读/不可写、文件系统根、家目录、应用数据目录、系统目录。
    """
    try:
        resolved = root.expanduser().resolve(strict=True)
    except OSError:
        return "目录不存在或无法访问。"
    if not resolved.is_dir():
        return "选择的不是目录。"
    if not os.access(resolved, os.R_OK | os.W_OK):
        return "目录不可读或不可写。"
    home = Path.home().resolve()
    if resolved == home:
        return "不能把整个家目录作为工作区根。"
    if resolved == Path(resolved.anchor).resolve():
        return "不能把文件系统根目录作为工作区根。"
    data_dir = os.environ.get("ENDLESS_TASK_DATA_DIR")
    if data_dir:
        data = Path(data_dir).expanduser().resolve()
        if resolved == data or _is_within(data, resolved) or _is_within(resolved, data):
            return "不能把应用数据目录作为工作区根。"
    return None
