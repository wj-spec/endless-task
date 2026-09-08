"""B5 用户画像服务：会话开始注入、结束更新，且**不轻易改动前缀**。

缓存/成本纪律（本服务的核心约束）：

* 画像块放在 system 前缀里，**同一版本必须字节一致**——渲染由纯函数完成，
  排序固定、无时间戳、无计数；
* **签名不变就不写库、不升版本**（`next_version`）：前缀不动，provider 的
  prompt 缓存继续命中；
* **刷新节流**：`DEFAULT_MIN_REFRESH_SECONDS`（默认 5 分钟）内即使内容变了也先
  不重写，避免一轮对话里反复变动导致成本激增；
* **用户手写优先**：`manual=1` 的画像不被自动重建覆盖（除非显式 force）；
* **体积有上限**：行数与字符数双上限（默认 8 行 / 600 字符）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence

from endless_task.runtime_v2.user_profile import (
    DEFAULT_MAX_CHARACTERS,
    DEFAULT_MAX_LINES,
    DEFAULT_MIN_REFRESH_SECONDS,
    UserProfileBlock,
    facts_from_memories,
    next_version,
    profile_signature,
    render_profile_block,
    render_profile_lines,
    should_refresh,
)
from endless_task.storage import (
    GENERAL_SCOPE_KEY,
    SqliteRuntimeV2MemoryRepository,
    SqliteUserProfileRepository,
)

#: 参与画像的用户级记忆作用域。
PROFILE_SCOPES = ("user_global", "workspace")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProfileRefreshResult:
    block: UserProfileBlock
    refreshed: bool
    version_changed: bool
    reason: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            **self.block.as_json(),
            "refreshed": self.refreshed,
            "versionChanged": self.version_changed,
            "reason": self.reason,
        }


class UserProfileService:
    def __init__(
        self,
        *,
        profile_repository: SqliteUserProfileRepository,
        memory_repository: SqliteRuntimeV2MemoryRepository,
        clock: Callable[[], str] = _utc_now,
        max_lines: int = DEFAULT_MAX_LINES,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        min_refresh_seconds: int = DEFAULT_MIN_REFRESH_SECONDS,
    ) -> None:
        self._profiles = profile_repository
        self._memories = memory_repository
        self._clock = clock
        self._max_lines = max_lines
        self._max_characters = max_characters
        self._min_refresh_seconds = min_refresh_seconds

    # ---------- 读取（注入路径，只读一行，不做计算） ----------

    def block_for(self, scope_key: str = GENERAL_SCOPE_KEY) -> UserProfileBlock:
        """注入用：读已存画像并拼成块；未存过或为空返回空块。"""
        record = self._profiles.get(scope_key)
        if record is None or not record.lines:
            return UserProfileBlock(content="", version=0, signature="")
        return UserProfileBlock(
            content=render_profile_block(record.lines),
            version=record.version,
            signature=record.signature,
            lines=record.lines,
            manual=record.manual,
        )

    # ---------- 运行前对齐（缓存友好 + 及时纠错） ----------

    def ensure_current(
        self, scope_key: str = GENERAL_SCOPE_KEY
    ) -> UserProfileBlock:
        """注入前对齐：内容没变就什么都不做；变了按策略决定是否立刻写。

        * **删除类变化**（记忆被取代/过期 → 某行消失）：立刻更新，避免用旧画像
          误导后续回答——宁可让这次前缀变一次，也不要留错；
        * **新增类变化**：遵守最小间隔（默认 5 分钟），避免频繁变动把提示缓存
          与调用成本顶起来；
        * 签名未变：直接返回已存画像，**不写库、不升版本**。
        """
        current = self._profiles.get(scope_key)
        facts = facts_from_memories(self._profile_memories(scope_key))
        lines = render_profile_lines(
            facts,
            max_lines=self._max_lines,
            max_characters=self._max_characters,
        )
        signature = profile_signature(lines)
        if current is not None and current.signature == signature:
            return self.block_for(scope_key)
        if current is not None and current.manual:
            return self.block_for(scope_key)
        # 只要有行**消失**（记忆被取代/过期/删除）就立刻更新：旧画像会误导回答，
        # 这种情况下宁可让前缀变一次，也不要把过期信息继续注入。
        lines_removed = bool(current) and bool(set(current.lines) - set(lines))
        result = self.refresh(scope_key, force=lines_removed)
        return result.block

    # ---------- 刷新（会话开始/结束或手动） ----------

    def refresh(
        self,
        scope_key: str = GENERAL_SCOPE_KEY,
        *,
        force: bool = False,
    ) -> ProfileRefreshResult:
        current = self._profiles.get(scope_key)
        if current is not None and current.manual and not force:
            block = self.block_for(scope_key)
            return ProfileRefreshResult(
                block=block,
                refreshed=False,
                version_changed=False,
                reason="manual_profile_kept",
            )
        facts = facts_from_memories(self._profile_memories(scope_key))
        lines = render_profile_lines(
            facts,
            max_lines=self._max_lines,
            max_characters=self._max_characters,
        )
        signature = profile_signature(lines)
        current_signature = current.signature if current else None
        if not lines and current is None:
            # 没有素材也没有历史画像 → 不写库、不占版本（避免无意义的前缀变更）。
            return ProfileRefreshResult(
                block=UserProfileBlock(content="", version=0, signature=""),
                refreshed=False,
                version_changed=False,
                reason="no_profile_facts",
            )
        changed = signature != current_signature
        if not changed:
            # 内容一字未变：连库都不写（force 也不写），前缀缓存因此保持命中。
            return ProfileRefreshResult(
                block=self.block_for(scope_key),
                refreshed=False,
                version_changed=False,
                reason="unchanged",
            )
        if not should_refresh(
            last_updated_at=current.updated_at if current else None,
            now=self._clock(),
            min_interval_seconds=self._min_refresh_seconds,
            signature_changed=changed,
            force=force,
        ):
            return ProfileRefreshResult(
                block=self.block_for(scope_key),
                refreshed=False,
                version_changed=False,
                reason="throttled",
            )
        version = next_version(
            current_version=current.version if current else 0,
            current_signature=current_signature,
            new_signature=signature,
        )
        record = self._profiles.upsert(
            scope_key=scope_key,
            content=render_profile_block(lines),
            lines=lines,
            signature=signature,
            version=version,
            manual=False,
            source_memory_ids=[
                fact.source for fact in facts if fact.source
            ],
        )
        return ProfileRefreshResult(
            block=UserProfileBlock(
                content=record.content,
                version=record.version,
                signature=record.signature,
                lines=record.lines,
            ),
            refreshed=True,
            version_changed=changed,
            reason="rebuilt_from_memories",
        )

    # ---------- 用户手写 ----------

    def set_manual(
        self,
        content: str,
        scope_key: str = GENERAL_SCOPE_KEY,
    ) -> ProfileRefreshResult:
        """用户手写画像：整段替换，标记 manual，版本 +1。"""
        lines = tuple(
            line.rstrip()
            for line in (content or "").splitlines()
            if line.strip()
        )
        normalized_lines = tuple(
            line if line.startswith("- ") else f"- {line.lstrip('- ').strip()}"
            for line in lines
        )
        signature = profile_signature(normalized_lines)
        current = self._profiles.get(scope_key)
        version = next_version(
            current_version=current.version if current else 0,
            current_signature=current.signature if current else None,
            new_signature=signature,
        )
        record = self._profiles.upsert(
            scope_key=scope_key,
            content=render_profile_block(normalized_lines),
            lines=normalized_lines,
            signature=signature,
            version=version,
            manual=True,
        )
        return ProfileRefreshResult(
            block=UserProfileBlock(
                content=record.content,
                version=record.version,
                signature=record.signature,
                lines=record.lines,
                manual=True,
            ),
            refreshed=True,
            version_changed=True,
            reason="manual_update",
        )

    # ---------- 素材 ----------

    def _profile_memories(self, scope_key: str) -> Sequence[object]:
        """画像素材：user_global（+ 该工作区）的活跃 v2 记忆，按最近更新排序。

        与 v2 运行时同源（v2 请求只认 v2 记忆），排序稳定 → 渲染字节稳定。
        """
        return self._memories.list_memories_for_profile(
            workspace_id=None if scope_key == GENERAL_SCOPE_KEY else scope_key,
            limit=self._max_lines * 4,
        )


__all__ = [
    "PROFILE_SCOPES",
    "ProfileRefreshResult",
    "UserProfileService",
]
