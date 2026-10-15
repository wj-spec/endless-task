"""v2_runs 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class ResendRuntimeV2RunBody(BaseModel):
    content: str


class RuntimeV2RecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: E3/B：`resume` = "在这条 run 上接着跑"（硬停后的终态 run 原地复活）。
    #: 拒绝时后端回 409 + `code=run_not_revivable`（message 带原因码），
    #: **不是 500**。
    action: Literal["mark_failed", "retry", "resume"]


class RuntimeV2RunMemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    sourceEntryId: Optional[str] = None
    expiresAt: Optional[str] = None


class RuntimeV2SteerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
