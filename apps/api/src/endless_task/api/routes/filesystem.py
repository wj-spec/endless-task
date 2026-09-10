"""filesystem 域路由（从 app.py 搬出；路径、状态码、响应体零改动）。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query

from endless_task.domain.repositories import ValidationError
from endless_task.workspace_runtime.browse import browse_directory

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.filesystem import RevealPathBody


def register_filesystem_routes(app: FastAPI, container: AppContainer) -> None:
    @app.get("/filesystem/browse")
    async def browse_filesystem(
        path: Optional[str] = Query(None),
        show_hidden: bool = Query(False),
    ) -> dict[str, object]:
        current, items = browse_directory(
            path, show_hidden=show_hidden, home=Path.home()
        )
        return {
            "currentPath": current,
            "items": [
                {
                    "name": item.name,
                    "path": item.path,
                    "kind": item.kind,
                    "size": item.size,
                    "writable": item.writable,
                    "isHidden": item.is_hidden,
                }
                for item in items
            ],
        }


    @app.post("/filesystem/reveal")
    async def reveal_path(body: RevealPathBody) -> dict[str, object]:
        import subprocess

        target = Path(body.path).expanduser().resolve()
        if not target.exists():
            raise ValidationError("路径不存在，无法在访达中显示。")
        if sys.platform != "darwin":
            return {"revealed": False, "message": "当前平台不支持打开系统文件管理器。"}
        try:
            subprocess.Popen(["open", "-R", str(target)])
        except OSError as error:
            raise ValidationError("无法打开系统文件管理器。") from error
        return {"revealed": True}
