"""reminders 域请求体（批次 D Task 1：一次性提醒的**显式创建**）。

在此之前创建提醒只有一条路：LLM 抽取提案 → 用户在
`POST /task-proposals/{id}/resolve` 接受。`/reminders` 只有读控路由
（列表 / 取消），没有 `POST /reminders`。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class CreateReminderBody(BaseModel):
    """`POST /reminders` 请求体。

    字段集合**就是**仓储唯一建提醒缝 `SqliteReminderRepository.create_reminder`
    要求的字段集合，一个不多一个不少：

    - `title` / `commitment`：提醒标题与到点执行的内容；
    - `dueAt`：**一次性**到期时刻，契约 `{"kind": "once", "at": "YYYY-MM-DDTHH:MM"}`
      （`at` 是**本地墙钟**；归一成 UTC 的活由路由交给 domain 的
      `parse_reminder_due` + `reminder_due_to_utc_iso`，见 `task_schedule.py`）；
    - `conversationId` / `turnId`：来源锚点。

    为什么来源锚点是必填：`reminders.source_conversation_id` / `source_turn_id`
    在迁移 023 里是 `NOT NULL`，而且 `tasks/worker.py:start_reminder` 以源会话为
    锚——手工提醒要和提案提醒走**同一条**执行路径，就必须提供同一个锚点。

    为什么 `dueAt` 声明为 `object` 而不是自建模型：`once` 词表与
    `YYYY-MM-DDTHH:MM` 格式只由 domain 的 `parse_reminder_due` **一处**裁决。
    本层不重写词表、不做静默回落——非法 kind / 缺 `at` / 格式错必须由那一处
    指名报错后原样冒到 4xx。

    字段**全 `Optional[...] = None`**：缺字段由信封层翻成**指名** 400
    （`missing_title` / `missing_commitment` / `missing_due_at` /
    `missing_source_conversation` / `missing_source_turn`），pydantic 的通用
    必填报错说不出是哪个字段缺。
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    commitment: Optional[str] = None
    dueAt: Optional[object] = None
    conversationId: Optional[str] = None
    turnId: Optional[str] = None
