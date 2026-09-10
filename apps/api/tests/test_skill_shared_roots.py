"""S4：技能发现支持本地共享目录（工作区 → 全局）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.skills import SkillScope
from endless_task.skills.loader import (
    SkillRootSpec,
    default_skill_root_specs,
    discover_from_roots,
)
from endless_task.skills.manifest import parse_skill_manifest


def _write_skill(root: Path, name: str, description: str = "") -> None:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description or name} 的说明\n---\n正文\n",
        encoding="utf-8",
    )


class SharedRootSpecsTest(unittest.TestCase):
    def test_default_specs_order_workspace_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = Path(tmp) / "ws"
            specs = default_skill_root_specs(
                user_dir=Path(tmp) / "app-skills",
                workspace_root=workspace,
                home=home,
            )
            labels = [spec.label for spec in specs]
            self.assertIn("~/.claude/skills", labels)
            self.assertIn("~/.agents/skills", labels)
            # 工作区根必须排在所有全局根之后（优先级更高）
            first_workspace = min(
                spec.rank for spec in specs if spec.scope is SkillScope.WORKSPACE
            )
            last_user = max(
                spec.rank for spec in specs if spec.scope is SkillScope.USER
            )
            self.assertGreater(first_workspace, last_user)

    def test_extra_dirs_from_env_are_included(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp) / "shared-skills"
            specs = default_skill_root_specs(
                user_dir=Path(tmp) / "app-skills",
                extra_dirs=(extra,),
                home=Path(tmp) / "home",
            )
            self.assertIn(str(extra), [spec.label for spec in specs])


class DiscoverFromRootsTest(unittest.TestCase):
    def test_higher_priority_root_overrides_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shared = base / "shared"
            app_dir = base / "app"
            workspace = base / "ws"
            _write_skill(shared, "review", "共享版")
            _write_skill(app_dir, "review", "应用版")
            _write_skill(workspace, "review", "工作区版")
            _write_skill(shared, "only-shared")

            skills = discover_from_roots(
                (
                    SkillRootSpec(shared, SkillScope.USER, "shared", 10),
                    SkillRootSpec(app_dir, SkillScope.USER, "app", 20),
                    SkillRootSpec(
                        workspace, SkillScope.WORKSPACE, "workspace", 30
                    ),
                )
            )
            by_name = {skill.name: skill for skill in skills}
            self.assertEqual({"only-shared", "review"}, set(by_name))
            self.assertEqual("工作区版 的说明", by_name["review"].description)
            self.assertEqual("workspace", by_name["review"].source)
            self.assertEqual("shared", by_name["only-shared"].source)

    def test_shared_skills_are_discoverable_without_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            claude = home / ".claude" / "skills"
            _write_skill(claude, "from-claude", "来自 Claude 共享目录")
            specs = default_skill_root_specs(
                user_dir=Path(tmp) / "app-skills", home=home
            )
            skills = discover_from_roots(specs)
            self.assertEqual(["from-claude"], [skill.name for skill in skills])
            self.assertEqual("~/.claude/skills", skills[0].source)


class RealWorldFrontmatterTest(unittest.TestCase):
    """共享目录里的真实技能 frontmatter 含嵌套块与列表，必须能解析。"""

    def test_nested_metadata_and_list_values(self) -> None:
        text = (
            "---\n"
            "name: journal-abbrev\n"
            "description: 期刊缩写查询\n"
            "allowed-tools: Bash, Read\n"
            "# 注释行\n"
            "metadata:\n"
            "  openclaw:\n"
            "    requires:\n"
            "      bins:\n"
            "        - python3\n"
            "---\n"
            "正文\n"
        )
        manifest = parse_skill_manifest(Path("pkg/SKILL.md"), text)
        self.assertTrue(manifest.valid, manifest.diagnostics)
        self.assertEqual("journal-abbrev", manifest.name)
        self.assertEqual("期刊缩写查询", manifest.description)

    def test_plain_key_fallback_still_works(self) -> None:
        # 含未被 YAML 接受的裸标签时回退逐行解析（历史兼容路径）
        text = "---\nname: legacy\ndescription: 旧格式\n---\n正文\n"
        manifest = parse_skill_manifest(Path("pkg/SKILL.md"), text)
        self.assertTrue(manifest.valid)
        self.assertEqual("legacy", manifest.name)


if __name__ == "__main__":
    unittest.main()


class SharedSkillManagementTest(unittest.IsolatedAsyncioTestCase):
    """S4：共享目录技能可读（统计/用例），但删除应被拒绝。"""

    async def test_usage_available_but_delete_refused_for_shared(self) -> None:
        import os

        import httpx

        from endless_task.api import AppSettings, create_app
        from endless_task.runtime import FakeProvider

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared = home / ".agents" / "skills"
            _write_skill(shared, "shared-skill", "共享技能")
            data = Path(tmp) / "data"
            data.mkdir(parents=True)
            previous = os.environ.get("ENDLESS_TASK_SKILL_HOME")
            os.environ["ENDLESS_TASK_SKILL_HOME"] = str(home)
            try:
                app = create_app(
                    settings=AppSettings(
                        database_path=data / "app.db",
                        memory_proposals_enabled=False,
                        knowledge_proposals_enabled=False,
                        artifact_proposals_enabled=False,
                        task_proposals_enabled=False,
                    ),
                    provider=FakeProvider(chunks=("ok",)),
                )
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                )
                try:
                    listed = await client.get("/skills")
                    names = [item["name"] for item in listed.json()["items"]]
                    self.assertEqual(["shared-skill"], names)
                    self.assertEqual(
                        "~/.agents/skills",
                        listed.json()["items"][0]["source"],
                    )

                    usage = await client.get("/skills/user/shared-skill/usage")
                    self.assertEqual(200, usage.status_code, usage.text)

                    denied = await client.delete("/skills/user/shared-skill")
                    self.assertEqual(400, denied.status_code)
                    self.assertEqual(
                        "skill_read_only", denied.json()["error"]["code"]
                    )
                    self.assertTrue((shared / "shared-skill" / "SKILL.md").is_file())
                finally:
                    await client.aclose()
                    await lifespan.__aexit__(None, None, None)
            finally:
                if previous is None:
                    os.environ.pop("ENDLESS_TASK_SKILL_HOME", None)
                else:
                    os.environ["ENDLESS_TASK_SKILL_HOME"] = previous


class CatalogBudgetTest(unittest.TestCase):
    """S4：技能很多时目录要有总预算（压描述 → 再截断并提示）。"""

    def _skill(self, index: int, description_length: int = 400):
        from endless_task.skills.models import Skill as SkillModel

        return SkillModel(
            name=f"skill-{index:03d}",
            description="x" * description_length,
            scope=SkillScope.USER,
            file_path=Path(f"/tmp/skills/skill-{index:03d}/SKILL.md"),
        )

    def test_large_catalog_is_bounded(self) -> None:
        from endless_task.skills.service import (
            MAX_CATALOG_CHARACTERS,
            build_available_skills_prompt,
        )

        skills = tuple(self._skill(index) for index in range(200))
        text = build_available_skills_prompt(skills)
        self.assertLessEqual(len(text), MAX_CATALOG_CHARACTERS)
        # 截断后必须告诉模型还有哪些技能可以显式调用
        self.assertIn("未列出", text)
        self.assertIn("/技能名", text)

    def test_small_catalog_keeps_full_descriptions(self) -> None:
        from endless_task.skills.service import build_available_skills_prompt

        text = build_available_skills_prompt((self._skill(0, 80),))
        self.assertNotIn("未列出", text)
        self.assertIn("<name>skill-000</name>", text)
