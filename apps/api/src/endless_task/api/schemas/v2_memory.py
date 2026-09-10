"""v2_memory 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class RuntimeV2MemoryPromotionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targetScope: Literal[
        "user_global", "workspace", "conversation_tree", "branch"
    ]
    targetLaneId: Optional[str] = None


class RuntimeV2MemoryPromotionResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
