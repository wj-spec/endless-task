"""API 层错误类型（从 app.py 搬出，供各路由模块共用）。

不放在 `app.py` 里的原因：路由模块要抛同一个错误、由同一个异常处理器翻译成
HTTP 响应；如果错误类留在 app.py，`routes/*` 与 `app.py` 就会互相导入。
"""

from __future__ import annotations

import uuid
from typing import Mapping, Optional

from fastapi.responses import JSONResponse


class ApiRequestError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: Optional[Mapping[str, object]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: Optional[Mapping[str, object]] = None,
) -> JSONResponse:
    payload: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "correlationId": correlation_id(),
    }
    if details is not None:
        payload["details"] = dict(details)
    return JSONResponse(
        status_code=status_code,
        content={"error": payload},
    )


def correlation_id() -> str:
    return f"corr_{uuid.uuid4().hex}"
