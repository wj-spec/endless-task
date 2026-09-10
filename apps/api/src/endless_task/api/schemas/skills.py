"""技能域请求体（从 app.py 搬出，字段与校验规则零改动）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class SkillPatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    disabled: Optional[bool] = None
    #: S5：固定/取消固定进默认目录。
    pinned: Optional[bool] = None


class SkillInstallBody(BaseModel):
    """S8：从生态安装技能（owner/repo 或 owner/repo@skill）。"""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=3, max_length=200)
    scope: Literal["user", "workspace"] = "user"
    workspaceId: Optional[str] = None
    allowUpgrade: bool = False


class SkillSearchBody(BaseModel):
    """S6：本地技能检索。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    scope: Literal["all", "workspace", "global"] = "all"
    workspaceId: Optional[str] = None
    limit: int = Field(default=8, ge=1, le=20)


class SkillValidateBody(BaseModel):
    """S2：校验技能包（目录路径）或内联 SKILL.md 内容。"""

    model_config = ConfigDict(extra="forbid")

    path: Optional[str] = None
    content: Optional[str] = None


class SkillImportBody(BaseModel):
    """S2：从本地目录导入技能包。"""

    model_config = ConfigDict(extra="forbid")

    sourcePath: str
    scope: Literal["user", "workspace"] = "user"
    workspaceId: Optional[str] = None
    allowUpgrade: bool = False


class SkillCreateBody(BaseModel):
    """S2：按模板新建技能（写入用户级或工作区级技能根）。"""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["user", "workspace"] = "user"
    workspaceId: Optional[str] = None
    name: str
    description: str
    whenToUse: Optional[str] = None
    body: str
    modelInvocable: bool = True
    userInvocable: bool = True


class SkillCasesRunBody(BaseModel):
    """S2：跑技能包声明的用例（可对某次运行的真实 trace 校验）。"""

    model_config = ConfigDict(extra="forbid")

    runId: Optional[str] = None
    toolsUsed: list[str] = []
    output: str = ""
