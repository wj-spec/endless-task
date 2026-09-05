"""Unattended-executor tool enforcement seam (M3B slice F).

05 §RS-6 slice-2c 结论：交互主路径不把内置工具强制 backend 化（丢 shell watchdog /
fs 锁 / receipt 双模型净价值为负）；真隔离落点是 **unattended/container**。本模块提供
executor 级注入 seam：

- ``ToolEnforcement``：late-binding 配置容器——executor 装配时先持空 holder，工具注册
  完成后（含 built-in 与动态 MCP）再由组合根填入 backend 代理工具；resolve 时按名替换，
  未启用/未配置的名字全部回落到内层 registry。
- ``EnforcingToolRegistry``：委托内层 ``ToolRegistry``，仅对已配置名字返回代理工具，
  其余行为（definitions 等）完全一致——executor 无法察觉差异，schema/能力/审批元数据
  不变（代理工具与原始工具同 class、同 definition）。

模式（``ENDLESS_TASK_EXECUTION_BACKEND``）："" = 不启用（现状）；``local`` /
``container`` = unattended 写/删工具经 ExecutionEnvironment 执行。文件 mutation 在
local 与 container backend 下同为 workspace 内受限执行（container 的隔离差异作用于
进程执行；unattended 无 shell——run_shell 是 REQUIRED，被 unattended 过滤器隐藏），
因此两值当前都解析到 LocalExecutionBackend 形态，取值差异仅为配置意图与未来进程
enforcement 的显式声明。
"""

from __future__ import annotations

from typing import Mapping

from endless_task.tooling import RegisteredTool, ToolRegistry


class ToolEnforcement:
    """Late-bound replacements by tool name for unattended executors."""

    def __init__(self) -> None:
        self._replacements: dict[str, RegisteredTool] = {}
        self._enabled = False

    def configure(self, *, replacements: Mapping[str, RegisteredTool]) -> None:
        self._replacements = dict(replacements)
        self._enabled = bool(self._replacements)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def resolve(self, name: str, fallback: RegisteredTool) -> RegisteredTool:
        if not self._enabled:
            return fallback
        return self._replacements.get(name, fallback)


class EnforcingToolRegistry(ToolRegistry):
    """ToolRegistry view that swaps configured names for their replacement.

    Delegates everything to ``inner`` except ``resolve``, which returns the
    enforcement replacement when configured. Kept late-bound: tools registered
    on ``inner`` after this view is built (built-ins, dynamic MCP bridges) are
    visible through delegation.
    """

    def __init__(self, inner: ToolRegistry, enforcement: ToolEnforcement) -> None:
        self._inner = inner
        self._enforcement = enforcement

    def resolve(self, name: str) -> RegisteredTool:
        base = self._inner.resolve(name)
        return self._enforcement.resolve(name, base)

    def definitions(self):
        return self._inner.definitions()

    def register(self, tool: RegisteredTool) -> None:
        self._inner.register(tool)

    def replace_tools(self, tools, *, namespace: str = "mcp__") -> None:
        self._inner.replace_tools(tools, namespace=namespace)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


__all__ = [
    "EnforcingToolRegistry",
    "ToolEnforcement",
]
