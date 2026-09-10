"""retrieval 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class RetrievalEventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    label: Optional[str] = None
    scope: Optional[str] = None
    refId: Optional[str] = None
    query: Optional[str] = None
    turnId: Optional[str] = None
    conversationId: Optional[str] = None
