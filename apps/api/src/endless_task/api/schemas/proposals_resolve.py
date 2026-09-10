"""提案解决/反馈/检索请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class ResponseFeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: str
    reason: Optional[str] = None
    note: Optional[str] = None
    variantId: Optional[str] = None
    conversationId: Optional[str] = None


class SearchBody(BaseModel):
    query: str
    scopes: list[str] = [
        "source",
        "memory",
        "artifact",
        "conversation",
    ]
    limit: int = 8
    workspaceId: Optional[str] = None


class ResolveMemoryProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveKnowledgeProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
    workspaceId: Optional[str] = None


class ResolveTaskProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]


class ResolveArtifactProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
