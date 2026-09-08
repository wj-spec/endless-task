"""B5 用户画像的渲染与刷新策略（纯计算）。

目标：把"用户是谁"压成一段**稳定、轻量、可缓存**的 system 前缀。

缓存/成本纪律（与实现强相关，故写在模块里）：

1. **同一份画像必须渲染成同一串字节**——排序确定、无时间戳、无计数，否则每次
   注入都会让 provider 的 prompt 缓存从这段开始失效；
2. **只在内容真的变了才升版本**——`next_version` 用签名比对，签名不变则版本不变，
   上层因此可以完全跳过写入；
3. **刷新有节流**——`should_refresh` 限制最小间隔，避免"每轮都重算画像"把
   上下文与调用成本顶起来；
4. **体积有上限**——行数与字符数双上限，画像不能无限增长。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError

#: 注入块的表头（固定文案，保证字节稳定）。
PROFILE_HEADER = (
    "以下是关于用户的长期画像（跨会话有效）：可以直接使用，"
    "不要向用户重复确认；若与用户当场说法冲突，以当场说法为准。"
)

DEFAULT_MAX_LINES = 8
DEFAULT_MAX_CHARACTERS = 600
#: 自动刷新最小间隔（秒）：默认 5 分钟，避免频繁变动影响缓存与成本。
DEFAULT_MIN_REFRESH_SECONDS = 300

#: 画像行前缀（保持稳定的列表结构）。
_LINE_PREFIX = "- "


@dataclass(frozen=True)
class ProfileFact:
    """一条画像素材（来自记忆或用户手写）。"""

    content: str
    kind: str = "preference"
    importance: float = 0.5
    source: str = ""


@dataclass(frozen=True)
class UserProfileBlock:
    """可直接注入的画像块（内容 + 版本 + 签名）。"""

    content: str
    version: int
    signature: str
    lines: tuple[str, ...] = ()
    manual: bool = False

    @property
    def empty(self) -> bool:
        return not self.content

    @property
    def characters(self) -> int:
        return len(self.content)

    def as_json(self) -> dict[str, object]:
        return {
            "content": self.content,
            "version": self.version,
            "signature": self.signature,
            "lines": list(self.lines),
            "manual": self.manual,
            "characters": self.characters,
        }


def _normalize(content: str) -> str:
    return " ".join((content or "").split())


def render_profile_lines(
    facts: Iterable[ProfileFact],
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_characters: int = DEFAULT_MAX_CHARACTERS,
) -> tuple[str, ...]:
    """确定性渲染：去重 → 按（重要性, 类型, 内容）排序 → 截断到上限。"""
    if max_lines < 1 or max_characters < 1:
        raise AgentPlatformError(
            "invalid_profile_limits", "profile limits must be positive"
        )
    unique: dict[str, ProfileFact] = {}
    for fact in facts:
        content = _normalize(fact.content)
        if not content:
            continue
        existing = unique.get(content)
        # 同一句话只保留重要性更高的那条，保证渲染结果与输入顺序无关。
        if existing is None or fact.importance > existing.importance:
            unique[content] = ProfileFact(
                content=content,
                kind=fact.kind or "preference",
                importance=float(fact.importance),
                source=fact.source,
            )
    ordered = sorted(
        unique.values(),
        key=lambda item: (-item.importance, item.kind, item.content),
    )
    lines: list[str] = []
    used = 0
    for fact in ordered:
        if len(lines) >= max_lines:
            break
        line = _LINE_PREFIX + fact.content
        if used + len(line) > max_characters:
            break
        lines.append(line)
        used += len(line) + 1
    return tuple(lines)


def profile_signature(lines: Sequence[str]) -> str:
    """画像行的稳定签名（顺序敏感，但渲染顺序本身是确定的）。"""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def render_profile_block(lines: Sequence[str]) -> str:
    """行 → 注入块；空画像返回空串（调用方据此完全不注入）。"""
    if not lines:
        return ""
    return PROFILE_HEADER + "\n" + "\n".join(lines)


def next_version(
    *,
    current_version: int,
    current_signature: Optional[str],
    new_signature: str,
) -> int:
    """签名不变则版本不变（调用方据此跳过写入，保住前缀缓存）。"""
    if current_signature == new_signature:
        return int(current_version)
    return int(current_version) + 1


def should_refresh(
    *,
    last_updated_at: Optional[str],
    now: Optional[str] = None,
    min_interval_seconds: int = DEFAULT_MIN_REFRESH_SECONDS,
    signature_changed: bool = True,
    force: bool = False,
) -> bool:
    """是否允许重算/写入画像：内容没变或间隔未到都不动。"""
    if force:
        return True
    if not signature_changed:
        return False
    if min_interval_seconds <= 0:
        return True
    last = _parse_timestamp(last_updated_at)
    if last is None:
        return True
    moment = _parse_timestamp(now) or datetime.now(timezone.utc)
    return moment - last >= timedelta(seconds=int(min_interval_seconds))


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def facts_from_memories(memories: Iterable[object]) -> tuple[ProfileFact, ...]:
    """把记忆记录转成画像素材（kind/importance 直接复用 B3 的字段）。"""
    facts: list[ProfileFact] = []
    for memory in memories:
        content = str(getattr(memory, "content", "") or "")
        if not content.strip():
            continue
        kind_value = getattr(memory, "kind", "preference")
        facts.append(
            ProfileFact(
                content=content,
                kind=str(getattr(kind_value, "value", kind_value)),
                importance=float(getattr(memory, "importance", 0.5) or 0.5),
                source=str(getattr(memory, "id", "")),
            )
        )
    return tuple(facts)


__all__ = [
    "DEFAULT_MAX_CHARACTERS",
    "DEFAULT_MAX_LINES",
    "DEFAULT_MIN_REFRESH_SECONDS",
    "PROFILE_HEADER",
    "ProfileFact",
    "UserProfileBlock",
    "facts_from_memories",
    "next_version",
    "profile_signature",
    "render_profile_block",
    "render_profile_lines",
    "should_refresh",
]
