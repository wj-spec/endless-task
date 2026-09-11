"""providers 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

搬家而非重构：每个 handler 仍闭包在 `container` 上，行为与搬家前一致。
"""

from typing import Optional

from fastapi import FastAPI

from endless_task.runtime.provider import ProviderError
from endless_task.storage.sqlite_provider_profile_repository import (
    ProviderModel,
    ProviderProfileDraft,
)

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.providers import (
    ProviderDefaultBody,
    ProviderDefaultModelBody,
    ProviderModelBody,
    ProviderModelPatchBody,
    ProviderProfileBody,
    ProviderProfilePatchBody,
)


def register_providers_routes(app: FastAPI, container: AppContainer) -> None:
    def _provider_model_json(
        model: ProviderModel,
        *,
        default_model: str,
    ) -> dict[str, object]:
        return {
            "providerProfileId": model.provider_profile_id,
            "modelId": model.model_id,
            "displayName": model.display_name,
            "source": model.source,
            "enabled": model.enabled,
            "isDefault": model.model_id == default_model,
            "lastSeenAt": model.last_seen_at,
        }


    def _provider_profile_json(profile) -> dict[str, object]:
        configured = container.provider_manager.is_configured(profile)
        connection_state = profile.connection_state
        if profile.is_builtin:
            connection_state = "ready" if configured else "failed"
        return {
            "id": profile.id,
            "name": profile.name,
            "kind": profile.kind,
            "baseUrl": profile.base_url,
            "defaultModel": profile.default_model,
            "timeoutSeconds": profile.timeout_seconds,
            "enabled": profile.enabled,
            "isBuiltin": profile.is_builtin,
            "isDefault": (
                profile.id
                == container.provider_profile_repository.get_default_profile_id()
            ),
            "configured": configured,
            "apiKeyConfigured": container.provider_secret_store.has(
                profile.api_key_ref
            ),
            "connectionState": connection_state,
            "lastCheckedAt": profile.last_checked_at,
            "lastError": profile.last_error,
            "models": [
                _provider_model_json(model, default_model=profile.default_model)
                for model in container.provider_profile_repository.list_models(profile.id)
            ],
            "createdAt": profile.created_at,
            "updatedAt": profile.updated_at,
        }


    def _provider_draft(
        *,
        current=None,
        name: Optional[str] = None,
        default_model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key_ref: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> ProviderProfileDraft:
        return ProviderProfileDraft(
            name=name if name is not None else current.name,
            default_model=(
                default_model if default_model is not None else current.default_model
            ),
            base_url=base_url if base_url is not None else current.base_url,
            api_key_ref=(
                api_key_ref if api_key_ref is not None else current.api_key_ref
            ),
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else current.timeout_seconds
            ),
            enabled=enabled if enabled is not None else current.enabled,
        )


    @app.get("/providers")
    async def list_providers() -> dict[str, object]:
        return {
            "items": [
                _provider_profile_json(profile)
                for profile in container.provider_profile_repository.list_profiles()
            ]
        }


    @app.post("/providers", status_code=201)
    async def create_provider_profile(
        body: ProviderProfileBody,
    ) -> dict[str, object]:
        if (
            body.apiKey
            and body.apiKey.strip()
            and body.apiKeyRef
            and body.apiKeyRef.strip()
        ):
            raise ApiRequestError(
                "invalid_provider_credentials",
                "API Key 和 API Key 引用不能同时填写。",
            )
        profile = container.provider_profile_repository.create_profile(
            ProviderProfileDraft(
                name=body.name,
                default_model=body.defaultModel,
                base_url=body.baseUrl,
                api_key_ref=body.apiKeyRef,
                timeout_seconds=body.timeoutSeconds,
                enabled=body.enabled,
            )
        )
        try:
            if body.apiKey and body.apiKey.strip():
                reference = container.provider_secret_store.put(
                    profile.id, body.apiKey
                )
                profile = container.provider_profile_repository.update_profile(
                    profile.id,
                    _provider_draft(current=profile, api_key_ref=reference),
                )
        except Exception:
            container.provider_secret_store.delete(profile.id)
            container.provider_profile_repository.delete_profile(profile.id)
            raise
        return {"profile": _provider_profile_json(profile)}


    @app.patch("/providers/{profile_id}")
    async def patch_provider_profile(
        profile_id: str, body: ProviderProfilePatchBody
    ) -> dict[str, object]:
        current = container.provider_profile_repository.get_profile(profile_id)
        if current.is_builtin and body.apiKey is not None:
            raise ApiRequestError(
                "builtin_provider_credentials",
                "环境配置的模型服务不能在此修改 API Key。",
            )
        if (
            body.apiKey
            and body.apiKey.strip()
            and body.apiKeyRef
            and body.apiKeyRef.strip()
        ):
            raise ApiRequestError(
                "invalid_provider_credentials",
                "API Key 和 API Key 引用不能同时填写。",
            )
        next_reference = body.apiKeyRef
        stored_key_changed = body.apiKey is not None and bool(body.apiKey.strip())
        previous_stored_secret = None
        if stored_key_changed:
            if current.api_key_ref == container.provider_secret_store.reference_for(
                profile_id
            ):
                previous_stored_secret = container.provider_secret_store.resolve(
                    current.api_key_ref
                )
            next_reference = container.provider_secret_store.put(
                profile_id, body.apiKey or ""
            )
        try:
            profile = container.provider_profile_repository.update_profile(
                profile_id,
                _provider_draft(
                    current=current,
                    name=body.name,
                    default_model=body.defaultModel,
                    base_url=body.baseUrl,
                    api_key_ref=next_reference,
                    timeout_seconds=body.timeoutSeconds,
                    enabled=body.enabled,
                ),
            )
        except Exception:
            if stored_key_changed:
                if previous_stored_secret:
                    container.provider_secret_store.put(
                        profile_id, previous_stored_secret
                    )
                else:
                    container.provider_secret_store.delete(profile_id)
            raise
        if body.baseUrl is not None or stored_key_changed or body.apiKeyRef is not None:
            profile = container.provider_profile_repository.set_connection_state(
                profile_id,
                state="untested",
            )
        stored_reference = container.provider_secret_store.reference_for(profile_id)
        if (
            body.apiKeyRef is not None
            and current.api_key_ref == stored_reference
            and profile.api_key_ref != stored_reference
        ):
            container.provider_secret_store.delete(profile_id)
        await container.provider_manager.invalidate(profile.id)
        return {"profile": _provider_profile_json(profile)}


    @app.delete("/providers/{profile_id}", status_code=204)
    async def delete_provider_profile(profile_id: str) -> None:
        container.provider_profile_repository.delete_profile(profile_id)
        container.provider_secret_store.delete(profile_id)
        await container.provider_manager.invalidate(profile_id)


    @app.post("/providers/default")
    async def set_default_provider(body: ProviderDefaultBody) -> dict[str, object]:
        container.provider_profile_repository.set_default_profile_id(body.profileId)
        profile = container.provider_profile_repository.get_profile(body.profileId)
        return {"profile": _provider_profile_json(profile)}


    @app.post("/providers/{profile_id}/refresh-models")
    async def refresh_provider_models(profile_id: str) -> dict[str, object]:
        try:
            discovered = await container.provider_manager.discover_models(profile_id)
        except ProviderError as error:
            container.provider_profile_repository.set_connection_state(
                profile_id,
                state="failed",
                error=error.safe_message,
            )
            raise ApiRequestError(
                error.code,
                error.safe_message,
                status_code=422,
            ) from error
        except ValueError as error:
            message = str(error) or "无法获取模型列表。"
            container.provider_profile_repository.set_connection_state(
                profile_id,
                state="failed",
                error=message,
            )
            raise ApiRequestError(
                "model_discovery_unavailable",
                message,
                status_code=422,
            ) from error
        models = container.provider_profile_repository.replace_discovered_models(
            profile_id,
            discovered,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        if not profile.default_model and models:
            profile = container.provider_profile_repository.set_default_model(
                profile_id,
                models[0].model_id,
            )
        profile = container.provider_profile_repository.set_connection_state(
            profile_id,
            state="ready",
        )
        return {"profile": _provider_profile_json(profile)}


    @app.post("/providers/{profile_id}/models", status_code=201)
    async def add_provider_model(
        profile_id: str,
        body: ProviderModelBody,
    ) -> dict[str, object]:
        model = container.provider_profile_repository.add_model(
            profile_id,
            model_id=body.modelId,
            display_name=body.displayName,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        if not profile.default_model:
            profile = container.provider_profile_repository.set_default_model(
                profile_id,
                model.model_id,
            )
        return {
            "model": _provider_model_json(
                model,
                default_model=profile.default_model,
            ),
            "profile": _provider_profile_json(profile),
        }


    @app.patch("/providers/{profile_id}/models/{model_id:path}")
    async def patch_provider_model(
        profile_id: str,
        model_id: str,
        body: ProviderModelPatchBody,
    ) -> dict[str, object]:
        model = container.provider_profile_repository.set_model_enabled(
            profile_id,
            model_id,
            enabled=body.enabled,
        )
        profile = container.provider_profile_repository.get_profile(profile_id)
        return {
            "model": _provider_model_json(
                model,
                default_model=profile.default_model,
            )
        }


    @app.delete("/providers/{profile_id}/models/{model_id:path}", status_code=204)
    async def delete_provider_model(profile_id: str, model_id: str) -> None:
        container.provider_profile_repository.delete_model(profile_id, model_id)


    @app.post("/providers/{profile_id}/default-model")
    async def set_provider_default_model(
        profile_id: str,
        body: ProviderDefaultModelBody,
    ) -> dict[str, object]:
        profile = container.provider_profile_repository.set_default_model(
            profile_id,
            body.modelId,
        )
        return {"profile": _provider_profile_json(profile)}
