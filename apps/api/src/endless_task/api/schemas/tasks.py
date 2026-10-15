"""tasks 域请求体（切片 10 T5：定时任务的**显式创建**）。

在此之前创建定时任务只有一条路：LLM 抽取提案 → 用户在
`POST /task-proposals/{id}/resolve` 接受。`/tasks` 只有读控路由
（列表/详情/暂停/恢复/取消/立即执行/运行历史），没有 `POST /tasks`。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class CreateTaskBody(BaseModel):
    """`POST /tasks` 请求体。

    字段集合**就是**仓储唯一建任务缝 `SqliteTaskRepository.create_task`
    要求的字段集合，一个不多一个不少：

    - `title` / `commitment`：任务标题与执行内容（`commitment` 就是到点后作为
      用户消息提交给 agent 的**执行内容/提示**）；
    - `schedule`：周期描述（`daily|weekly|monthly` + `HH:MM`，周几/几号）；
    - `conversationId` / `turnId`：来源锚点。

    为什么来源锚点是必填：`tasks.source_conversation_id` / `source_turn_id` 在
    迁移 016 里是 `NOT NULL`，而且 `tasks/worker.py:_ensure_branch` 用
    `source_conversation_id` 作为 EPHEMERAL 执行子会话的父会话——手工任务要和
    提案任务走**同一条**执行路径，就必须提供同一个锚点。放开为空需要迁移 +
    读路径改造（与 `POST /reminders` 同一类裁定），不在本任务范围内。

    为什么 `schedule` 声明为 `object` 而不是自建模型：周期词表（`daily` /
    `weekly` / `monthly`，以及"`once` 不是任务"）只由 domain 的
    `parse_task_schedule` **一处**裁决。本层不重写词表、不做静默回落——
    非法频度/时间/越界字段必须由那一处指名报错后原样冒到 4xx。
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    commitment: Optional[str] = None
    schedule: Optional[object] = None
    conversationId: Optional[str] = None
    turnId: Optional[str] = None


class UpdateTaskBody(BaseModel):
    """`PATCH /tasks/{task_id}` 请求体（批次 D Task 1，契约照 `PATCH /memories/{id}`）。

    只接受**可编辑的语义字段**，一个不多一个不少：

    - `title` / `commitment`：复用 `SqliteTaskRepository._validate_text`
      （title ≤200 / commitment ≤2000），词表不在这里重写；
    - `schedule`：周期描述，必须经 domain 的 `parse_task_schedule` 再
      `serialize_task_schedule`（**不新增词表**：`once` 仍然不是任务 → 400）。

    **刻意不接受 `status`**：那会与 `POST /tasks/{id}/pause|resume|cancel` 的
    `_transition` 形成两个状态入口、两套允许集。也**不动** `sourceConversationId`
    / `sourceTurnId` / `sourceProposalId` / `createdAt`（前两者是执行环境来源，
    `createdAt` 是触发 anchor 的一部分）。

    字段全 `Optional[...] = None`，全 None 由路由翻成
    `ValidationError("Nothing to update.")`（与 `PATCH /memories/{id}` 同一口径）。
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    commitment: Optional[str] = None
    schedule: Optional[object] = None
