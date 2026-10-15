r"""L3 缝层：工作区内容检索的**契约**与 **Python 参考实现**。

分层约束：

- 本模块是 **L3**：只定义契约 + 提供参考实现，**不 import 任何 Rust 扩展**
  （由 `tests/test_rk_native_layering.py` 的守卫守住）。
- 参考实现**不删**：它是差分测试的基准（门①「逐字相同」的另一半），
  也是原生后端不可用时的对照物。

## 契约（必须与 `crates/endless-kernel-search` 逐字一致）

| 维度 | 约定 |
| --- | --- |
| 路径 | 相对 `root`，**用 `/` 分隔**（跨平台稳定，供差分比对） |
| 行号 | 从 1 开始 |
| 行切分 | **Python `str.splitlines()` 的边界集合**（不止 `\n`） |
| 二进制 | 首 8 KiB 含 NUL ⇒ 跳过，计入 `files_skipped` |
| 非 UTF-8 | 跳过，计入 `files_skipped` |
| 超大 | `> max_file_bytes` ⇒ 跳过，计入 `files_skipped` |
| 读不动 | 跳过，计入 `files_skipped`（**不让整个检索失败**） |
| 顺序 | 每层目录**按名字的 UTF-8 字节序**排序后 DFS（确定性，差分才可比） |
| 上限 | `max_results`（全局）/ `max_hits_per_file`（每文件）触发即 `truncated=True` |

## 已知的语义发散风险（差分测试必须限定在交集内）

Rust `regex` 与 Python `re` **不是同一门语言**。契约只覆盖**两者交集**，
差分用例必须使用文档化的模式子集（字面量、字符类、`\d`/`\w`/`\s`、`+*?{}`、
`^$`、`|`、分组与 `(?:...)`）。以下**不保证**一致，不进差分语料：

- 回溯类构造：**已被本契约显式拒绝**（`unsupported_pattern_syntax`）；
- `$` 在多行/末尾换行上的细节、`\b` 的 Unicode 边界、贪婪/占有量词的角落语义；
- 大小写折叠的罕见 Unicode 特例（两侧都做了 Unicode 感知，但不是逐字等价）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence, Tuple

#: 二进制探测窗口（与 Rust 侧 `BINARY_SNIFF_BYTES` 一致）。
BINARY_SNIFF_BYTES = 8192

#: 默认忽略目录（与 Rust 侧 `SearchOptions::default()` 一致）。
DEFAULT_IGNORE_DIRS: Tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".hg",
    ".svn",
)

#: 已知不支持的构造表。**顺序必须与 Rust 侧 `UNSUPPORTED` 完全一致** ——
#: 先命中哪个决定报哪个名字，顺序不同会让差分在"多构造同时出现"时报不同名字。
UNSUPPORTED_CONSTRUCTS: Tuple[Tuple[str, str], ...] = (
    ("(?<=", "lookbehind"),
    ("(?<!", "negative lookbehind"),
    ("(?=", "lookahead"),
    ("(?!", "negative lookahead"),
    ("(?>", "atomic group"),
    ("(?P=", "named backreference"),
)


class SearchPatternError(ValueError):
    """模式被拒。

    `code` 是**稳定标识**，与 Rust 侧 `PatternError` 对齐：
    - `unsupported_pattern_syntax`：用了契约外的回溯类构造（G0-B：**指名**报错）
    - `invalid_pattern`：模式本身语法错误
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        construct: str = "",
        needle: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.construct = construct
        self.needle = needle


@dataclass(frozen=True)
class SearchHit:
    path: str
    line_number: int
    line_text: str


@dataclass(frozen=True)
class SearchOptions:
    max_results: int = 50
    max_file_bytes: int = 1_000_000
    max_hits_per_file: int = 200
    ignore_dirs: Tuple[str, ...] = DEFAULT_IGNORE_DIRS
    file_suffixes: Tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class SearchOutcome:
    hits: Tuple[SearchHit, ...]
    files_scanned: int
    files_skipped: int
    truncated: bool


class SearchBackend(Protocol):
    """检索后端契约。Rust 内核与 Python 参考实现都实现它。"""

    name: str

    def search(
        self,
        root: Path,
        pattern: str,
        *,
        case_sensitive: bool = True,
        options: Optional[SearchOptions] = None,
    ) -> SearchOutcome: ...


