"""测试包入口：让测试进程与开发机上的真实技能目录隔离。

设计要点：技能发现现在会扫描本地共享目录（`~/.claude/skills`、`~/.agents/skills` 等）。
若不做隔离，开发机上装了 41 个共享技能的机器上跑测试，所有"只应看到我造的技能"
的断言都会失败。这里把共享根指向一个空的临时 home（可被显式设置的
``ENDLESS_TASK_SKILL_HOME`` 覆盖）。
"""

from __future__ import annotations

import os
import tempfile

_SANDBOX_HOME = tempfile.mkdtemp(prefix="endless-task-test-home-")

# 用 setdefault：显式设置的（例如真实共享目录的集成测试）优先。
os.environ.setdefault("ENDLESS_TASK_SKILL_HOME", _SANDBOX_HOME)
