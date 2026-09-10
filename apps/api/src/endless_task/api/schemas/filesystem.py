"""filesystem 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class RevealPathBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sourceConversationId: str
    sourceTurnId: str
    note: Optional[str] = None
