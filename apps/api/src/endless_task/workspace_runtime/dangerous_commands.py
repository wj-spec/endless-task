"""shell 危险命令名单：恒显式确认（提权模式也不放行）。

参考 Hermes dangerous-command approval 与 deepseek-harness tools/pre-execute
的 allow/deny/ask 策略。名单按首词前缀匹配，可配置扩展。
"""

from __future__ import annotations

import re

_DANGEROUS_PATTERNS = [
    # 删除类：不可逆破坏
    re.compile(r"^\s*rm\b"),
    re.compile(r"^\s*rmdir\b"),
    re.compile(r"^\s*unlink\b"),
    re.compile(r"^\s*truncate\b"),
    re.compile(r"^\s*shred\b"),
    # git 破坏/外发
    re.compile(r"^\s*git\s+(push|reset\s+--hard|clean\s+-f|checkout\s+--\s|filter-branch|rebase\s+--interactive)"),
    re.compile(r"^\s*git\s+remote\s+(add|set-url)\b"),
    # 数据外发：curl/wget 写请求或向非 localhost 目标
    re.compile(r"^\s*(curl|wget)\b"),
    # 提权与系统写
    re.compile(r"^\s*sudo\b"),
    re.compile(r"^\s*(chmod|chown|chgrp)\b"),
    re.compile(r"^\s*mkfs\.\w+\b"),
    re.compile(r"^\s*dangerzone\b"),
    # 危险组合：重定向到系统文件
    re.compile(r">\s*/etc/|>\s*/dev/(sd|disk|rdisk)"),
]


def is_dangerous(command: str) -> bool:
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return True
    return False
