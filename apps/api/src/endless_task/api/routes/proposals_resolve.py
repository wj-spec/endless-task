"""提案解决与杂项路由（从 app.py 搬出；路径、状态码、响应体零改动）。

集合：`/memory-proposals|knowledge-proposals|task-proposals|artifact-proposals/{id}/resolve`、
`/undo-journal/{id}/undo`、`/reflections`、`/search`、`/turns/{id}/feedback`、
`/retrieval-stats`。
"""

from __future__ import annotations

import logging

from typing import Optional

from fastapi import FastAPI, Query, Response

from endless_task.domain.models import (
    FeedbackRating,
    KnowledgeProposalType,
    KnowledgeScope,
    RetrievalEventKind,
)
from endless_task.runtime_v2 import MemoryScope
from endless_task.storage.sqlite_knowledge_repository import scope_tier
from endless_task.domain.repositories import NotFoundError, ValidationError
from endless_task.domain.task_schedule import ReminderDue

from ..audit_support import audit_fact_from_event  # noqa: F401  (由部分 handler 使用)
from ..container import AppContainer
from ..errors import ApiRequestError
from ..hub_support import hub_append_memory_consolidated, hub_append_proposal_resolved
from ..knowledge_support import emit_knowledge_duplicates
from ..serialization import (
    artifact_json,
    artifact_proposal_json,
    knowledge_proposal_json,
    knowledge_source_json,
    memory_proposal_json,
    memory_record_json,
    memory_reflection_json,
    reminder_json,
    task_json,
    task_proposal_json,
    undo_entry_json,
)
from ..workspace_support import resolve_workspace_reference
from ..schemas.proposals_resolve import (
    ResolveArtifactProposalBody,
    ResolveKnowledgeProposalBody,
    ResolveMemoryProposalBody,
    ResolveTaskProposalBody,
    ResponseFeedbackBody,
    SearchBody,
)
from endless_task.workspace_runtime.undo_service import UndoUnavailableError


logger = logging.getLogger(__name__)


