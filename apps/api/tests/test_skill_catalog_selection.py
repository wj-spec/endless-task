"""S5：默认技能目录的精选规则与预算。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from endless_task.skills import Skill, SkillScope
from endless_task.skills.service import (
    DEFAULT_CATALOG_BUDGET_CHARACTERS,
    DEFAULT_CATALOG_LIMIT,
    DEFAULT_CATALOG_SUMMARY_CHARACTERS,
    SkillService,
    build_available_skills_prompt,
)
from endless_task.storage import Database
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.storage.sqlite_skill_usage_repository import (
    SqliteSkillUsageRepository,
)


def _write_skill(root: Path, name: str, description: str | None = None) -> None:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name}\n---\n正文\n",
        encoding="utf-8",
    )


class CatalogSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user_dir = base / "app-skills"
        self.workspace = base / "ws"
        (self.workspace / ".endless-task" / "skills").mkdir(parents=True)
        self.database = Database(base / "skills.db")
        self.database.initialize()
        self.overrides = SqliteSkillOverrideRepository(self.database)
        self.usage = SqliteSkillUsageRepository(self.database)
        self.service = SkillService(
            user_dir=self.user_dir,
            database_path=base / "app.db",
            override_repository=self.overrides,
            usage_repository=self.usage,
            home_dir=base / "empty-home",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _workspace_skill(self, name: str) -> None:
        _write_skill(self.workspace / ".endless-task" / "skills", name)

    def test_workspace_skills_come_first(self) -> None:
        for index in range(12):
            _write_skill(self.user_dir, f"global-{index:02d}")
        self._workspace_skill("project-a")
        self._workspace_skill("project-b")

        selected, folded = self.service.catalog_skills(
            self.workspace, workspace_id="ws_1", limit=4
        )
        names = [skill.name for skill in selected]
        self.assertEqual(["project-a", "project-b"], names[:2])
        self.assertEqual(4, len(selected))
        self.assertEqual(10, folded)

    def test_recent_usage_beats_alphabetical(self) -> None:
        for name in ("aaa", "bbb", "ccc"):
            _write_skill(self.user_dir, name)
        self.usage.record(scope="user", name="ccc", digest="d", kind="invoked")

        selected, _folded = self.service.catalog_skills(workspace_id="", limit=1)
        self.assertEqual(["ccc"], [skill.name for skill in selected])

    def test_stale_usage_does_not_count(self) -> None:
        _write_skill(self.user_dir, "zzz")
        _write_skill(self.user_dir, "aaa")
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO skill_usage(scope, name, digest, kind, count, last_at) "
                "VALUES ('user', 'zzz', 'd', 'invoked', 9, ?)",
                (old,),
            )
        selected, _ = self.service.catalog_skills(workspace_id="", limit=1)
        self.assertEqual(["aaa"], [skill.name for skill in selected])

    def test_pinned_skills_are_included(self) -> None:
        for name in ("aaa", "mmm", "zzz"):
            _write_skill(self.user_dir, name)
        self.service.set_pinned(scope=SkillScope.USER, name="zzz", pinned=True)

        selected, _ = self.service.catalog_skills(workspace_id="", limit=1)
        self.assertEqual(["zzz"], [skill.name for skill in selected])

    def test_model_only_skills_never_enter_catalog(self) -> None:
        package = self.user_dir / "user-only"
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(
            "---\nname: user-only\ndescription: 只能手动调用\n"
            "disable-model-invocation: true\n---\n正文\n",
            encoding="utf-8",
        )
        _write_skill(self.user_dir, "normal")
        selected, _ = self.service.catalog_skills(workspace_id="", limit=8)
        self.assertEqual(["normal"], [skill.name for skill in selected])


class CatalogRenderTest(unittest.TestCase):
    def _skill(self, name: str, description: str = "") -> Skill:
        return Skill(
            name=name,
            description=description or f"{name} 的说明",
            scope=SkillScope.USER,
            file_path=Path(f"/tmp/skills/{name}/SKILL.md"),
        )

    def test_folded_skills_produce_awareness_block(self) -> None:
        skills = tuple(self._skill(f"skill-{index}") for index in range(3))
        text = build_available_skills_prompt(skills, folded=38)
        self.assertIn("本地还有 38 个技能未列出", text)
        self.assertIn("skill_search", text)
        self.assertIn("read_skill_file", text)
        self.assertIn("/技能名", text)

    def test_no_awareness_block_without_folded(self) -> None:
        text = build_available_skills_prompt((self._skill("only"),))
        self.assertNotIn("未列出", text)

    def test_summary_is_truncated(self) -> None:
        text = build_available_skills_prompt(
            (self._skill("long", "x" * 500),)
        )
        self.assertIn("x" * DEFAULT_CATALOG_SUMMARY_CHARACTERS + "…", text)

    def test_budget_is_respected(self) -> None:
        skills = tuple(
            self._skill(f"skill-{index:02d}", "y" * 400) for index in range(40)
        )
        text = build_available_skills_prompt(
            skills, folded=0, budget=DEFAULT_CATALOG_BUDGET_CHARACTERS
        )
        # 未折叠时不做预算裁剪，仅检查默认精选路径（8 条）在预算内
        selected = skills[:DEFAULT_CATALOG_LIMIT]
        trimmed = build_available_skills_prompt(
            selected,
            folded=len(skills) - len(selected),
            budget=DEFAULT_CATALOG_BUDGET_CHARACTERS,
        )
        self.assertLessEqual(len(trimmed), DEFAULT_CATALOG_BUDGET_CHARACTERS)
        self.assertIn("未列出", trimmed)
        del text


if __name__ == "__main__":
    unittest.main()


class CatalogAlwaysIncludedTest(unittest.TestCase):
    """固定的技能不被 limit 挤占（否则"固定"没有意义）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user_dir = base / "app-skills"
        self.database = Database(base / "skills.db")
        self.database.initialize()
        self.overrides = SqliteSkillOverrideRepository(self.database)
        self.service = SkillService(
            user_dir=self.user_dir,
            database_path=base / "app.db",
            override_repository=self.overrides,
            usage_repository=SqliteSkillUsageRepository(self.database),
            home_dir=base / "empty-home",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_pinned_skills_exceed_limit_within_hard_cap(self) -> None:
        for index in range(12):
            _write_skill(self.user_dir, f"skill-{index:02d}")
        for index in range(3):
            self.service.set_pinned(
                scope=SkillScope.USER, name=f"skill-{index:02d}", pinned=True
            )

        selected, folded = self.service.catalog_skills(workspace_id="", limit=2)
        names = {skill.name for skill in selected}
        # 3 个固定项都在，另有 2 个按 limit 填充的槽位不可用（always 已超 limit）
        self.assertEqual({"skill-00", "skill-01", "skill-02"}, names)
        self.assertEqual(9, folded)

    def test_workspace_skills_are_never_evicted(self) -> None:
        workspace_skills = Path(self._tmp.name) / "ws" / ".endless-task" / "skills"
        workspace_skills.mkdir(parents=True)
        for index in range(3):
            _write_skill(workspace_skills, f"proj-{index}")
        for index in range(9):
            _write_skill(self.user_dir, f"global-{index:02d}")

        selected, folded = self.service.catalog_skills(
            Path(self._tmp.name) / "ws", workspace_id="ws_1", limit=3
        )
        names = [skill.name for skill in selected]
        self.assertEqual(["proj-0", "proj-1", "proj-2"], names)
        self.assertEqual(9, folded)
