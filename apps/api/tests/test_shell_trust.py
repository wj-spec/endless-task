"""S8 只读命令信任：白名单判定与绕过尝试。"""

from __future__ import annotations

import unittest

from endless_task.workspace_runtime.shell_trust import (
    ShellTrustPolicy,
    is_read_only_command,
    trust_enabled_from_env,
)


class IsReadOnlyCommandTest(unittest.TestCase):
    def test_plain_read_only_commands(self) -> None:
        for command in (
            "ls -la",
            "pwd",
            "cat README.md",
            "head -n 20 app.log",
            "tail -f app.log",
            "wc -l src/*.py",
            "git status",
            "git log --oneline -5",
            "git diff HEAD~1",
            "git branch",
            "git branch -a",
            "git remote -v",
            "git rev-parse HEAD",
            "python --version",
            "node -v",
            "npm --version",
            "grep -rn TODO src",
            "rg TODO",
            "du -sh .",
            "stat src/main.py",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_read_only_command(command), command)

    def test_pipelines_and_chains_of_read_only_commands(self) -> None:
        for command in (
            "cat a.md | grep x",
            "ls && git status",
            "git status || pwd",
            "cat a.md | grep x | wc -l",
        ):
            with self.subTest(command=command):
                self.assertTrue(is_read_only_command(command), command)

    def test_write_and_execution_constructs_are_rejected(self) -> None:
        for command in (
            "rm -rf build",
            "ls > out.txt",
            "cat a.md >> b.md",
            "echo $(rm -rf x)",
            "echo `ls`",
            "ls; rm -rf x",
            "ls && rm -rf x",
            "ls &",
            "find . -delete",
            "find . -name '*.py' -exec rm {} \\;",
            "sort -o out.txt in.txt",
            "git branch -D main",
            "git remote add origin https://x",
            "git config --global user.name x",
            "git stash drop",
            "git tag v1",
            "python -c 'import os'",
            "python3 script.py",
            "node -e '1'",
            "perl -e '1'",
            "sh -c 'ls'",
            "bash script.sh",
            "env FOO=1 ls",
            "sudo ls",
            "cat a.md < b.md",
            "(ls)",
            "{ ls; }",
            "VAR=1 ls",
        ):
            with self.subTest(command=command):
                self.assertFalse(is_read_only_command(command), command)

    def test_empty_and_garbage(self) -> None:
        self.assertFalse(is_read_only_command(""))
        self.assertFalse(is_read_only_command("   "))
        self.assertFalse(is_read_only_command("cat 'unclosed"))


class ShellTrustPolicyTest(unittest.TestCase):
    def test_disabled_by_default(self) -> None:
        policy = ShellTrustPolicy()
        self.assertFalse(policy.enabled)
        self.assertFalse(policy.is_trusted("ls -la"))

    def test_enabled_trusts_read_only_only(self) -> None:
        policy = ShellTrustPolicy(enabled=True)
        self.assertTrue(policy.is_trusted("ls -la"))
        self.assertTrue(policy.is_trusted("git status"))
        self.assertFalse(policy.is_trusted("rm -rf build"))
        self.assertFalse(policy.is_trusted("ls > out.txt"))

    def test_extra_prefixes_are_opt_in_and_guarded(self) -> None:
        policy = ShellTrustPolicy(enabled=True, extra_prefixes=("pytest", "npm test"))
        self.assertTrue(policy.is_trusted("pytest -q"))
        self.assertTrue(policy.is_trusted("npm test -- --watch=false"))
        self.assertFalse(policy.is_trusted("pytest && rm -rf x"))
        self.assertFalse(policy.is_trusted("npm test > out.txt"))
        self.assertFalse(policy.is_trusted("rm -rf x"))

    def test_extra_prefixes_inactive_when_disabled(self) -> None:
        policy = ShellTrustPolicy(enabled=False, extra_prefixes=("pytest",))
        self.assertFalse(policy.is_trusted("pytest -q"))

    def test_env_flag_parsing(self) -> None:
        self.assertFalse(trust_enabled_from_env(None))
        self.assertFalse(trust_enabled_from_env("0"))
        self.assertFalse(trust_enabled_from_env("false"))
        self.assertTrue(trust_enabled_from_env("1"))
        self.assertTrue(trust_enabled_from_env("TRUE"))
        self.assertTrue(trust_enabled_from_env(" on "))


if __name__ == "__main__":
    unittest.main()
