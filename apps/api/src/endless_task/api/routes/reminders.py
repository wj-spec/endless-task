"""reminders 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from endless_task.domain.repositories import NotFoundError
from endless_task.domain.task_schedule import (
    parse_reminder_due,
    reminder_due_to_utc_iso,
)

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.reminders import CreateReminderBody
from ..serialization import reminder_json


def reminder_due_has_passed(due_at_utc: str, *, now: datetime) -> bool:
    """归一后的 UTC 到期时刻是否**早于** `now` 所在的**分钟**（批次 D R-D3）。

    `parse_reminder_due` 只校验 `kind == 'once'`、拒绝额外键、校验 `at` 形如
    `YYYY-MM-DDTHH:MM` 且日期真实存在——它**不判"是否已过去"**（见
    `domain/task_schedule.py`）。而调度是**字符串比较**（`due_at <= now_iso`，
    `storage/sqlite_reminder_repository.py:list_due_pending`），过去时刻会被
    `TaskScheduler.scan_once` **立刻**选中。所以"已过去"必须在这里单独挡下。

    边界如实定义：**当前分钟不算过去**（`at` 精确到分钟，用户填"现在这一分钟"
    是合法的最紧输入），严格早于当前分钟才是过去。比较的两个时刻都截到分钟。
    """
    moment = datetime.fromisoformat(due_at_utc.replace("Z", "+00:00"))
    current_minute = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return moment < current_minute


def register_reminders_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/reminders")
    async def list_reminders(include_cancelled: bool = False) -> dict[str, object]:
        items = container.reminder_repository.list_reminders(
            include_cancelled=include_cancelled
        )
        return {"items": [reminder_json(item) for item in items]}


    @app.post("/reminders", status_code=201)
    async def create_reminder(body: CreateReminderBody) -> dict[str, object]:
        """显式创建一条**一次性提醒**（批次 D Task 1）。

        **这不是第二份建提醒实现**：本 handler 只做三件事——① 把缺字段翻成
        指名的 4xx；② 把 `dueAt` 的**本地墙钟**归一成 **UTC ISO(ms)+Z** 再交给
        仓储；③ 复用 `POST /tasks` 同一个 `source_turn_mismatch` 口径校验来源
        两列相容。title/commitment 的非空与长度由仓储
        （`SqliteReminderRepository._validate_text`）裁决。

        为什么归一必须在**路由**做：`create_reminder` 今天**零格式/时区校验**
        （只 `strip()` 非空），而调度是**字符串比较**——把本地墙钟直接落库会被
        当成 UTC，**恒早一个时区差**触发。这里是本地墙钟 → UTC 唯一的转换点。

        校验一律发生在写库之前、且落库是单事务单行，所以非法输入**不留半行**。

        写出来的行与"提案接受"写出来的行同形（两边共用同一个
        `storage/sqlite_reminder_repository.py:insert_reminder_row`）。
        """
        for value, code, field in (
            (body.title, "missing_title", "title"),
            (body.commitment, "missing_commitment", "commitment"),
            (body.dueAt, "missing_due_at", "dueAt"),
            (
                body.conversationId,
                "missing_source_conversation",
                "conversationId",
            ),
            (body.turnId, "missing_source_turn", "turnId"),
        ):
            if value is None:
                raise ApiRequestError(
                    code,
                    f"{field} 是必填项。",
                    status_code=400,
                )
        # `parse_reminder_due` 把 kind/额外键/`at` 格式与日期真实性裁决完
        # （非法 → `ValidationError` → 400 `validation_error` + 指名 message），
        # `reminder_due_to_utc_iso` 把本地墙钟按进程本地时区转成与调度器
        # `now_iso` 同格式的 UTC 绝对值。请求字符串**绝不**直接交给仓储。
        parsed_due = parse_reminder_due(body.dueAt)
        due_at_utc = reminder_due_to_utc_iso(parsed_due)
        if reminder_due_has_passed(due_at_utc, now=datetime.now(timezone.utc)):
            raise ApiRequestError(
                "reminder_due_in_past",
                "提醒时间已过去（dueAt 必须不早于当前分钟）；请改成一个还没到的时刻。",
                status_code=400,
            )
        # 与 `POST /tasks` 同一套来源相容校验：`turnId` 必须**确实指向该会话的
        # 一轮**。两列在迁移 023 里是 NOT NULL，但此前没人校验二者**相容**——
        # 直接调 API 能建出"来源轮次属于另一个会话"的提醒。
        #
        # 两种失败（该 run 不存在 / 该 run 属于别的会话）共用**同一个** code：
        # 既指名到输入字段，又不泄露"这个 run id 在别的会话里存在"——否则就成了
        # 一个跨会话的存在性探针。校验发生在 `create_reminder` **之前**、写库是
        # 单事务单行，所以不匹配的请求**不留任何库行**。
        try:
            # `or ""`：上面的必填校验已把 None 翻成 `missing_source_turn` 400，
            # 这里只是为了不把 `Optional[str]` 直接喂给仓储（空串绝不可能命中行）。
            source_run = container.runtime_v2_repository.get_run(body.turnId or "")
        except NotFoundError:
            source_run = None
        if source_run is None or source_run.conversation_id != body.conversationId:
            raise ApiRequestError(
                "source_turn_mismatch",
                "turnId 指向的对话轮次不存在，或不属于该 conversationId。",
                status_code=400,
            )
        reminder = container.reminder_repository.create_reminder(
            title=body.title,
            commitment=body.commitment,
            due_at=due_at_utc,
            source_conversation_id=body.conversationId,
            source_turn_id=body.turnId,
        )
        return {"reminder": reminder_json(reminder)}


    @app.post("/reminders/{reminder_id}/cancel")
    async def cancel_reminder(reminder_id: str) -> dict[str, object]:
        reminder = container.reminder_repository.cancel_reminder(reminder_id)
        return {"reminder": reminder_json(reminder)}
