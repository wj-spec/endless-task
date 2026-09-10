"""系统域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SetPermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    acknowledge: bool = False
