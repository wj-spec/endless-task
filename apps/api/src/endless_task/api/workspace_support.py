"""工作区归属解析（从 app.py 的 create_app 闭包里提出来变成显式依赖）。

这两个判定被 conversations / knowledge / artifacts 等多族共用，所以不能跟着
某一个路由族走；做成模块级函数、显式接收 `container`，路由模块与 app.py 都能用。

行为零改动：错误码、状态码、文案与搬家前一致。
"""

from __future__ import annotations

from typing import Optional

from endless_task.domain.repositories import NotFoundError

from .errors import ApiRequestError


def resolve_workspace_reference(container, value: Optional[str]) -> Optional[str]:
    """把请求里的归属值落为列值："general"/空 → None（全局）；其余校验存在。"""
    text = (value or "").strip()
    if not text or text == "general":
        return None
    container.workspace_repository.get_workspace(text)
    return text


def require_bound_workspace(container, value: Optional[str]) -> str:
    """校验目标工作区存在且已绑定本地目录，返回其 id。

    会话必须归属到已绑定目录的工作区（必选绑定产品设定）。失败抛出
    ApiRequestError（409），不返回 None。
    """
    text = (value or "").strip()
    if not text or text == "general":
        raise ApiRequestError(
            "workspace_required",
            "会话必须归属到一个工作区，请先选择并绑定工作区目录。",
            status_code=409,
        )
    try:
        workspace = container.workspace_repository.get_workspace(text)
    except NotFoundError as error:
        raise ApiRequestError(
            "workspace_not_found",
            "所选工作区已不存在，请重新选择。",
            status_code=404,
        ) from error
    if not workspace.root_path:
        raise ApiRequestError(
            "workspace_not_bound",
            "该工作区尚未绑定本地目录，请先绑定目录后再创建/迁移会话。",
            status_code=409,
        )
    return text
