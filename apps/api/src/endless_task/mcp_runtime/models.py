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
