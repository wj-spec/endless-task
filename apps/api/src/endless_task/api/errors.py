"""API 层错误类型（从 app.py 搬出，供各路由模块共用）。

不放在 `app.py` 里的原因：路由模块要抛同一个错误、由同一个异常处理器翻译成
HTTP 响应；如果错误类留在 app.py，`routes/*` 与 `app.py` 就会互相导入。
"""

from __future__ import annotations

from typing import Mapping, Optional


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
