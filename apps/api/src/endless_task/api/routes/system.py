"""系统域路由：健康/能力/权限模式（从 app.py 搬出；路径、响应体零改动）。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from endless_task.domain.models import PermissionMode
from endless_task.runtime.openai_compatible_provider import UnconfiguredProvider
from endless_task.skills import Skill

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.system import SetPermissionBody


def _tool_platform_v2_profile_name() -> str:
    """Name of the calibrated provider profile used by the v2 tool path."""
    from endless_task.tool_platform import create_openai_compatible_profile

    return create_openai_compatible_profile().name


def register_system_routes(app: FastAPI, container: AppContainer) -> None:
        @app.get("/health")
        async def health() -> dict[str, object]:
            selection = container.provider_manager.default_selection()
            return {
                "status": "ok",
                "provider": selection.provider.name,
                "model": selection.model,
                "providerConfigured": not isinstance(
                    selection.provider,
                    UnconfiguredProvider,
                ),
                "embedding": {
                    "enabled": container.settings.embedding_enabled,
                    "backend": (
                        container.settings.embedding_backend
                        if container.settings.embedding_enabled
                        else None
                    ),
                    "model": (
                        container.embedding_indexer.model_name
                        if container.embedding_indexer is not None
                        else None
                    ),
                    "ready": (
                        container.embedding_indexer is not None
                        and not container.embedding_indexer.unavailable
                    ),
                },
            }

        @app.get("/capabilities")
        async def capabilities() -> dict[str, object]:
            skills_by_path: dict[str, Skill] = {}
            for skill in container.skill_service.list_skills():
                skills_by_path[str(skill.file_path)] = skill
            for workspace in container.workspace_repository.list_workspaces():
                if workspace.root_path:
                    for skill in container.skill_service.list_skills(
                        Path(workspace.root_path),
                        workspace_id=workspace.id,
                    ):
                        skills_by_path[str(skill.file_path)] = skill
            skills = list(skills_by_path.values())
            skill_diagnostics = [
                {
                    "path": str(diagnostic.path),
                    "code": diagnostic.code,
                    "message": diagnostic.message,
                }
                for skill in skills
                for diagnostic in skill.diagnostics
            ]
            skills_state = "degraded" if skill_diagnostics else "ok"

            mcp_statuses = container.mcp_manager.list_statuses()
            mcp_issues: list[str] = []
            for status in mcp_statuses:
                if status.state == "reconnecting":
                    mcp_issues.append("mcp:reconnecting")
                elif status.enabled and status.state != "connected":
                    mcp_issues.append("mcp:error")
            mcp_state = "degraded" if mcp_issues else "ok"

            provider_selection = container.provider_manager.default_selection()
            provider_configured = not isinstance(
                provider_selection.provider,
                UnconfiguredProvider,
            )
            provider_state = "ok" if provider_configured else "unavailable"
            provider_issues = [] if provider_configured else ["provider:unconfigured"]

            embedding_ready = (
                container.embedding_indexer is not None
                and not container.embedding_indexer.unavailable
            )
            embedding_issues = (
                [] if not container.settings.embedding_enabled or embedding_ready
                else ["embedding:unavailable"]
            )
            embedding_state = "ok" if not embedding_issues else "degraded"

            states = (skills_state, mcp_state, provider_state, embedding_state)
            if "unavailable" in states:
                summary_state = "unavailable"
            elif "degraded" in states:
                summary_state = "degraded"
            else:
                summary_state = "ok"
            issues = [
                *(["skill:diagnostics"] if skill_diagnostics else []),
                *mcp_issues,
                *provider_issues,
                *embedding_issues,
            ]
            return {
                "summary": {"state": summary_state, "issues": issues},
                "skills": {
                    "state": skills_state,
                    "total": len(skills),
                    "enabled": sum(not skill.disabled for skill in skills),
                    "diagnostics": skill_diagnostics,
                },
                "mcp": {
                    "state": mcp_state,
                    "servers": [
                        {
                            "id": status.server_id,
                            "name": status.name,
                            "state": status.state,
                            "toolCount": status.tool_count,
                            "lastError": status.last_error,
                        }
                        for status in mcp_statuses
                    ],
                },
                "provider": {
                    "state": provider_state,
                    "defaultProfileId": provider_selection.profile.id,
                    "profileName": provider_selection.profile.name,
                    "model": provider_selection.model,
                    "configured": provider_configured,
                    "fallback": False,
                },
                "embedding": {
                    "state": embedding_state,
                    "enabled": container.settings.embedding_enabled,
                    "backend": (
                        container.settings.embedding_backend
                        if container.settings.embedding_enabled
                        else None
                    ),
                    "ready": embedding_ready,
                },
                "toolPlatformV2": {
                    "enabled": container.settings.tool_platform_v2_enabled,
                    "profileName": (
                        _tool_platform_v2_profile_name()
                        if container.settings.tool_platform_v2_enabled
                        else None
                    ),
                },
                "contextEngineV2": {
                    "enabled": container.settings.context_engine_v2_enabled,
                },
                "delegation": {
                    "enabled": container.settings.delegation_mode != "0",
                    "mode": (
                        container.settings.delegation_mode
                        if container.settings.delegation_mode != "0"
                        else None
                    ),
                },
                "skillPackages": {
                    "enabled": container.settings.skill_packages_enabled,
                },
                "providerRetry": {
                    "enabled": container.settings.provider_retry_mode != "0",
                    "mode": (
                        container.settings.provider_retry_mode
                        if container.settings.provider_retry_mode != "0"
                        else None
                    ),
                },
                "runtimeTrace": {
                    "mode": container.settings.runtime_trace_mode,
                },
                "otlpExport": {
                    "enabled": container.settings.otel_export_mode != "0",
                    "mode": (
                        container.settings.otel_export_mode
                        if container.settings.otel_export_mode != "0"
                        else None
                    ),
                },
                "executionBackend": {
                    "mode": container.settings.execution_backend_mode or None,
                },
                "stopPolicy": {
                    "enabled": container.settings.stop_policy_enforcement,
                },
            }

        @app.get("/settings/permissions")
        async def get_permission_mode() -> dict[str, object]:
            mode, updated_at = container.preferences_repository.get_permission_mode()
            return {"mode": mode.value, "updatedAt": updated_at}

        @app.post("/settings/permissions")
        async def set_permission_mode(
            body: SetPermissionBody,
        ) -> dict[str, object]:
            try:
                target = PermissionMode(body.mode)
            except ValueError as error:
                raise ApiRequestError(
                    "invalid_request",
                    "mode 必须是 confirm_every_time / trust_local_writes / trust_all。",
                ) from error
            current, _ = container.preferences_repository.get_permission_mode()
            if target.is_escalation_from(current) and not body.acknowledge:
                raise ApiRequestError(
                    "invalid_request",
                    "提权需要 acknowledge=true 显式确认。",
                )
            mode, updated_at = container.preferences_repository.set_permission_mode(
                target
            )
            return {"mode": mode.value, "updatedAt": updated_at}
