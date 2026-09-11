"""S1 用户显式调用技能：从用户消息里解析 ``/技能名``。

设计要点（对齐 07 文档与参考项目）：

- **只认 kebab-case 且位于行首或空白之后**的 ``/name``，且名称后必须是空白或行尾；
  这样 ``/Users/example`` 这类路径不会被误判（``Users`` 后面紧跟 ``/``）。
- 解析只产出**候选名**；是否真的存在、是否允许用户调用由服务层判定——
  普通文本里出现 ``/tmp`` 不应报错。
- 一条消息最多请求 ``MAX_SKILL_REQUESTS`` 个技能，避免上下文被撑爆。
"""

from __future__ import annotations

import re
from typing import Tuple

#: 与 frontmatter 的技能名规则一致：小写字母/数字/连字符。
SKILL_NAME_PATTERN = r"[a-z][a-z0-9-]{0,63}"

#: ``/name`` 前必须是行首或空白，后必须是空白或行尾（避免匹配路径）。
SKILL_COMMAND_RE = re.compile(
    rf"(?:^|\s)/({SKILL_NAME_PATTERN})(?=\s|$)",
    re.MULTILINE,
)

MAX_SKILL_REQUESTS = 3


def parse_skill_commands(content: str, *, limit: int = MAX_SKILL_REQUESTS) -> Tuple[str, ...]:
    """按出现顺序返回去重后的技能名候选（最多 ``limit`` 个）。"""
    if not content:
        return ()
    names: list[str] = []
    for match in SKILL_COMMAND_RE.finditer(content):
        name = match.group(1)
        if name in names:
            continue
        names.append(name)
        if len(names) >= max(1, limit):
            break
    return tuple(names)
