"""knowledge 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class KnowledgeSourceBody(BaseModel):
    kind: str
    title: str
    content: str
    fileName: Optional[str] = None
    expiresAt: Optional[str] = None
    workspaceId: Optional[str] = None


class KnowledgeSourcePatch(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    fileName: Optional[str] = None
    expiresAt: Optional[str] = None
