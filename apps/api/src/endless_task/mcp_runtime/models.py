from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class McpToolStatus:
    public_name: str
    raw_name: str
    description: str
    effect: str
    requires_explicit_confirmation: bool
    #: F2（契约新增）：该工具是否在服务器白名单内（``True`` = 已注册进工具注册表）。
    #: 被挡掉的工具**仍然出现在列表里**，只是这个标记为 False——否则用户收窄白名单后
    #: 就看不到被排除的工具、无法重新放行。默认 True 让既有构造点保持"全部允许"。
    allowed: bool = True


@dataclass(frozen=True)
class McpServerRuntimeStatus:
    server_id: str
    name: str
    transport: str
    enabled: bool
    state: str
    tool_count: int
    last_error: Optional[str] = None
    tools: tuple[McpToolStatus, ...] = field(default_factory=tuple)
