"""conversations 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
"""

from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import Response

from endless_task.domain.models import (
    ConversationKind,
    ConversationSnapshot,
    ConversationStatus,
)
from endless_task.domain.repositories import NotFoundError
from endless_task.files import FileError

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.conversations import (
    ConversationPatch,
    CreateBranchBody,
    CreateConversationBody,
)
from ..serialization import (
    artifact_json,
    artifact_proposal_json,
    conversation_json,
    conversation_snapshot_json,
    knowledge_proposal_json,
    memory_proposal_json,
    memory_reflection_json,
    task_proposal_json,
    undo_entry_json,
    uploaded_text_file_json,
)
from ..workspace_support import (
    require_bound_workspace,
    resolve_workspace_reference,
)


def register_conversations_routes(app: FastAPI, container: AppContainer) -> None:
    @app.post("/conversations", status_code=201)
    async def create_conversation(
        body: Optional[CreateConversationBody] = None,
    ) -> dict[str, object]:
        # 必选绑定设定：新建会话必须归属到一个已绑定目录的工作区。
        workspace_id = require_bound_workspace(
            container, body.workspaceId if body is not None else None
        )
        return conversation_json(
            container.chat_repository.create_or_reuse_empty_conversation(
                workspace_id
            )
        )


    @app.get("/conversations")
    async def list_conversations(
        status: ConversationStatus = Query(ConversationStatus.ACTIVE),
        query: Optional[str] = Query(None),
        workspace: Optional[str] = Query(None),
    ) -> dict[str, object]:
        workspace_id: Optional[str] = None
        general_only = False
        text = (workspace or "").strip()
        if text == "general":
            general_only = True
        elif text:
            workspace_id = resolve_workspace_reference(container, text)
            if workspace_id is None:
                general_only = True
        conversations = container.chat_repository.list_conversations(
            status=status,
            title_query=query,
            workspace_id=workspace_id,
            general_only=general_only,
        )
        return {"items": [conversation_json(item) for item in conversations]}


    @app.get("/conversations/{conversation_id}")
    async def get_conversation(conversation_id: str) -> dict[str, object]:
        snapshot = container.chat_repository.get_conversation_snapshot(conversation_id)
        if snapshot.conversation.parent_conversation_id is not None:
            lineage = container.chat_repository.list_lineage_turns(conversation_id)
            snapshot = ConversationSnapshot(
                conversation=snapshot.conversation,
                turns=tuple(lineage) + tuple(snapshot.turns),
            )
        payload = conversation_snapshot_json(
            snapshot,
        )
        payload["files"] = [
            uploaded_text_file_json(item)
            for item in container.file_repository.list_files(conversation_id)
        ]
        if snapshot.conversation.parent_conversation_id is not None:
            try:
                parent = container.chat_repository.get_conversation(
                    snapshot.conversation.parent_conversation_id
                )
                payload["parentTitle"] = parent.title
            except NotFoundError:
                payload["parentTitle"] = None
        return payload


    @app.post("/conversations/{conversation_id}/files", status_code=201)
    async def upload_conversation_file(
        conversation_id: str,
        request: Request,
        filename: str = Query(..., min_length=1, max_length=512),
    ) -> dict[str, object]:
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > container.settings.max_file_bytes:
                raise FileError(
                    "file_too_large",
                    f"文件超过 {container.settings.max_file_bytes} 字节的本地限制。",
                    status_code=413,
                )
            content.extend(chunk)
        uploaded = container.file_repository.create_file(
            conversation_id=conversation_id,
            original_name=filename,
            media_type=request.headers.get("content-type", "application/octet-stream"),
            content=bytes(content),
        )
        return uploaded_text_file_json(uploaded)


    @app.delete("/conversations/{conversation_id}/files/{file_id}", status_code=204)
    async def delete_conversation_file(
        conversation_id: str,
        file_id: str,
    ) -> Response:
        container.file_repository.delete_file(
            conversation_id=conversation_id,
            file_id=file_id,
        )
        return Response(status_code=204)


    @app.patch("/conversations/{conversation_id}")
    async def patch_conversation(
        conversation_id: str,
        body: ConversationPatch,
    ) -> dict[str, object]:
        supplied_fields = body.model_fields_set
        if not supplied_fields:
            raise ApiRequestError(
                "invalid_request",
                "至少需要提供 title、status、providerProfileId、modelOverride 或 workspaceId。",
            )
        conversation = container.chat_repository.get_conversation(conversation_id)
        if body.title is not None:
            conversation = container.chat_repository.rename_conversation(
                conversation_id,
                body.title,
            )
        if body.status is not None:
            conversation = container.chat_repository.set_conversation_status(
                conversation_id,
                body.status,
            )
        if "providerProfileId" in supplied_fields or "modelOverride" in supplied_fields:
            provider_profile_id = (
                body.providerProfileId
                if "providerProfileId" in supplied_fields
                else conversation.provider_profile_id
            )
            model_override = (
                body.modelOverride
                if "modelOverride" in supplied_fields
                else conversation.model_override
            )
            if provider_profile_id is not None or model_override is not None:
                profile_id = provider_profile_id
                if profile_id is None:
                    profile_id = (
                        container.provider_profile_repository.get_default_profile_id()
                    )
                if profile_id is None:
                    raise ApiRequestError(
                        "provider_not_configured",
                        "请先配置可用的模型服务。",
                        status_code=409,
                    )
                try:
                    profile = container.provider_profile_repository.get_profile(
                        profile_id
                    )
                except NotFoundError as error:
                    raise ApiRequestError(
                        "provider_not_available",
                        "所选模型服务已不存在，请重新选择。",
                        status_code=409,
                    ) from error
                if not profile.enabled:
                    raise ApiRequestError(
                        "provider_disabled",
                        "该模型服务已停用，请重新选择。",
                        status_code=409,
                    )
                selected_model_id = model_override or profile.default_model
                if not selected_model_id:
                    raise ApiRequestError(
                        "model_not_available",
                        "该模型服务没有可用的默认模型，请先完成模型配置。",
                        status_code=409,
                    )
                try:
                    model = container.provider_profile_repository.get_model(
                        profile_id,
                        selected_model_id,
                    )
                except NotFoundError as error:
                    raise ApiRequestError(
                        "model_not_available",
                        "所选模型不在该服务的可用模型中，请重新选择。",
                        status_code=409,
                    ) from error
                if not model.enabled:
                    raise ApiRequestError(
                        "model_disabled",
                        "该模型已停用，请重新选择。",
                        status_code=409,
                    )
            conversation = container.chat_repository.set_conversation_model(
                conversation_id,
                provider_profile_id=provider_profile_id,
                model_override=model_override,
            )
        if "workspaceId" in supplied_fields:
            if conversation.kind != ConversationKind.NORMAL or (
                conversation.parent_conversation_id is not None
            ):
                raise ApiRequestError(
                    "workspace_conversation_mismatch",
                    "临时/分支会话不允许跨工作区迁移。",
                    status_code=409,
                )
            workspace_id = require_bound_workspace(container, body.workspaceId)
            conversation = container.chat_repository.set_conversation_workspace(
                conversation_id,
                workspace_id,
            )
        return conversation_json(conversation)


    @app.delete("/conversations/{conversation_id}", status_code=204)
    async def delete_conversation(conversation_id: str) -> Response:
        descendant_ids = container.chat_repository.list_descendant_ids(
            conversation_id
        )
        for target_id in (conversation_id, *descendant_ids):
            container.reminder_repository.cancel_reminders_for_conversation(
                target_id
            )
            container.task_repository.cancel_tasks_for_conversation(target_id)
            container.task_proposal_repository.cancel_proposals_for_conversation(
                target_id
            )
            container.proposal_repository.cancel_proposals_for_conversation(
                target_id
            )
        container.chat_repository.delete_conversation(conversation_id)
        return Response(status_code=204)


    @app.post("/conversations/{conversation_id}/branches", status_code=201)
    async def create_branch(
        conversation_id: str,
        body: CreateBranchBody,
    ) -> dict[str, object]:
        branch = container.chat_repository.create_branch(
            parent_conversation_id=conversation_id,
            fork_turn_id=body.forkTurnId,
            kind=ConversationKind.EPHEMERAL,
        )
        return {"conversation": conversation_json(branch)}


    @app.get("/conversations/{conversation_id}/branches")
    async def list_branches(conversation_id: str) -> dict[str, object]:
        branches = container.chat_repository.list_branches(conversation_id)
        return {"items": [conversation_json(item) for item in branches]}


    @app.post("/conversations/{conversation_id}/promote")
    async def promote_conversation(conversation_id: str) -> dict[str, object]:
        conversation = container.chat_repository.promote_conversation(
            conversation_id
        )
        return {"conversation": conversation_json(conversation)}


    @app.get("/conversations/{conversation_id}/citations")
    async def get_conversation_citations(
        conversation_id: str,
    ) -> dict[str, object]:
        turn_citations = container.retrieval_event_repository.list_citations_by_turn(
            conversation_id,
        )
        return {"turnCitations": turn_citations}


    @app.get("/conversations/{conversation_id}/reflections")
    async def list_memory_reflections(
        conversation_id: str,
        include_resolved: bool = True,
        limit: int = 50,
    ) -> dict[str, object]:
        """B4：反思记录（洞见 + 来源 Episode），供面板溯源。"""
        service = container.memory_reflection_service
        if service is None:
            return {"items": []}
        return {
            "items": [
                memory_reflection_json(record)
                for record in service.list_records(
                    conversation_id=conversation_id,
                    include_resolved=include_resolved,
                    limit=limit,
                )
            ]
        }


    @app.get("/conversations/{conversation_id}/undo-journal")
    async def list_undo_journal(
        conversation_id: str, limit: int = 10
    ) -> dict[str, object]:
        """A5：最近可撤销的工具副作用（文件写/删）。"""
        entries = container.undo_journal_repository.list_for_conversation(
            conversation_id, limit=limit
        )
        return {
            "items": [undo_entry_json(entry) for entry in entries],
            "latestAvailableId": (
                entries[0].id
                if entries and entries[0].undoable
                else None
            ),
        }


    @app.get("/conversations/{conversation_id}/memory-proposals")
    async def list_memory_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [memory_proposal_json(item) for item in proposals]}


    @app.get("/conversations/{conversation_id}/knowledge-proposals")
    async def list_knowledge_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.knowledge_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [knowledge_proposal_json(item) for item in proposals]}


    @app.get("/conversations/{conversation_id}/task-proposals")
    async def list_task_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.task_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [task_proposal_json(item) for item in proposals]}


    @app.get("/conversations/{conversation_id}/artifact-proposals")
    async def list_artifact_proposals(
        conversation_id: str, include_resolved: bool = False
    ) -> dict[str, object]:
        proposals = container.artifact_proposal_repository.list_proposals(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
        )
        return {"items": [artifact_proposal_json(item) for item in proposals]}


    @app.get("/conversations/{conversation_id}/workspace")
    async def get_conversation_workspace(conversation_id: str) -> dict[str, object]:
        container.chat_repository.get_conversation(conversation_id)
        artifacts = container.artifact_repository.list_artifacts_for_conversation(
            conversation_id
        )
        proposals = container.artifact_proposal_repository.list_proposals(
            conversation_id=conversation_id
        )
        artifact_items = [artifact_json(item) for item in artifacts]
        proposal_items = [artifact_proposal_json(item) for item in proposals]
        return {
            "conversationId": conversation_id,
            "visible": bool(artifact_items or proposal_items),
            "artifacts": artifact_items,
            "pendingProposals": proposal_items,
        }
