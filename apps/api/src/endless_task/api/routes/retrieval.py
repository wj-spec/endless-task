"""retrieval 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI

from endless_task.domain.models import RetrievalEventKind

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.retrieval import RetrievalEventBody
from ..serialization import knowledge_proposal_json


def register_retrieval_routes(app: FastAPI, container: AppContainer) -> None:
    @app.post("/retrieval-events", status_code=201)
    async def create_retrieval_event(
        body: RetrievalEventBody,
    ) -> dict[str, object]:
        if body.kind != "citation_click":
            raise ApiRequestError(
                "invalid_request", "仅支持记录角标点击事件。", status_code=400
            )
        event = container.retrieval_event_repository.record(
            RetrievalEventKind.CITATION_CLICK,
            body.query or "",
            conversation_id=body.conversationId,
            turn_id=body.turnId,
            detail={
                "label": body.label,
                "scope": body.scope,
                "refId": body.refId,
            },
        )
        return {"id": event.id}


    @app.post("/knowledge-lifecycle/decay-check")
    async def run_knowledge_decay_check() -> dict[str, object]:
        service = container.knowledge_lifecycle_service
        if service is None:
            raise ApiRequestError(
                "not_available", "知识生命周期服务未启用。", status_code=400
            )
        proposals = service.check_decay()
        return {
            "created": len(proposals),
            "items": [knowledge_proposal_json(item) for item in proposals],
        }
