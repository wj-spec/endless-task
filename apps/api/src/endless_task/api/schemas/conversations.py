"""conversations 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from endless_task.domain.models import ConversationStatus


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    status: Optional[ConversationStatus] = None
    providerProfileId: Optional[str] = None
    modelOverride: Optional[str] = None
    workspaceId: Optional[str] = None


class CreateBranchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    forkTurnId: Optional[str] = None


class CreateConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspaceId: Optional[str] = None
