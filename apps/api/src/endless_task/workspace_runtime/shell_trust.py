"""只读命令信任策略（S8）：把"确定只读"的命令从逐条确认里豁免。

默认关闭（``ENDLESS_TASK_SHELL_TRUST_READONLY=0``），此时行为与今天完全一致：
所有 ``run_shell`` 命令都要确认。开启后，仅当整条命令**每一段**都命中只读白名单、
且不含任何写/执行/替换语义时才自动放行。

设计原则：**宁可漏放，不可错放**。任何拿不准的写法一律回落为"需要确认"——
黑名单式的危险命令识别永远追不上 shell 的表达能力（见 06 立场稿 §3），
所以这里的白名单只做"降噪"，真正的边界仍是沙箱 + 快照 + 撤销。
"""

from __future__ import annotations

import re
import shlex
from typing import Optional

from .dangerous_commands import is_dangerous

# 简单只读命令（首词命中即视为只读，参数不再逐个解释）。
_SAFE_COMMANDS = frozenset(
    {
        "basename",
        "cat",
        "column",
        "cut",
        "date",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "egrep",
        "fgrep",
        "file",
        "grep",
        "head",
        "id",
        "jq",
        "ls",
        "nl",
        "pwd",
        "readlink",
        "realpath",
        "rg",
        "sort",
        "stat",
        "tail",
        "tree",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

# 需要参数守卫的只读命令：命中禁止片段即视为不可信。
_ARGUMENT_GUARDS: dict[str, tuple[str, ...]] = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"),
    "sort": ("-o",),
    "column": (),
}

# 允许只读的 git 子命令（更细的守卫见 _git_is_read_only）。
_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "describe",
        "ls-files",
        "blame",
        "stash",
        "tag",
        "config",
        "shortlog",
        "whatchanged",
        "symbolic-ref",
    }
)

_GIT_WRITING_FLAGS = (
    "-d",
    "-D",
    "-m",
    "-M",
    "-c",
    "-C",
    "--delete",
    "--move",
    "--copy",
    "--edit-description",
    "--set-upstream-to",
    "--unset-upstream",
)

# 版本/环境探测：只读且无副作用。
_VERSION_PROBES = frozenset(
    {
        ("python", "--version"),
        ("python", "-v"),
        ("python3", "--version"),
        ("python3", "-v"),
        ("node", "-v"),
        ("node", "--version"),
        ("npm", "--version"),
        ("npm", "-v"),
        ("pip", "--version"),
        ("pip", "list"),
        ("pip3", "--version"),
        ("go", "version"),
        ("cargo", "--version"),
        ("java", "-version"),
        ("git", "--version"),
        ("docker", "--version"),
        ("make", "--version"),
        ("bash", "--version"),
    }
)

# 出现即拒绝的构造：命令替换 / 重定向 / 后台 / 分组 / 变量拼接。
_UNSAFE_CONSTRUCT = re.compile(r"\$\(|`|>|<|(?<!&)&(?!&)|[(){}]|\$\{")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")


def trust_enabled_from_env(value: Optional[str]) -> bool:
    """环境开关：只有显式 ``1/true/yes/on`` 才开启（默认关）。"""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _segments(command: str) -> list[str]:
    return [segment.strip() for segment in _SEGMENT_SPLIT.split(command) if segment.strip()]


def _git_is_read_only(args: list[str]) -> bool:
    if not args:
        return True
    subcommand = args[0]
    if subcommand not in _GIT_READ_ONLY_SUBCOMMANDS:
        return False
    rest = args[1:]
    if subcommand in {"branch", "tag"}:
        # 无参数/纯列表读取；任何写标志或分支名都不可信。
        if not rest:
            return True
        return all(
            item in {"-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv"}
            for item in rest
        )
    if subcommand == "remote":
        if not rest:
            return True
        return all(item in {"-v", "--verbose", "show", "get-url"} for item in rest)
    if subcommand == "stash":
        return all(item in {"list", "show"} for item in rest)
    if subcommand == "config":
        return bool(rest) and all(
            item in {"--get", "--get-all", "--list", "-l", "--get-regexp"}
            for item in rest[:1]
        )
    if any(flag in rest for flag in _GIT_WRITING_FLAGS):
        return False
    return True


def _segment_is_read_only(segment: str) -> bool:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return False
    if not tokens:
        return False
    command = tokens[0].rsplit("/", 1)[-1]
    args = tokens[1:]

    if args and (command, args[0]) in _VERSION_PROBES:
        return True
    if command in {"python", "python3", "node", "perl", "ruby", "bash", "sh", "zsh", "env"}:
        return False
    if command == "git":
        return _git_is_read_only(args)
    if command == "sudo":
        return False
    if command in _SAFE_COMMANDS:
        blocked = _ARGUMENT_GUARDS.get(command)
        if blocked:
            return not any(flag in args for flag in blocked)
        return True
    return False


def is_read_only_command(command: str) -> bool:
    """整条命令是否"每一段都确定只读"。任何不确定的构造都返回 False。"""
    if not command or not command.strip():
        return False
    if _UNSAFE_CONSTRUCT.search(command):
        return False
    segments = _segments(command)
    if not segments:
        return False
    return all(_segment_is_read_only(segment) for segment in segments)


class ShellTrustPolicy:
    """只读命令信任策略（按工具实例持有，默认关闭）。"""

    def __init__(self, *, enabled: bool = False, extra_prefixes: tuple[str, ...] = ()) -> None:
        self._enabled = enabled
        self._extra = tuple(prefix.strip() for prefix in extra_prefixes if prefix.strip())

    @property
    def enabled(self) -> bool:
        return self._enabled

    def is_trusted(self, command: str) -> bool:
        """命令是否可免确认（信任关闭时恒为 False）。"""
        if not self._enabled:
            return False
        if _UNSAFE_CONSTRUCT.search(command):
            return False
        if is_read_only_command(command):
            return True
        if not self._extra:
            return False
        # 追加前缀也必须覆盖**每一段**，否则 `pytest && rm -rf x` 会被误放。
        segments = _segments(command)
        if not segments:
            return False
        return all(
            any(segment.startswith(prefix) for prefix in self._extra)
            for segment in segments
        )

    def allows(self, tool_name: str, call) -> bool:
        """执行协调器使用的信任裁决（只对 run_shell 生效，危险命令恒拒绝）。"""
        if not self._enabled or tool_name != "run_shell":
            return False
        try:
            command = call.require_argument("command", str)
        except Exception:  # noqa: BLE001 参数异常一律视为不可信
            return False
        if is_dangerous(command):
            return False
        return self.is_trusted(command)
