"""user-profile 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

from fastapi import FastAPI

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.userprofile import UserProfileBody


def register_userprofile_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/user-profile")
    async def get_user_profile(scope_key: Optional[str] = None) -> dict[str, object]:
        """B5：当前用户画像（注入用的稳定前缀块）。"""
        service = container.user_profile_service
        if service is None:
            return {"content": "", "version": 0, "signature": "", "lines": []}
        block = service.block_for(scope_key or GENERAL_SCOPE_KEY)
        return block.as_json()


    @app.put("/user-profile")
    async def put_user_profile(body: UserProfileBody) -> dict[str, object]:
        """B5：用户手写画像（整段替换；自动重建不再覆盖它）。"""
        service = container.user_profile_service
        if service is None:
            raise ValidationError("User profile is not available.")
        result = service.set_manual(
            body.content, body.scopeKey or GENERAL_SCOPE_KEY
        )
        return result.as_json()


    @app.post("/user-profile/refresh")
    async def refresh_user_profile(
        scope_key: Optional[str] = None,
        force: bool = False,
    ) -> dict[str, object]:
        """B5：从记忆重建画像（默认遵守节流，force=true 立即重建）。"""
        service = container.user_profile_service
        if service is None:
            return {"content": "", "version": 0, "signature": "", "lines": []}
        result = service.refresh(scope_key or GENERAL_SCOPE_KEY, force=force)
        return result.as_json()
