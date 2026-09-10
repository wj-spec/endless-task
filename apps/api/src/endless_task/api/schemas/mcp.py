"""mcp 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class McpServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = {}
    enabled: bool = True
    toolCallTimeoutSeconds: float = 60.0


class McpServerPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    transport: Optional[Literal["stdio", "http"]] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None
    toolCallTimeoutSeconds: Optional[float] = None
