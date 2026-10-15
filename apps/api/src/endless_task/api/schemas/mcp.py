"""mcp 域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict


class McpServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    transport: Literal["stdio", "http"]
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = {}
    enabled: bool = True
    toolCallTimeoutSeconds: float = 60.0
    #: F2（契约新增）：工具白名单。``None``/缺省 = 未配置 = 全部允许；``[]`` = 全部禁用；
    #: 非空 = 只允许列出的**原始工具名**。不传即"全部允许"，与迁移前的行为一致。
    allowedTools: Optional[list[str]] = None
    #: F2（契约新增）：是否允许未值守（定时任务 / 委派子运行）调用。**缺省 false**
    #: （安全默认：新建服务器只在交互运行可用，用户显式打开才进未值守）。
    allowUnattended: bool = False


class McpServerPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    transport: Optional[Literal["stdio", "http"]] = None
    command: Optional[str] = None
    args: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[dict[str, str]] = None
    enabled: Optional[bool] = None
    toolCallTimeoutSeconds: Optional[float] = None
    #: F2（契约新增）：**三态**（用 ``model_fields_set`` 区分后两者，见路由的 C1 修复）：
    #: * **缺字段** = 保留现值（与其它字段同一口径，防"点开关吞数据"）；
    #: * **显式 ``null``** = 重置为**未配置 = 全部允许**（前端"全选"发的就是它）；
    #: * ``[]`` = 全部禁用；非空 = 只允许列出的**原始工具名**。
    allowedTools: Optional[list[str]] = None
    #: F2（契约新增）：缺省 = **保留现值**——这是"点开关不吞数据"的关键。
    #: 注意：``null`` 同样是"保留"（前端从不发 ``null``；布尔开关没有"显式 null =
    #: 重置"的可用语义），这与 ``allowedTools`` 的口径差异是**刻意**的，见路由注释。
    allowUnattended: Optional[bool] = None
