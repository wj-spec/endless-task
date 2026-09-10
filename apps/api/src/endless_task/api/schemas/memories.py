"""memories 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class UpdateMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = None
    importance: Optional[float] = None
    pinned: Optional[bool] = None
