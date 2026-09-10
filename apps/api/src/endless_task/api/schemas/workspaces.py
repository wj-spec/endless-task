"""workspaces 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class TerminalCreateBody(BaseModel):
    """S4 内嵌终端：创建 PTY 会话的可选参数。"""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    rows: Optional[int] = None
    cols: Optional[int] = None


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    rootPath: Optional[str] = None


class WorkspaceFileWriteBody(BaseModel):
    """P1 文件编辑保存：内容 + 打开时的版本 token（乐观并发）。"""

    model_config = ConfigDict(extra="forbid")

    content: str
    version: Optional[str] = None


class WorkspacePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rootPath: Optional[str] = None
