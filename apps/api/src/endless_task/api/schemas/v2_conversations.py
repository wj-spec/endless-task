"""v2_conversations 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class RuntimeV2CreateLaneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["persistent_branch"] = "persistent_branch"
    sourceLaneId: Optional[str] = None
    baseEntryId: Optional[str] = None
    displayName: Optional[str] = None


class RuntimeV2CreateTemporaryConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Kept optional for request compatibility; the server always snapshots the
    # current main lane and its complete leaf path.
    sourceLaneId: Optional[str] = None
    sourceLeafEntryId: Optional[str] = None
    title: Optional[str] = None


class RuntimeV2MemoryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["preference", "fact"]
    content: str
    laneId: Optional[str] = None
    sourceEntryId: Optional[str] = None
    expiresAt: Optional[str] = None


class RuntimeV2MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    laneId: Optional[str] = None