def classify_unsupported(pattern: str) -> Optional[Tuple[str, str]]:
    """判定模式里是否含契约外构造，返回 `(construct, needle)`。

    与 Rust 侧 `classify_unsupported` **同一张表、同一顺序、同一语义**。
    """
    for needle, construct in UNSUPPORTED_CONSTRUCTS:
        if needle in pattern:
            return construct, needle

    # 数字反向引用：未被转义的 `\\` 后紧跟 1..9。
    length = len(pattern)
    index = 0
    while index + 1 < length:
        if pattern[index] == "\\":
            run = 0
            while index + run < length and pattern[index + run] == "\\":
                run += 1
            following = index + run
            if (
                run % 2 == 1
                and following < length
                and pattern[following] in "123456789"
            ):
                return "backreference", pattern[index : following + 1]
            index += run
            continue
        index += 1
    return None


def compile_pattern(pattern: str, case_sensitive: bool = True) -> "re.Pattern[str]":
    """先查契约外构造，再编译。**顺序与 Rust 侧一致**。"""
    found = classify_unsupported(pattern)
    if found is not None:
        construct, needle = found
        raise SearchPatternError(
            "unsupported_pattern_syntax",
            f"unsupported_pattern_syntax: {construct} is not supported by the "
            f"regex subset (found {needle!r})",
            construct=construct,
            needle=needle,
        )
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as error:
        raise SearchPatternError("invalid_pattern", f"invalid_pattern: {error}") from error


def _suffix_allowed(name: str, suffixes: Sequence[str]) -> bool:
    if not suffixes:
        return True
    return any(name.endswith(suffix) for suffix in suffixes)


def _sort_key(path: Path) -> bytes:
    """按名字的 UTF-8 字节序排序，对齐 Rust 侧 `PathBuf` 的字节序比较。"""
    return path.name.encode("utf-8", "surrogateescape")


def _collect_files(
    directory: Path,
    options: SearchOptions,
    collected: list[Path],
    skipped: list[int],
) -> None:
    try:
        entries = sorted(directory.iterdir(), key=_sort_key)
    except OSError:
        skipped[0] += 1
        return
    for path in entries:
        try:
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            skipped[0] += 1
            continue
        if is_dir:
            if path.name not in options.ignore_dirs:
                _collect_files(path, options, collected, skipped)
            continue
        if is_file and _suffix_allowed(path.name, options.file_suffixes):
            collected.append(path)


def _relative_posix(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return relative.as_posix()


class PythonSearchBackend:
    """参考实现。**它定义契约的"标准答案"**，Rust 内核必须与它逐字一致。"""

    name = "python-reference"

    def search(
        self,
        root: Path,
        pattern: str,
        *,
        case_sensitive: bool = True,
        options: Optional[SearchOptions] = None,
    ) -> SearchOutcome:
        selected = options or SearchOptions()
        compiled = compile_pattern(pattern, case_sensitive)

        collected: list[Path] = []
        skipped = [0]
        _collect_files(Path(root), selected, collected, skipped)

        hits: list[SearchHit] = []
        files_scanned = 0
        truncated = False
        stop = False

        for path in collected:
            if stop:
                break
            try:
                size = path.stat().st_size
            except OSError:
                skipped[0] += 1
                continue
            if size > selected.max_file_bytes:
                skipped[0] += 1
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                skipped[0] += 1
                continue
            if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
                skipped[0] += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped[0] += 1
                continue

            files_scanned += 1
            relative = _relative_posix(path, Path(root))
            per_file = 0
            # 注意：`str.splitlines()` 的边界集合正是契约里那一条，
            # Rust 侧 `split_lines` 是它的手写镜像。
            for line_number, line in enumerate(text.splitlines(), start=1):
                if per_file >= selected.max_hits_per_file:
                    truncated = True
                    break
                if not compiled.search(line):
                    continue
                hits.append(SearchHit(relative, line_number, line))
                per_file += 1
                if len(hits) >= selected.max_results:
                    truncated = True
                    stop = True
                    break

        return SearchOutcome(
            hits=tuple(hits),
            files_scanned=files_scanned,
            files_skipped=skipped[0],
            truncated=truncated,
        )


def python_reference_backend() -> PythonSearchBackend:
    """取参考实现。

    ⚠️ **这里刻意不提供"自动回落到原生后端"的分支** —— 后端选择是**装配期**的
    决策（L4），不是 L3 的职责；而且 L3 不许 import Rust 扩展（分层守卫）。
    """
    return PythonSearchBackend()