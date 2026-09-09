from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider

from endless_task.runtime.cancellation import CancellationToken
from endless_task.skills import (
    SkillScope,
    build_available_skills_prompt,
    discover_skills,
)
from endless_task.storage import Database
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.skills.service import SkillService
from endless_task.tooling import ToolCall, ToolCallStatus
from endless_task.workspace_runtime.fs_tools import ReadSkillFileTool


def write_skill(root: Path, name: str, description: str = "示例技能") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n正文：{name}\n",
        encoding="utf-8",
    )
    return path


class SkillLoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.user = Path(self._temporary_directory.name) / "user"
        self.user.mkdir()
        self.workspace = Path(self._temporary_directory.name) / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_discovery_and_workspace_override(self) -> None:
        write_skill(self.user, "weekly-report", "用户级技能")
        write_skill(self.workspace / ".endless-task" / "skills", "weekly-report", "工作区覆盖")
        write_skill(self.workspace / ".endless-task" / "skills", "project-specific")

        skills = discover_skills(self.user, self.workspace / ".endless-task" / "skills")
        self.assertEqual(
            {skill.name for skill in skills},
            {"project-specific", "weekly-report"},
        )
        weekly = next(skill for skill in skills if skill.name == "weekly-report")
        self.assertEqual(weekly.scope, SkillScope.WORKSPACE)
        self.assertEqual(weekly.description, "工作区覆盖")

    def test_invalid_skill_is_skipped_from_injection(self) -> None:
        directory = self.user / "broken"
        directory.mkdir()
        (directory / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")
        write_skill(self.user, "valid")

        skills = discover_skills(self.user)
        self.assertEqual([skill.name for skill in skills], ["broken", "valid"])
        self.assertFalse(skills[0].valid)
        self.assertNotIn("broken", build_available_skills_prompt(skills))

    def test_prompt_contains_progressive_disclosure_fields(self) -> None:
        path = write_skill(self.user, "weekly-report", "写周报")
        skills = discover_skills(self.user)
        prompt = build_available_skills_prompt(skills)
        self.assertIn("<available_skills>", prompt)
        self.assertIn("read_skill_file", prompt)
        self.assertIn("<name>weekly-report</name>", prompt)
        self.assertIn("<description>写周报</description>", prompt)
        # S3：目录不再暴露宿主路径，正文也不进目录。
        self.assertNotIn(str(path), prompt)
        self.assertNotIn("<location>", prompt)
        self.assertNotIn("正文：", prompt)

    def test_catalog_never_exposes_locator_or_host_path(self) -> None:
        write_skill(self.user, "weekly-report", "写周报")
        skills = discover_skills(self.user)
        prompt = build_available_skills_prompt(skills, locator_mode=True)
        self.assertNotIn("skill://user/weekly-report", prompt)
        self.assertNotIn(str(self.user), prompt)


class ReadSkillFileToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name) / "skills"
        self.path = write_skill(self.root, "demo")

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    async def test_reads_only_inside_skill_roots(self) -> None:
        tool = ReadSkillFileTool(lambda conversation_id: (self.root,))
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"path": str(self.path)},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        result = await tool.execute(call, CancellationToken())
        self.assertIn("[来源：技能", result.content)
        self.assertIn("正文：demo", result.content)

    async def test_rejects_path_outside_roots(self) -> None:
        tool = ReadSkillFileTool(lambda conversation_id: (self.root,))
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"path": str(Path.home() / "secret.txt")},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        with self.assertRaises(Exception):
            await tool.execute(call, CancellationToken())

    async def test_resolves_locator_when_resolver_configured(self) -> None:
        tool = ReadSkillFileTool(
            lambda conversation_id: (self.root,),
            locator_resolver_provider=lambda conversation_id: (
                lambda locator: (
                    self.path if locator == "skill://user/demo" else None
                )
            ),
        )
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"locator": "skill://user/demo"},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        result = await tool.execute(call, CancellationToken())
        self.assertIn("正文：demo", result.content)
        self.assertEqual(
            str(self.path.resolve()),
            result.structured_content["path"],
        )

    async def test_locator_without_resolver_is_rejected(self) -> None:
        tool = ReadSkillFileTool(lambda conversation_id: (self.root,))
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"locator": "skill://user/demo"},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        from endless_task.tooling import ToolError

        with self.assertRaises(ToolError) as caught:
            await tool.execute(call, CancellationToken())
        self.assertEqual("locator_unavailable", caught.exception.code)

    async def test_unknown_locator_is_rejected(self) -> None:
        tool = ReadSkillFileTool(
            lambda conversation_id: (self.root,),
            locator_resolver_provider=lambda conversation_id: (
                lambda locator: None
            ),
        )
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"locator": "skill://user/missing"},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        from endless_task.tooling import ToolError

        with self.assertRaises(ToolError) as caught:
            await tool.execute(call, CancellationToken())
        self.assertEqual("skill_not_found", caught.exception.code)

    async def test_locator_outside_roots_is_rejected(self) -> None:
        tool = ReadSkillFileTool(
            lambda conversation_id: (self.root,),
            locator_resolver_provider=lambda conversation_id: (
                lambda locator: Path.home() / "secret.txt"
            ),
        )
        call = ToolCall(
            id="call",
            conversation_id="conversation",
            turn_id="turn",
            response_variant_id="variant",
            tool_name="read_skill_file",
            arguments={"locator": "skill://user/escape"},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-27T00:00:00.000Z",
        )
        from endless_task.tooling import ToolError

        # The same containment resolver that guards legacy paths rejects the
        # escape; its stable code is path_escape.
        with self.assertRaises(ToolError) as caught:
            await tool.execute(call, CancellationToken())
        self.assertEqual("path_escape", caught.exception.code)


class SkillOverrideRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(
            Path(self._temporary_directory.name) / "skills.db"
        )
        self.database.initialize()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_disabled_index_and_update(self) -> None:
        repository = SqliteSkillOverrideRepository(self.database)
        repository.set_disabled(
            scope="user", workspace_id="", name="demo", disabled=True
        )
        self.assertEqual(
            repository.disabled_index(), {("user", ""): {"demo"}}
        )
        repository.set_disabled(
            scope="user", workspace_id="", name="demo", disabled=False
        )
        self.assertEqual(repository.disabled_index(), {})


class SkillServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.user = self.root / "skills"
        self.user.mkdir()
        self.database = Database(self.root / "app.db")
        self.database.initialize()
        self.repository = SqliteSkillOverrideRepository(self.database)
        self.service = SkillService(
            user_dir=self.user,
            database_path=self.root / "app.db",
            override_repository=self.repository,
        )

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def test_disable_filters_visible_skills(self) -> None:
        write_skill(self.user, "demo")
        self.assertEqual(len(self.service.visible_skills()), 1)
        self.service.set_disabled(
            scope=SkillScope.USER, name="demo", disabled=True
        )
        self.assertEqual(self.service.visible_skills(), ())

    def test_workspace_disable_is_isolated(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        for root in (first, second):
            write_skill(root / ".endless-task" / "skills", "demo")
        self.assertEqual(
            len(self.service.visible_skills(first, workspace_id="w1")), 1
        )
        self.service.set_disabled(
            scope=SkillScope.WORKSPACE,
            name="demo",
            disabled=True,
            workspace_id="w1",
        )
        self.assertEqual(
            self.service.visible_skills(first, workspace_id="w1"), ()
        )
        self.assertEqual(
            len(self.service.visible_skills(second, workspace_id="w2")), 1
        )


if __name__ == "__main__":
    unittest.main()


class SkillApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_list_and_disable_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "skills" / "demo").mkdir(parents=True)
            (root / "skills" / "demo" / "SKILL.md").write_text(
                "---\nname: demo\ndescription: 演示\n---\n\n正文\n",
                encoding="utf-8",
            )
            app = create_app(
                settings=AppSettings(
                    database_path=root / "app.db",
                    memory_proposals_enabled=False,
                    knowledge_proposals_enabled=False,
                ),
                provider=FakeProvider(chunks=("ok",)),
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    listed = await client.get("/skills")
                    self.assertEqual(200, listed.status_code)
                    payload = listed.json()
                    self.assertEqual(1, len(payload["items"]))
                    self.assertEqual("demo", payload["items"][0]["name"])

                    patched = await client.patch(
                        "/skills/user/demo", json={"disabled": True}
                    )
                    self.assertEqual(200, patched.status_code)
                    listed = await client.get("/skills")
                    self.assertTrue(listed.json()["items"][0]["disabled"])
            finally:
                await lifespan.__aexit__(None, None, None)
