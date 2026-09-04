"""M5 SK-1a: skill:// locator + in-memory registry precedence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.skills.registry import (
    InMemorySkillRegistry,
    SkillLocator,
    SkillRevision,
    SkillRoot,
)


def write_skill(root: Path, name: str, *, body: str = "正文内容") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} 技能\n"
        "version: 1.0.0\n"
        "schema-version: 2\n"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


class SkillLocatorTest(unittest.TestCase):
    def test_format_and_parse_round_trip(self) -> None:
        locator = SkillLocator(scope="user", name="repo-review")
        self.assertEqual("skill://user/repo-review", str(locator))
        self.assertEqual(locator, SkillLocator.parse("skill://user/repo-review"))

    def test_parse_rejects_malformed(self) -> None:
        for bad in ("repo-review", "skill://user", "skill:///repo", "http://x/y"):
            with self.assertRaises(ValueError):
                SkillLocator.parse(bad)

    def test_invalid_scope_or_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SkillLocator(scope="root", name="x")
        with self.assertRaises(ValueError):
            SkillLocator(scope="user", name="Bad Name")

    def test_hashable_and_comparable(self) -> None:
        a = SkillLocator(scope="user", name="demo")
        b = SkillLocator(scope="user", name="demo")
        c = SkillLocator(scope="workspace", name="demo")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertNotEqual(a, c)
        self.assertEqual(2, len({a, b, c}))


class InMemorySkillRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_user_and_workspace_roots_discovered(self) -> None:
        user = self.root / "user"
        workspace = self.root / "ws" / ".endless-task" / "skills"
        write_skill(user, "weekly-report")
        write_skill(workspace, "project-specific")
        registry = InMemorySkillRegistry()
        report = registry.discover(
            (
                SkillRoot(user, "user", rank=2),
                SkillRoot(workspace, "workspace", rank=1),
            )
        )
        self.assertEqual(2, len(report.revisions))
        self.assertEqual(
            {"skill://user/weekly-report", "skill://workspace/project-specific"},
            {str(item.locator) for item in report.revisions},
        )

    def test_workspace_overrides_user_same_name(self) -> None:
        user = self.root / "user"
        workspace = self.root / "ws" / ".endless-task" / "skills"
        write_skill(user, "weekly-report", body="用户版")
        write_skill(workspace, "weekly-report", body="工作区版")
        registry = InMemorySkillRegistry()
        report = registry.discover(
            (
                SkillRoot(user, "user", rank=2),
                SkillRoot(workspace, "workspace", rank=1),
            )
        )
        revision = report.by_locator[
            SkillLocator(scope="workspace", name="weekly-report")
        ]
        self.assertEqual("工作区版", revision.body)
        self.assertEqual(1, len(report.revisions))
        # Higher-priority source wins; no digest conflict reported when the
        # lower-priority source is fully shadowed is NOT the rule - doc says
        # same name + different digest must be a visible diagnostic.
        self.assertEqual(1, len(report.conflicts))

    def test_same_name_same_digest_is_not_a_conflict(self) -> None:
        user = self.root / "user"
        workspace = self.root / "ws" / ".endless-task" / "skills"
        # Identical content => identical digest.
        text = (
            "---\nname: dup\ndescription: 相同技能\nversion: 1.0.0\n"
            "schema-version: 2\n---\n\nsame body\n"
        )
        for base, scope in ((user, "user"), (workspace, "workspace")):
            directory = base / "dup"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text(text, encoding="utf-8")
        registry = InMemorySkillRegistry()
        report = registry.discover(
            (
                SkillRoot(user, "user", rank=2),
                SkillRoot(workspace, "workspace", rank=1),
            )
        )
        self.assertEqual(0, len(report.conflicts))

    def test_resolve_by_locator(self) -> None:
        user = self.root / "user"
        write_skill(user, "demo")
        registry = InMemorySkillRegistry()
        registry.discover((SkillRoot(user, "user", rank=2),))
        revision = registry.resolve(SkillLocator(scope="user", name="demo"))
        self.assertIsInstance(revision, SkillRevision)
        self.assertEqual("1.0.0", revision.version)
        self.assertEqual(64, len(revision.digest))
        self.assertIsNone(
            registry.resolve(SkillLocator(scope="user", name="missing"))
        )

    def test_bundled_root_has_lowest_rank(self) -> None:
        user = self.root / "user"
        bundled = self.root / "bundled"
        write_skill(user, "common", body="用户覆盖")
        write_skill(bundled, "common", body="打包默认")
        registry = InMemorySkillRegistry()
        report = registry.discover(
            (
                SkillRoot(user, "user", rank=2),
                SkillRoot(bundled, "bundled", rank=3),
            )
        )
        revision = report.by_locator[SkillLocator(scope="user", name="common")]
        self.assertEqual("用户覆盖", revision.body)


if __name__ == "__main__":
    unittest.main()
