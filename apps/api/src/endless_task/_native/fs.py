"""L2 适配层：文件内核的原生后端（唯一允许 import Rust 扩展的层）。

契约见 `endless_task.workspace_runtime.path_safety` 的既有语义；
安全性质见 `crates/endless-kernel-fs`（逐段 `openat` + `O_NOFOLLOW`，返回**句柄**）。
失败即 raise（fail-closed，**不静默回落到按路径读**）。
"""

from __future__ import annotations

import os

from pathlib import Path

from .. import _native as binding


SEPARATOR = chr(31)  # 与 Rust 侧 fs_* 线协议一致（US，U+001F）


class NativeFsError(Exception):
    """原生文件操作失败。

    `code` 是稳定标识，来自 Rust 侧 `FsError` 的 Display 前缀：
    `invalid_path` / `path_escape` / `path_not_found` / `permission_denied`
    / `file_too_large` / `io_error` / `not_a_directory`。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def split_code(message: str) -> tuple[str, str]:
    """把 `"<code>: <detail>"` 拆成 `(code, detail)`；不成形时退化为 `io_error`。"""
    head, separator, rest = message.partition(":")
    if separator and rest.strip() and all(ch.isalnum() or ch == "_" for ch in head):
        return head, rest.strip()
    return "io_error", message


def read_beneath(root: Path, rel: str, max_bytes: int) -> bytes:
    """通过**句柄**读工作区内的文件。

    与 `Path.read_bytes()` 的关键区别：
    - 逐段 `openat(dirfd, seg, O_NOFOLLOW)`：任一段是符号链接即拒；
    - 返回的是**内容**，读的过程**不再碰路径** ⇒ 校验之后换不掉。

    失败即 raise（含 `code`），由调用方决定映射成哪种 ToolError。
    """
    payload = binding.require()
    try:
        return bytes(payload.fs_read_beneath(str(root), rel, int(max_bytes)))
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error


def write_beneath(root: Path, rel: str, data: bytes) -> None:
    """覆盖写（**create-or-truncate**），全程句柄。

    注意：`O_CREAT` 只创建**文件**，不创建中间目录 —— 目录不存在时如实 raise
    `path_not_found`，**不静默 mkdir**（建目录是另一个操作）。
    """
    payload = binding.require()
    try:
        payload.fs_write_beneath(str(root), rel, bytes(data))
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error


def remove_beneath(root: Path, rel: str) -> None:
    """删除工作区内的**文件**（`unlinkat`，刻意不带 `AT_REMOVEDIR`：不删目录）。"""
    payload = binding.require()
    try:
        payload.fs_remove_beneath(str(root), rel)
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error


def open_beneath(root: Path, rel: str):
    """按句柄打开工作区内文件，返回**文本文件对象**（fd 所有权已交给 Python）。

    语义等价于 `open(path, "r", encoding="utf-8", newline=None)`，
    但路径是先逐段 `openat` + `O_NOFOLLOW` 解析并**锁定成句柄**的：
    打开之后再换路径也影响不到它。
    """
    payload = binding.require()
    try:
        raw_fd = int(payload.fs_open_beneath(str(root), rel))
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error
    return os.fdopen(raw_fd, "r", encoding="utf-8", newline=None)


def mkdir_beneath(root: Path, rel: str) -> None:
    """逐段建目录链（等价 `Path.mkdir(parents=True, exist_ok=True)`）。

    每一段都带 `O_NOFOLLOW`：中途任一段是 symlink 即拒 —— 不会在区外建目录。
    空 rel 视为「工作区根本身」，直接成功。
    """
    payload = binding.require()
    try:
        payload.fs_mkdir_beneath(str(root), rel)
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error


def list_beneath(root: Path, rel: str = "") -> list:
    """列出目录项（通过句柄；不跟随符号链接）。

    返回 `[(name, is_dir, is_file, is_symlink, size), ...]`，**按名字排序**。
    `is_dir` / `is_file` / `size` 都是 **lstat 语义**（不跟随符号链接）。
    语义对齐 `Path.iterdir()` —— 不含 `.` 与 `..`（`readdir` 会返回它们，内核已过滤）。
    `rel` 为空表示工作区根本身。
    """
    payload = binding.require()
    try:
        rows = payload.fs_list_beneath(str(root), rel)
    except ValueError as error:
        code, message = split_code(str(error))
        raise NativeFsError(code, message) from error
    out = []
    for row in rows:
        is_dir, is_file, is_symlink, size, name = str(row).split(SEPARATOR, 4)
        out.append(
            (name, is_dir == "1", is_file == "1", is_symlink == "1", int(size))
        )
    return out
