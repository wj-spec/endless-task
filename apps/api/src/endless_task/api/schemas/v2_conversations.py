"""v2_conversations 域请求体（从 app.py 搬出，字段与校验规则零改动）。

C3a 新增：发言体的可选 ``references``（@ 引用会话上下文）。**没有客户端发它时
行为逐字不变**（惰性）——这是一期就能单独合入后端半场的前提。
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


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


class RuntimeV2ConversationReferenceBody(BaseModel):
    """@ 引用的一条 = **判别联合**的一个成员（``kind`` 为判别键）。

    一期只有 ``kind="conversation"``；后续加"产物 / 文件 / 知识条目 / 记忆"引用
    不用改协议（``01`` §5.3）。``entryIds`` 是**一期预留**字段（``02`` §3.2：
    一期不做选中轮次），必须接受空数组；非空由路由层指名 400。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["conversation"]
    conversationId: str
    laneId: Optional[str] = None
    mode: Literal["summary", "recent", "full"] = "summary"
    recentTurns: Optional[int] = None
    entryIds: list[str] = Field(default_factory=list)


#: 判别联合（今天只有一个成员；判别键 ``kind`` 已生效，未知 kind → 422 指名
#: ``references.<i>``）。
RuntimeV2ConversationReference = Annotated[
    Union[RuntimeV2ConversationReferenceBody],
    Field(discriminator="kind"),
]


class RuntimeV2MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    laneId: Optional[str] = None
    #: 缺省空 —— 不发 ``references`` 的老客户端行为逐字不变。
    references: tuple[RuntimeV2ConversationReference, ...] = ()


class RuntimeV2ReferencePreviewBody(BaseModel):
    """引用预览（picker 的 token 预估必须来自后端：progress.md R-C4 修订）。"""

    model_config = ConfigDict(extra="forbid")

    references: tuple[RuntimeV2ConversationReference, ...] = ()
