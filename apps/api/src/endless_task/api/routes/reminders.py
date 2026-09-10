"""reminders 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI

from ..container import AppContainer
from ..errors import ApiRequestError
from ..serialization import reminder_json


def register_reminders_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/reminders")
    async def list_reminders(include_cancelled: bool = False) -> dict[str, object]:
        items = container.reminder_repository.list_reminders(
            include_cancelled=include_cancelled
        )
        return {"items": [reminder_json(item) for item in items]}


    @app.post("/reminders/{reminder_id}/cancel")
    async def cancel_reminder(reminder_id: str) -> dict[str, object]:
        reminder = container.reminder_repository.cancel_reminder(reminder_id)
        return {"reminder": reminder_json(reminder)}
