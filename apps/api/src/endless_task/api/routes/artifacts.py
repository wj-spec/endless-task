"""artifacts 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response

from endless_task.artifacts.export_service import (
    ExportError,
    build_export,
    content_disposition_header,
    export_filename,
)
from endless_task.domain.models import ArtifactVersionOperation

from ..container import AppContainer
from ..errors import ApiRequestError, error_response
from ..schemas.artifacts import CreateArtifactVersionBody, RollbackArtifactBody
from ..serialization import artifact_json, artifact_version_json


def register_artifacts_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/artifacts")
    async def list_artifacts(include_deleted: bool = False) -> dict[str, object]:
        records = container.artifact_repository.list_artifacts(
            include_deleted=include_deleted
        )
        return {"items": [artifact_json(item) for item in records]}


    @app.get("/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> dict[str, object]:
        artifact = container.artifact_repository.get_artifact(artifact_id)
        version = container.artifact_repository.get_current_version(artifact_id)
        references = container.reference_resolver.resolve(
            version.source_labels,
            conversation_id=version.source_conversation_id,
        )
        return {
            "artifact": artifact_json(artifact),
            "currentVersion": artifact_version_json(
                version, source_references=references
            ),
        }


    @app.post("/artifacts/{artifact_id}/versions", status_code=201)
    async def create_artifact_version(
        artifact_id: str, body: CreateArtifactVersionBody
    ) -> dict[str, object]:
        """A7：在聊天/面板里原位编辑产物 → 保存为新版本（可回溯、可回滚）。"""
        snapshot = container.artifact_repository.append_version(
            artifact_id=artifact_id,
            content=body.content,
            operation=ArtifactVersionOperation.UPDATE,
            source_conversation_id=body.sourceConversationId,
            source_turn_id=body.sourceTurnId,
            note=body.note or "用户在界面编辑",
        )
        artifact = snapshot.artifact
        if artifact.storage_path and body.sourceConversationId:
            binding = container.artifact_file_store.binding_for(
                body.sourceConversationId
            )
            if binding is not None:
                try:
                    container.artifact_file_store.restore(
                        binding,
                        artifact.storage_path,
                        snapshot.current_version.content,
                    )
                except Exception:  # noqa: BLE001 文件同步失败不阻断 DB
                    pass
        return {
            "artifact": artifact_json(snapshot.artifact),
            "currentVersion": artifact_version_json(snapshot.current_version),
        }


    @app.get("/artifacts/{artifact_id}/versions")
    async def list_artifact_versions(artifact_id: str) -> dict[str, object]:
        versions = container.artifact_repository.list_versions(artifact_id)
        items = []
        for version in versions:
            references = container.reference_resolver.resolve(
                version.source_labels,
                conversation_id=version.source_conversation_id,
            )
            items.append(
                artifact_version_json(version, source_references=references)
            )
        return {"items": items}


    @app.get("/artifacts/{artifact_id}/export")
    async def export_artifact(
        artifact_id: str, format: str = "markdown"
    ) -> Response:
        artifact = container.artifact_repository.get_artifact(artifact_id)
        version = container.artifact_repository.get_current_version(artifact_id)
        try:
            data, media_type = build_export(artifact, version, fmt=format)
            filename = export_filename(artifact, fmt=format)
        except ExportError as error:
            return error_response(
                status_code=error.status_code,
                code=error.code,
                message=error.message,
            )
        return Response(
            content=data,
            media_type=media_type,
            headers={
                "Content-Disposition": content_disposition_header(filename)
            },
        )


    @app.post("/artifacts/{artifact_id}/rollback")
    async def rollback_artifact(
        artifact_id: str, body: RollbackArtifactBody
    ) -> dict[str, object]:
        snapshot = container.artifact_repository.rollback_to_version(
            artifact_id=artifact_id,
            target_ordinal=body.targetOrdinal,
            source_conversation_id=body.sourceConversationId,
            source_turn_id=body.sourceTurnId,
            note=body.note,
        )
        artifact = snapshot.artifact
        if artifact.storage_path and body.sourceConversationId:
            binding = container.artifact_file_store.binding_for(
                body.sourceConversationId
            )
            if binding is not None:
                try:
                    container.artifact_file_store.restore(
                        binding,
                        artifact.storage_path,
                        snapshot.current_version.content,
                    )
                except Exception:  # noqa: BLE001 文件恢复失败不阻断 DB
                    pass
        return {
            "artifact": artifact_json(snapshot.artifact),
            "currentVersion": artifact_version_json(snapshot.current_version),
        }
