"""notifications 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI

from ..container import AppContainer
from ..errors import ApiRequestError
from ..serialization import notification_json


def register_notifications_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/notifications")
    async def list_notifications(
        unread_only: bool = False,
    ) -> dict[str, object]:
        items = container.notification_repository.list_notifications(
            unread_only=unread_only
        )
        return {"items": [notification_json(item) for item in items]}


    @app.post("/notifications/read-all")
    async def read_all_notifications() -> dict[str, object]:
        count = container.notification_repository.mark_all_read()
        if count > 0:
            container.hub_event_repository.append(
                "notification.read_all",
                data={"count": count},
            )
        return {"count": count}


    @app.post("/notifications/{notification_id}/read")
    async def read_notification(notification_id: str) -> dict[str, object]:
        notification = container.notification_repository.mark_read(
            notification_id
        )
        container.hub_event_repository.append(
            "notification.read",
            conversation_id=notification.conversation_id,
            data={
                "id": notification.id,
                "conversationId": notification.conversation_id,
            },
        )
        return {"notification": notification_json(notification)}
