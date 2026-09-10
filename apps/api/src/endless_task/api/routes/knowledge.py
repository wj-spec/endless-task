"""knowledge-sources 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import Response

from endless_task.domain.models import (
    KnowledgeSourceKind,
    KnowledgeSourceOrigin,
    KnowledgeSourceStatus,
)
from endless_task.domain.repositories import ValidationError
from endless_task.knowledge.ingestion import IngestionError, ingest_file_bytes

from ..container import AppContainer
from ..knowledge_support import emit_knowledge_duplicates
from ..errors import ApiRequestError
from ..schemas.knowledge import KnowledgeSourceBody, KnowledgeSourcePatch
from ..serialization import knowledge_source_json
from ..workspace_support import resolve_workspace_reference

logger = logging.getLogger(__name__)


def register_knowledge_routes(app: FastAPI, container: AppContainer) -> None:


    @app.get("/knowledge-sources")
    async def list_knowledge_sources(
        status: str = "active", workspace: Optional[str] = Query(None)
    ) -> dict[str, object]:
        try:
            status_enum = KnowledgeSourceStatus(status)
        except ValueError as error:
            raise ValidationError(f"Unknown knowledge source status: {status}") from error
        workspace_value = (workspace or "").strip() or None
        workspace_filter: Optional[str] = None
        if workspace_value is not None:
            if workspace_value == "general":
                workspace_filter = container.knowledge_repository.WORKSPACE_GENERAL
            else:
                workspace_filter = resolve_workspace_reference(container, workspace_value)
        sources = container.knowledge_repository.list_sources(
            status=status_enum, workspace_id=workspace_filter
        )
        return {"items": [knowledge_source_json(item) for item in sources]}


    @app.post("/knowledge-sources", status_code=201)
    async def create_knowledge_source(body: KnowledgeSourceBody) -> dict[str, object]:
        try:
            kind = KnowledgeSourceKind(body.kind)
        except ValueError as error:
            raise ValidationError(f"Unknown knowledge source kind: {body.kind}") from error
        source = container.knowledge_repository.create_source(
            kind=kind,
            origin=KnowledgeSourceOrigin.USER,
            title=body.title,
            content=body.content,
            file_name=body.fileName,
            expires_at=body.expiresAt,
            workspace_id=resolve_workspace_reference(container, body.workspaceId),
        )
        emit_knowledge_duplicates(container, source)
        return {"source": knowledge_source_json(source)}


    @app.patch("/knowledge-sources/{source_id}")
    async def update_knowledge_source(
        source_id: str, body: KnowledgeSourcePatch
    ) -> dict[str, object]:
        source = container.knowledge_repository.update_source(
            source_id,
            title=body.title,
            content=body.content,
            file_name=body.fileName,
            expires_at=body.expiresAt,
        )
        return {"source": knowledge_source_json(source)}


    @app.post("/knowledge-sources/{source_id}/expire")
    async def expire_knowledge_source(source_id: str) -> dict[str, object]:
        source = container.knowledge_repository.expire_source(source_id)
        return {"source": knowledge_source_json(source)}


    @app.post("/knowledge-sources/{source_id}/restore")
    async def restore_knowledge_source(source_id: str) -> dict[str, object]:
        source = container.knowledge_repository.restore_source(source_id)
        return {"source": knowledge_source_json(source)}


    @app.delete("/knowledge-sources/{source_id}", status_code=204)
    async def delete_knowledge_source(source_id: str) -> Response:
        container.knowledge_repository.delete_source(source_id)
        return Response(status_code=204)


    @app.post("/knowledge-sources/import", status_code=201)
    async def import_knowledge_source(
        file: UploadFile = File(...),
        title: Optional[str] = Form(default=None),
        workspaceId: Optional[str] = Form(default=None),
    ) -> dict[str, object]:
        raw = await file.read()
        file_name = (file.filename or "").strip() or "未命名文件"
        try:
            ingested = ingest_file_bytes(raw, file_name=file_name)
        except IngestionError as error:
            raise ApiRequestError(
                error.code, error.safe_message, status_code=400
            ) from error
        source = container.knowledge_repository.create_source(
            kind=KnowledgeSourceKind.FILE,
            origin=KnowledgeSourceOrigin.USER,
            title=(title or "").strip() or file_name,
            content=ingested.text,
            file_name=file_name,
            file_size=ingested.size,
            file_sha256=ingested.sha256,
            workspace_id=resolve_workspace_reference(container, workspaceId),
        )
        emit_knowledge_duplicates(container, source)
        return {
            "source": knowledge_source_json(source),
            "truncated": ingested.truncated,
            "encoding": ingested.encoding,
        }