def register_proposals_resolve_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/retrieval-stats")
    async def get_retrieval_stats() -> dict[str, object]:
        return {"stats": container.retrieval_event_repository.summarize()}


    @app.post("/turns/{turn_id}/feedback")
    async def record_response_feedback(
        turn_id: str,
        body: ResponseFeedbackBody,
    ) -> dict[str, object]:
        try:
            rating = FeedbackRating(body.rating)
        except ValueError:
            raise ApiRequestError(
                "invalid_rating",
                "rating 只能是 up 或 down。",
                status_code=422,
            )
        record = container.response_feedback_repository.upsert(
            conversation_id=body.conversationId or "",
            turn_id=turn_id,
            variant_id=body.variantId,
            rating=rating,
            reason=body.reason,
            note=body.note,
        )
        return {
            "id": record.id,
            "conversationId": record.conversation_id,
            "turnId": record.turn_id,
            "variantId": record.variant_id,
            "rating": record.rating.value,
            "reason": record.reason,
            "note": record.note,
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
        }


    @app.post("/search")
    async def search_knowledge(body: SearchBody) -> dict[str, object]:
        scopes: list[KnowledgeScope] = []
        for value in body.scopes:
            try:
                scopes.append(KnowledgeScope(value))
            except ValueError as error:
                raise ValidationError(f"Unknown search scope: {value}") from error
        limit = max(1, min(body.limit, 20))
        workspace_filter: Optional[str] = None
        if body.workspaceId is not None:
            text = body.workspaceId.strip()
            if text == "general":
                workspace_filter = container.knowledge_repository.WORKSPACE_GENERAL
            elif text:
                workspace_filter = resolve_workspace_reference(container, text)
        grouped = container.knowledge_repository.search(
            body.query, scopes, limit, workspace_id=workspace_filter
        )
        # 分组顺序对齐注入优先级：curated（知识源/记忆）在前，层内按配置顺序。
        ordered_scopes = sorted(
            scopes,
            key=lambda item: (
                scope_tier(item),
                scopes.index(item),
            ),
        )
        groups: list[dict[str, object]] = []
        hit_counts: dict[str, int] = {}
        for scope in ordered_scopes:
            hits = grouped.get(scope, [])
            items: list[dict[str, object]] = []
            for hit in hits:
                entry: dict[str, object] = {
                    "refId": hit.ref_id,
                    "title": hit.title,
                    "snippet": hit.snippet,
                }
                if scope is KnowledgeScope.SOURCE:
                    source_id = hit.source_id or hit.ref_id
                    try:
                        source = container.knowledge_repository.get_source(source_id)
                    except NotFoundError:
                        continue
                    entry["origin"] = source.origin.value
                    entry["kind"] = source.kind.value
                    entry["updatedAt"] = source.updated_at
                    if hit.chunk_seq is not None:
                        entry["sourceId"] = source_id
                        entry["chunkSeq"] = hit.chunk_seq
                elif scope is KnowledgeScope.MEMORY:
                    try:
                        memory = container.memory_repository.get_memory(hit.ref_id)
                    except NotFoundError:
                        continue
                    entry["updatedAt"] = memory.updated_at
                elif scope is KnowledgeScope.ARTIFACT:
                    try:
                        artifact = container.artifact_repository.get_artifact(hit.ref_id)
                    except NotFoundError:
                        continue
                    entry["updatedAt"] = artifact.updated_at
                elif scope is KnowledgeScope.CONVERSATION:
                    with container.database.connect() as connection:
                        row = connection.execute(
                            "SELECT conversation_id FROM v2_transcript_entries WHERE id = ?",
                            (hit.ref_id,),
                        ).fetchone()
                        if row is None:
                            row = connection.execute(
                                "SELECT conversation_id FROM turns WHERE id = ?",
                                (hit.ref_id,),
                            ).fetchone()
                    if row is None:
                        continue
                    entry["conversationId"] = row["conversation_id"]
                items.append(entry)
            if items:
                groups.append({"scope": scope.value, "hits": items})
            hit_counts[scope.value] = len(items)
        try:
            container.retrieval_event_repository.record(
                RetrievalEventKind.SEARCH,
                body.query,
                hit_counts=hit_counts,
            )
        except Exception:  # noqa: BLE001 埋点失败不影响检索结果
            logger.debug("Failed to record search event", exc_info=True)
        return {"groups": groups}


    def _mirror_confirmed_memory_to_v2(container, memory) -> None:
        """v2 会话的确认记忆镜像到 v2 user_global。

        读取端（v2 链路）统一只读 v2_runtime_memories，因此 v2 会话确认的
        记忆必须同时进入 v2 表；v1 表保留（提案状态机完整），但不再被
        v2 链路读取，避免 v1/v2 记忆双重注入。
        """
        conversation_id = memory.source_conversation_id
        try:
            quality = container.runtime_v2_memory_quality_service
            repository = container.runtime_v2_memory_repository
            similar = quality.find_similar(
                conversation_id=conversation_id,
                content=memory.content,
            )
            if similar is None:
                repository.create_memory(
                    scope=MemoryScope.USER_GLOBAL,
                    kind=memory.kind.value,
                    content=memory.content,
                    conversation_id=conversation_id,
                )
                return
            if similar.similarity >= quality.similarity_threshold:
                return  # NOOP:重复内容不新增
            created = repository.create_memory(
                scope=MemoryScope.USER_GLOBAL,
                kind=memory.kind.value,
                content=memory.content,
                conversation_id=conversation_id,
            )
            repository.supersede_memory(
                similar.record.id,
                superseded_by=created.id,
            )  # UPDATE:新记忆取代旧记忆(旧保留可追溯)
        except Exception:  # noqa: BLE001 镜像失败不影响提案确认
            logger.debug(
                "Failed to mirror confirmed memory to v2",
                exc_info=True,
            )


    @app.post("/memory-proposals/{proposal_id}/resolve")
    async def resolve_memory_proposal(
        proposal_id: str, body: ResolveMemoryProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.proposal_repository.reject_proposal(proposal_id)
            if container.memory_consolidation_service is not None:
                container.memory_consolidation_service.reject(proposal_id)
            if container.memory_reflection_service is not None:
                container.memory_reflection_service.reject(proposal_id)
            hub_append_proposal_resolved(
                container, kind="memory", proposal=proposal, decision="reject"
            )
            return {"proposal": memory_proposal_json(proposal)}
        proposal, memory = container.proposal_repository.accept_proposal(
            proposal_id
        )
        consolidated_ids: list[str] = []
        if container.memory_consolidation_service is not None:
            merged = container.memory_consolidation_service.finalize(
                proposal_id, memory
            )
            if merged:
                consolidated_ids = list(merged)
                hub_append_memory_consolidated(
                    container,
                    proposal=proposal,
                    memory=memory,
                    source_memory_ids=merged,
                )
        if container.memory_reflection_service is not None:
            container.memory_reflection_service.finalize(proposal_id, memory)
        hub_append_proposal_resolved(
            container, kind="memory", proposal=proposal, decision="accept"
        )
        if container.memory_conflict_service is not None:
            await container.memory_conflict_service.resolve_conflicts_for(memory)
        _mirror_confirmed_memory_to_v2(container, memory)
        return {
            "proposal": memory_proposal_json(proposal),
            "memory": memory_record_json(memory),
            "consolidatedMemoryIds": consolidated_ids,
        }


    @app.get("/reflections")
    async def list_all_memory_reflections(
        include_resolved: bool = True,
        limit: int = 50,
    ) -> dict[str, object]:
        """B4：跨会话的反思记录（记忆面板用）。"""
        service = container.memory_reflection_service
        if service is None:
            return {"items": []}
        return {
            "items": [
                memory_reflection_json(record)
                for record in service.list_records(
                    include_resolved=include_resolved, limit=limit
                )
            ]
        }


    @app.post("/undo-journal/{entry_id}/undo")
    async def undo_journal_entry(entry_id: str) -> dict[str, object]:
        """A5：撤销一次可逆的文件操作（幂等）。"""
        service = container.undo_service
        if service is None:
            raise NotFoundError("Undo service is not available.")
        try:
            entry, performed = service.undo(entry_id)
        except UndoUnavailableError as error:
            raise ValidationError(error.message) from error
        if performed:
            container.hub_event_repository.append(
                "effect.undone",
                conversation_id=entry.conversation_id,
                data={
                    "entryId": entry.id,
                    "conversationId": entry.conversation_id,
                    "kind": entry.kind,
                    "target": entry.target,
                    "description": entry.description,
                },
            )
        return {
            "entry": undo_entry_json(entry),
            "performed": performed,
            "alreadyUndone": not performed,
        }


    @app.post("/knowledge-proposals/{proposal_id}/resolve")
    async def resolve_knowledge_proposal(
        proposal_id: str, body: ResolveKnowledgeProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.knowledge_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="knowledge", proposal=proposal, decision="reject"
            )
            return {"proposal": knowledge_proposal_json(proposal)}
        workspace_override = container.knowledge_proposal_repository._UNSET_WORKSPACE
        if body.workspaceId is not None:
            workspace_override = resolve_workspace_reference(container, body.workspaceId)
        proposal, source = container.knowledge_proposal_repository.accept_proposal(
            proposal_id, workspace_override=workspace_override
        )
        hub_append_proposal_resolved(
            container, kind="knowledge", proposal=proposal, decision="accept"
        )
        if proposal.proposal_type is KnowledgeProposalType.ADD_SOURCE:
            emit_knowledge_duplicates(container, source)
        return {
            "proposal": knowledge_proposal_json(proposal),
            "source": knowledge_source_json(source),
        }


    @app.post("/task-proposals/{proposal_id}/resolve")
    async def resolve_task_proposal(
        proposal_id: str, body: ResolveTaskProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.task_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="task", proposal=proposal, decision="reject"
            )
            return {"proposal": task_proposal_json(proposal)}
        peek = container.task_proposal_repository.get_proposal(proposal_id)
        if isinstance(peek.schedule, ReminderDue):
            proposal, reminder = (
                container.task_proposal_repository.accept_proposal_as_reminder(
                    proposal_id
                )
            )
            hub_append_proposal_resolved(
                container, kind="task", proposal=proposal, decision="accept"
            )
            return {
                "proposal": task_proposal_json(proposal),
                "reminder": reminder_json(reminder),
            }
        proposal, task = container.task_proposal_repository.accept_proposal(
            proposal_id
        )
        hub_append_proposal_resolved(
            container, kind="task", proposal=proposal, decision="accept"
        )
        return {
            "proposal": task_proposal_json(proposal),
            "task": task_json(task),
        }


    @app.post("/artifact-proposals/{proposal_id}/resolve")
    async def resolve_artifact_proposal(
        proposal_id: str, body: ResolveArtifactProposalBody
    ) -> dict[str, object]:
        if body.decision == "reject":
            proposal = container.artifact_proposal_repository.reject_proposal(
                proposal_id
            )
            hub_append_proposal_resolved(
                container, kind="artifact", proposal=proposal, decision="reject"
            )
            return {"proposal": artifact_proposal_json(proposal)}
        proposal, artifact = container.artifact_proposal_repository.accept_proposal(
            proposal_id
        )
        hub_append_proposal_resolved(
            container, kind="artifact", proposal=proposal, decision="accept"
        )
        return {
            "proposal": artifact_proposal_json(proposal),
            "artifact": artifact_json(artifact),
        }
