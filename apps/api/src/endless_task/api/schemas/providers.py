"""providers 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, ConfigDict


class ProviderDefaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profileId: str


class ProviderDefaultModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modelId: str


class ProviderModelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modelId: str
    displayName: str = ""


class ProviderModelPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ProviderProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    defaultModel: str = ""
    baseUrl: str = ""
    apiKey: Optional[str] = None
    apiKeyRef: str = ""
    timeoutSeconds: float = 60.0
    enabled: bool = True


class ProviderProfilePatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    defaultModel: Optional[str] = None
    baseUrl: Optional[str] = None
    apiKey: Optional[str] = None
    apiKeyRef: Optional[str] = None
    timeoutSeconds: Optional[float] = None
    enabled: Optional[bool] = None
