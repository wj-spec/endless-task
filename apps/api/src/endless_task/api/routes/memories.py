"""memories 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
"""

from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import Response

from endless_task.domain.repositories import NotFoundError, ValidationError

from ..container import AppContainer
from ..schemas.memories import UpdateMemoryBody
from ..serialization import memory_record_json


def register_memories_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/memories")
    async def list_memories(
        include_deleted: bool = False,
        write_origin: Optional[str] = Query(default=None),
    ) -> dict[str, object]:
        memories = container.memory_repository.list_memories(
            include_deleted=include_deleted
        )
        if write_origin is not None:
            memories = [
                item for item in memories if item.write_origin == write_origin
            ]
        titles: dict[str, str] = {}
        for item in memories:
            conversation_id = item.source_conversation_id
            if conversation_id and conversation_id not in titles:
                try:
                    titles[conversation_id] = container.chat_repository.get_conversation(
                        conversation_id
                    ).title
                except NotFoundError:
                    titles[conversation_id] = ""
        return {
            "items": [
                memory_record_json(
                    item,
                    source_title=titles.get(item.source_conversation_id) or None,
                )
                for item in memories
            ]
        }


    @app.patch("/memories/{memory_id}")
    async def update_memory(
        memory_id: str, body: UpdateMemoryBody
    ) -> dict[str, object]:
        if (
            body.content is None
            and body.importance is None
            and body.pinned is None
        ):
            raise ValidationError("Nothing to update.")
        record = container.memory_repository.get_memory(memory_id)
        if body.content is not None:
            record = container.memory_repository.update_memory_content(
                memory_id, body.content
            )
        if body.importance is not None:
            record = container.memory_repository.set_memory_importance(
                memory_id, body.importance
            )
        if body.pinned is not None:
            record = container.memory_repository.set_memory_pinned(
                memory_id, body.pinned
            )
        return {"memory": memory_record_json(record)}


    @app.get("/memories/forgetting-preview")
    async def preview_memory_forgetting() -> dict[str, object]:
        """B3：只读预览——哪些记忆会被忘、哪些重要记忆需要确认。"""
        service = container.memory_forgetting_service
        if service is None:
            return {"dryRun": True, "forgottenCount": 0, "forgotten": [], "needsReview": []}
        return service.preview().as_json()


    @app.post("/memories/forget")
    async def run_memory_forgetting(dry_run: bool = False) -> dict[str, object]:
        """B3：执行一次遗忘巡检（重要记忆只报告、不删除）。"""
        service = container.memory_forgetting_service
        if service is None:
            return {"dryRun": dry_run, "forgottenCount": 0, "forgotten": [], "needsReview": []}
        report = service.preview() if dry_run else service.run()
        return report.as_json()


    @app.post("/memories/consolidate")
    async def consolidate_memories() -> dict[str, object]:
        """B2：扫描同类冗余记忆并生成巩固提案（走确认流，不直接改写记忆）。"""
        service = container.memory_consolidation_service
        if service is None:
            return {"clusterCount": 0, "createdCount": 0, "created": [], "skipped": []}
        report = service.create_proposals()
        if report.created:
            container.hub_event_repository.append(
                "proposal.pending",
                conversation_id=report.created[0].proposal.conversation_id,
                data={
                    "kind": "memory",
                    "conversationId": report.created[0].proposal.conversation_id,
                    "count": len(report.created),
                },
            )
        return report.as_json()


    @app.get("/memories/consolidations")
    async def list_memory_consolidations(
        include_resolved: bool = True,
    ) -> dict[str, object]:
        """B2：巩固记录（含来源记忆内容），供面板展示溯源。"""
        service = container.memory_consolidation_service
        if service is None:
            return {"items": []}
        items: list[dict[str, object]] = []
        for record in service.list_records(include_resolved=include_resolved):
            sources: list[dict[str, object]] = []
            for memory_id in record.source_memory_ids:
                try:
                    memory = container.memory_repository.get_memory(memory_id)
                except NotFoundError:
                    continue
                sources.append(
                    {
                        "id": memory.id,
                        "content": memory.content,
                        "status": memory.status.value,
                    }
                )
            items.append(
                {
                    "id": record.id,
                    "proposalId": record.proposal_id,
                    "kind": record.kind,
                    "status": record.status,
                    "signature": record.signature,
                    "insightMemoryId": record.insight_memory_id,
                    "createdAt": record.created_at,
                    "resolvedAt": record.resolved_at,
                    "sources": sources,
                }
            )
        return {"items": items}


    @app.delete("/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: str) -> Response:
        container.memory_repository.delete_memory(memory_id)
        return Response(status_code=204)
