"""Artifact 文件事实源（R5.12a）：工作区 Artifact 落盘 + 版本快照。

文件路径约定（相对工作区根）：
- 主文件：`.endless-task/artifacts/<artifact_id>/<safe_title>.<ext>`
- 版本快照：`.endless-task/versions/<artifact_id>/v<ordinal>.<ext>`

DB 仍双写内容（读侧快速路径与回滚兜底）；文件是用户可见的事实源。
文件写失败不阻断 DB 事务（内容完整），下次写入时幂等覆盖重试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from endless_task.domain.models import ArtifactKind

from .effect_log import sha256_text
from .resolver import WorkspaceBinding, WorkspaceResolver

_INVALID_FILENAME = re.compile(r"[/\\\x00-\x1f]")
_EXTENSIONS = {
    ArtifactKind.MARKDOWN: ".md",
    ArtifactKind.TEXT: ".txt",
}


@dataclass(frozen=True)
class ArtifactFilePlan:
    storage_path: str  # 相对工作区根的主文件路径
    version_storage_path: Optional[str]  # 快照路径（更新时）
    content_sha256: str


def _safe_title(title: str) -> str:
    cleaned = _INVALID_FILENAME.sub("_", title.strip())
    cleaned = cleaned.replace("..", "_")
    return (cleaned or "artifact")[:80]


def _ext_for(kind: ArtifactKind) -> str:
    return _EXTENSIONS.get(kind, ".md")


def plan_new(
    binding: WorkspaceBinding,
    artifact_id: str,
    title: str,
    kind: ArtifactKind,
    content: str,
) -> ArtifactFilePlan:
    name = f"{_safe_title(title)}{_ext_for(kind)}"
    storage_path = f".endless-task/artifacts/{artifact_id}/{name}"
    return ArtifactFilePlan(
        storage_path=storage_path,
        version_storage_path=None,
        content_sha256=sha256_text(content),
    )


def plan_update(
    binding: WorkspaceBinding,
    artifact_id: str,
    title: str,
    kind: ArtifactKind,
    content: str,
    *,
    new_ordinal: int,
    existing_storage_path: Optional[str],
) -> ArtifactFilePlan:
    name = f"{_safe_title(title)}{_ext_for(kind)}"
    storage_path = (
        existing_storage_path
        if existing_storage_path
        else f".endless-task/artifacts/{artifact_id}/{name}"
    )
    version_storage_path = (
        f".endless-task/versions/{artifact_id}/v{max(1, new_ordinal - 1)}{_ext_for(kind)}"
    )
    return ArtifactFilePlan(
        storage_path=storage_path,
        version_storage_path=version_storage_path,
        content_sha256=sha256_text(content),
    )


class ArtifactFileStore:
    """工作区 Artifact 文件落盘；未绑定工作区时 no-op（返回 None 计划）。"""

    def __init__(self, resolver: WorkspaceResolver) -> None:
        self._resolver = resolver

    def binding_for(self, conversation_id: str) -> Optional[WorkspaceBinding]:
        return self._resolver.resolve_binding(conversation_id)

    def materialize(
        self,
        binding: WorkspaceBinding,
        plan: ArtifactFilePlan,
        content: str,
        *,
        previous_content: Optional[str] = None,
    ) -> None:
        root = binding.root
        main_path = root / plan.storage_path
        if plan.version_storage_path and previous_content is not None:
            version_path = root / plan.version_storage_path
            try:
                version_path.parent.mkdir(parents=True, exist_ok=True)
                version_path.write_text(previous_content, encoding="utf-8")
            except OSError:
                pass  # 快照失败不阻断；DB 内容仍完整
        main_path.parent.mkdir(parents=True, exist_ok=True)
        main_path.write_text(content, encoding="utf-8")

    def restore(
        self,
        binding: WorkspaceBinding,
        storage_path: str,
        content: str,
    ) -> None:
        main_path = binding.root / storage_path
        main_path.parent.mkdir(parents=True, exist_ok=True)
        main_path.write_text(content, encoding="utf-8")
