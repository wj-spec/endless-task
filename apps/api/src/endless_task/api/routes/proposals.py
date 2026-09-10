"""proposals 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI

from endless_task.domain.models import KnowledgeProposalType

from ..container import AppContainer
from ..errors import ApiRequestError


def register_proposals_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/proposals/pending")
    async def list_pending_proposals() -> dict[str, object]:
        items: list[dict[str, object]] = []
        for proposal in container.artifact_proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "artifact",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.title,
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.task_proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "task",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.title,
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.proposal_repository.list_pending(limit=50):
            items.append(
                {
                    "id": proposal.id,
                    "kind": "memory",
                    "conversationId": proposal.conversation_id,
                    "title": proposal.content[:60],
                    "createdAt": proposal.created_at,
                }
            )
        for proposal in container.knowledge_proposal_repository.list_pending(
            limit=50
        ):
            if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
                title = f"知识：{str(proposal.payload.get('title', ''))[:50]}"
            else:
                title = f"知识过期：{str(proposal.payload.get('title', ''))[:50]}"
            items.append(
                {
                    "id": proposal.id,
                    "kind": "knowledge",
                    "conversationId": proposal.conversation_id,
                    "title": title,
                    "createdAt": proposal.created_at,
                }
            )
        items.sort(key=lambda item: str(item["createdAt"]))
        return {"items": items}
