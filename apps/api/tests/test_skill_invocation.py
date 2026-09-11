"""S1 用户显式调用技能：解析、候选、注入与 API。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.skills import (
    INVOKE_DISABLED,
    Skill,
    INVOKE_NOT_USER_INVOCABLE,
    INVOKE_OK,
    INVOKE_UNKNOWN,
    MAX_SKILL_REQUESTS,
    SkillScope,
    SkillService,
    parse_skill_commands,
)
from endless_task.storage import Database
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)


class ParseSkillCommandsTest(unittest.TestCase):
    def test_matches_leading_and_spaced_commands(self) -> None:
        self.assertEqual(("my-skill",), parse_skill_commands("/my-skill 做点事"))
        self.assertEqual(
            ("a-skill", "b2"), parse_skill_commands("先 /a-skill 再 /b2 收尾")
        )

    def test_ignores_paths_and_prose(self) -> None:
        # 路径里 /Users、/tmp 后面紧跟 / 或非空白，不能当成技能名
        self.assertEqual((), parse_skill_commands("看看 /Users/example/x 和 /tmp/a"))
        self.assertEqual((), parse_skill_commands("3/4 的比例"))
        self.assertEqual((), parse_skill_commands("/My-Skill 大写不认"))

    def test_dedupes_and_caps(self) -> None:
        self.assertEqual(("a",), parse_skill_commands("/a /a /a"))
        content = " ".join(f"/skill-{index}" for index in range(6))
        self.assertEqual(MAX_SKILL_REQUESTS, len(parse_skill_commands(content)))


class SkillInvocationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.user_skills = self.data_dir / "skills"
        self.user_skills.mkdir(parents=True)
        self.database = Database(Path(self._tmp.name) / "skills.db")
        self.database.initialize()
        self.overrides = SqliteSkillOverrideRepository(self.database)
        self.service = SkillService(
            user_dir=self.user_skills,
            database_path=self.data_dir / "app.db",
            override_repository=self.overrides,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, frontmatter: str = "", body: str = "步骤一") -> None:
        directory = self.user_skills / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} 的说明\n{frontmatter}---\n{body}\n",
            encoding="utf-8",
        )

    def test_invocable_list_excludes_model_only_and_disabled(self) -> None:
        self._write("normal")
        self._write("model-only", "model-invocable: true\nuser-invocable: false\n")
        self._write("hidden")

        names = [skill.name for skill in self.service.invocable_skills()]
        self.assertEqual(["hidden", "normal"], names)

        self.service.set_disabled(scope=SkillScope.USER, name="hidden", disabled=True)
        names = [skill.name for skill in self.service.invocable_skills()]
        self.assertEqual(["normal"], names)

    def test_resolve_reports_reason(self) -> None:
        self._write("normal")
        self._write("model-only", "user-invocable: false\n")

        skill, reason = self.service.resolve_invocable("normal")
        self.assertEqual(INVOKE_OK, reason)
        self.assertIsNotNone(skill)

        self.assertEqual(
            INVOKE_NOT_USER_INVOCABLE, self.service.resolve_invocable("model-only")[1]
        )
        self.assertEqual(INVOKE_UNKNOWN, self.service.resolve_invocable("nope")[1])

        self.service.set_disabled(scope=SkillScope.USER, name="normal", disabled=True)
        self.assertEqual(INVOKE_DISABLED, self.service.resolve_invocable("normal")[1])

    def test_skill_body_is_bounded(self) -> None:
        self._write("big", body="x" * 5000)
        skill, _ = self.service.resolve_invocable("big")
        body = self.service.skill_body(skill, max_bytes=1024)
        self.assertLessEqual(len(body.encode("utf-8")), 1024 + 32)
        self.assertIn("已截断", body)


if __name__ == "__main__":
    unittest.main()


class ReadSkillFileToolSchemaTest(unittest.TestCase):
    """S1：未启用 locator 解析时，工具面不应推荐必然失败的 locator。"""

    def test_schema_follows_locator_availability(self) -> None:
        from endless_task.workspace_runtime.fs_tools import ReadSkillFileTool

        without = ReadSkillFileTool(lambda _: ())
        self.assertEqual(
            [{"required": ["name"]}, {"required": ["path"]}],
            without.definition.input_schema.get("anyOf"),
        )
        self.assertNotIn("locator", without.definition.input_schema["properties"])
        self.assertIn("name", without.definition.description)

        with_locator = ReadSkillFileTool(
            lambda _: (), locator_resolver_provider=lambda _: None
        )
        self.assertIn("locator", with_locator.definition.input_schema["properties"])


class SkillCatalogFormTest(unittest.TestCase):
    """S3：目录只给名称 + 截断描述，不给路径。"""

    def test_catalog_hides_paths_and_caps_description(self) -> None:
        from endless_task.skills.service import (
            MAX_CATALOG_DESCRIPTION_CHARACTERS,
            build_available_skills_prompt,
        )

        skill = Skill(
            name="review-notes",
            description="x" * 900,
            scope="user",
            file_path=Path("/Users/someone/secret/skills/review-notes/SKILL.md"),
        )
        prompt = build_available_skills_prompt((skill,))
        self.assertIn("<name>review-notes</name>", prompt)
        self.assertNotIn("/Users/someone", prompt)
        self.assertNotIn("<location>", prompt)
        self.assertNotIn("x" * (MAX_CATALOG_DESCRIPTION_CHARACTERS + 1), prompt)
        self.assertIn("…", prompt)
        self.assertIn("read_skill_file", prompt)

    def test_empty_catalog_returns_empty_string(self) -> None:
        from endless_task.skills.service import build_available_skills_prompt

        self.assertEqual("", build_available_skills_prompt(()))


class ReadSkillFileByNameTest(unittest.IsolatedAsyncioTestCase):
    """S3：read_skill_file 支持 name（目录不再暴露路径）。"""

    async def test_name_resolution_and_errors(self) -> None:
        from endless_task.runtime.cancellation import CancellationToken
        from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
        from endless_task.workspace_runtime.fs_tools import ReadSkillFileTool

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            (root / "demo").mkdir(parents=True)
            skill_file = root / "demo" / "SKILL.md"
            skill_file.write_text("---\nname: demo\ndescription: d\n---\n暗号 ORANGE-42\n", encoding="utf-8")

            tool = ReadSkillFileTool(
                lambda _: (root,),
                name_resolver_provider=lambda _: (
                    lambda name: skill_file if name == "demo" else None
                ),
            )

            def call(**arguments):
                return ToolCall(
                    id="call_skill",
                    conversation_id="conv_1",
                    turn_id="turn_1",
                    response_variant_id="variant_1",
                    tool_name="read_skill_file",
                    arguments=arguments,
                    status=ToolCallStatus.CREATED,
                    created_at="2026-09-09T00:00:00.000Z",
                )

            result = await tool.execute(
                call(name="demo"), CancellationToken()
            )
            self.assertIn("ORANGE-42", result.content)
            self.assertNotIn("<location>", result.content)

            with self.assertRaises(ToolError) as ctx:
                await tool.execute(call(name="missing"), CancellationToken())
            self.assertEqual("skill_not_found", ctx.exception.code)

            # 目录外路径仍被拒绝（name 解析结果也要过根包含性校验）
            outside = Path(tmp) / "outside.md"
            outside.write_text("secret", encoding="utf-8")
            escaping = ReadSkillFileTool(
                lambda _: (root,),
                name_resolver_provider=lambda _: (lambda name: outside),
            )
            with self.assertRaises(ToolError):
                await escaping.execute(call(name="demo"), CancellationToken())
