"""v2 零散路由请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class ResolveApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny", "modify"]
    arguments: Optional[dict] = None
