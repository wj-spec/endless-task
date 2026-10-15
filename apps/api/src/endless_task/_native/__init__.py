"""L2 适配层：Rust 内核扩展的 Python 薄封装。

分层约束：本包是**唯一**允许 import
Rust 扩展的地方；L3 缝层与 L4 策略层一律不许直接 import。

失败语义（用户 2026-09-16 拍板 = **fail-closed**）：扩展不可用或 ABI 不匹配时，
相关能力**不可用并指名报错**，**绝不静默降级**（口径见 15 号文档）。
"""
from __future__ import annotations

from typing import Optional

HOST_ABI_VERSION = 1

def _load_binding():
    """加载 Rust 扩展；失败时返回 (None, 指名原因)。

    刻意放在函数里而不是模块级 try/except：tests/module_scope.py 的**模块级**
    收集器不采集 `except ... as x` 的目标名（函数级收集器才采集），模块级写法会被
    卫生门禁误报为未定义名字。放在函数里同时是更清晰的「加载自检」步骤。
    """
    try:
        from .. import _native_core

        return _native_core, None
    except Exception as exc:  # 扩展缺失 / 编译失败 / 导入期异常
        return None, f"{type(exc).__name__}: {exc}"


_LOADED = _load_binding()
_BINDING = _LOADED[0]
_LOAD_ERROR = _LOADED[1]

def is_available() -> bool:
    """扩展是否可用。不可用时上层必须走 fail-closed 分支。"""
    return _BINDING is not None

def unavailable_reason() -> Optional[str]:
    """扩展不可用的**指名**原因（可直接展示给用户 / 写进 tool error）。"""
    return _LOAD_ERROR

def require():
    """取用扩展；不可用即**抛错并指名原因**（fail-closed，不返回 None）。"""
    if _BINDING is None:
        raise RuntimeError(f"native kernel unavailable: {_LOAD_ERROR}")
    return _BINDING

def abi_version() -> Optional[int]:
    """扩展编译时的内核 ABI 版本；不可用时返回 None。"""
    if _BINDING is None:
        return None
    return int(_BINDING.abi_version())

def check_abi() -> Optional[str]:
    """ABI 自检。返回 None = 通过；否则返回**指名**原因（23 的 5.2 规则 6）。"""
    if _BINDING is None:
        return f"extension not loaded: {_LOAD_ERROR}"
    found = abi_version()
    if found != HOST_ABI_VERSION:
        return f"kernel ABI mismatch: host expects {HOST_ABI_VERSION}, build reports {found}"
    return None

def capabilities() -> tuple:
    """扩展的**如实**能力自证；(name, available, detail) 三元组元组。

    底层刻意返回 "name|1|detail" 字符串行而不是元组：pyo3 0.29.2 下
    `Vec<(String, bool, String)>` 会产出形状错乱且含空指针的元组，
    **一索引就段错误**。在这里解析回三元组。
    """
    if _BINDING is None:
        return ()
    parsed = []
    for row in _BINDING.capabilities():
        name, flag, detail = str(row).split("|", 2)
        parsed.append((name, flag == "1", detail))
    return tuple(parsed)
