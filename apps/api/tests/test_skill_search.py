"""S6：本地技能检索（匹配排序 + API + 模型工具）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken
from endless_task.skills import rank_skills, tokenize
from endless_task.skills.service import SkillService
from endless_task.storage import Database
from endless_task.storage.sqlite_skill_override_repository import (
    SqliteSkillOverrideRepository,
)
from endless_task.storage.sqlite_skill_usage_repository import (
    SqliteSkillUsageRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.skill_search_tool import SkillSearchTool


def _write_skill(root: Path, name: str, description: str) -> None:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文\n",
        encoding="utf-8",
    )


class SearchRankingTest(unittest.TestCase):
    def test_tokenize_and_rank(self) -> None:
        # 中文展开成重叠二元组（整段匹配会漏掉"流程图"这类词）
        self.assertEqual(
            ("架构", "构图", "架构图", "diagram"), tokenize("架构图 Diagram")
        )
        self.assertEqual((), tokenize("   "))

        hits = rank_skills(
            [
                ("drawio-skill", "Create diagrams with drawio", "user", "shared"),
                ("archify", "architecture diagrams as HTML", "user", "shared"),
                ("weekly-report", "写周报", "user", "shared"),
            ],
            query="diagram",
        )
        self.assertEqual({"drawio-skill", "archify"}, {hit.name for hit in hits})
        self.assertNotIn("weekly-report", [hit.name for hit in hits])

    def test_exact_name_wins(self) -> None:
        hits = rank_skills(
            [
                ("report", "写周报", "user", "a"),
                ("report-plus", "写周报并汇总", "user", "a"),
            ],
            query="report",
        )
        self.assertEqual("report", hits[0].name)

    def test_chinese_query_with_verb_prefix_matches(self) -> None:
        hits = rank_skills(
            [
                ("creating-mermaid-diagrams", "创建 Mermaid 流程图与架构图", "user", "a"),
                ("weekly-report", "写周报", "user", "a"),
            ],
            query="画流程图",
        )
        self.assertEqual(["creating-mermaid-diagrams"], [hit.name for hit in hits])

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual((), rank_skills([("a", "b", "user", "c")], query="   "))


class SkillSearchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.user_dir = base / "app-skills"
        self.workspace = base / "ws"
        (self.workspace / ".endless-task" / "skills").mkdir(parents=True)
        database = Database(base / "skills.db")
        database.initialize()
        self.usage = SqliteSkillUsageRepository(database)
        self.service = SkillService(
            user_dir=self.user_dir,
            database_path=base / "app.db",
            override_repository=SqliteSkillOverrideRepository(database),
            usage_repository=self.usage,
            home_dir=base / "empty-home",
        )
        _write_skill(self.user_dir, "global-diagram", "画架构图（全局）")
        _write_skill(self.workspace / ".endless-task" / "skills", "proj-diagram", "工作区画图")
        _write_skill(self.user_dir, "weekly-report", "写周报")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_scope_filter(self) -> None:
        all_hits = self.service.search_skills(
            "diagram", self.workspace, workspace_id="ws_1", scope="all"
        )
        self.assertEqual(
            {"global-diagram", "proj-diagram"}, {hit.name for hit in all_hits}
        )
        global_only = self.service.search_skills(
            "diagram", self.workspace, workspace_id="ws_1", scope="global"
        )
        self.assertEqual(["global-diagram"], [hit.name for hit in global_only])
        workspace_only = self.service.search_skills(
            "diagram", self.workspace, workspace_id="ws_1", scope="workspace"
        )
        self.assertEqual(["proj-diagram"], [hit.name for hit in workspace_only])

    def test_search_records_usage(self) -> None:
        self.service.search_skills("diagram", self.workspace, workspace_id="ws_1")
        rows = self.usage.snapshot(scope="user", name="global-diagram")
        kinds = {row.counts.get("search_hit") for row in rows}
        self.assertIn(1, kinds)

    def test_invalid_scope_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.search_skills("x", None, scope="bogus")


class SkillSearchToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_tool_returns_candidates_and_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            user_dir = base / "skills"
            _write_skill(user_dir, "drawio-skill", "用 drawio 画图")
            service = SkillService(
                user_dir=user_dir,
                database_path=base / "app.db",
                override_repository=SqliteSkillOverrideRepository(
                    _database(base)
                ),
                usage_repository=SqliteSkillUsageRepository(_database(base, "u.db")),
                home_dir=base / "empty-home",
            )
            tool = SkillSearchTool(_NoBindingResolver(), service)

            def call(**arguments):
                return ToolCall(
                    id="call_search",
                    conversation_id="conv_1",
                    turn_id="turn_1",
                    response_variant_id="variant_1",
                    tool_name="skill_search",
                    arguments=arguments,
                    status=ToolCallStatus.CREATED,
                    created_at="2026-09-10T00:00:00.000Z",
                )

            result = await tool.execute(call(query="画图"), CancellationToken())
            self.assertIn("drawio-skill", result.content)
            self.assertIn("read_skill_file", result.content)
            self.assertEqual(1, len(result.structured_content["items"]))

            missing = await tool.execute(call(query="量子计算"), CancellationToken())
            self.assertIn("没有匹配", missing.content)

            with self.assertRaises(ToolError):
                await tool.execute(call(query="x", scope="bogus"), CancellationToken())


class SkillSearchEcosystemScopeTest(unittest.IsolatedAsyncioTestCase):
    """S8：Local 没有时用 scope=ecosystem 找生态技能（只发查询、不落盘）。"""

    def _tool(self, base: Path, searcher) -> SkillSearchTool:
        user_dir = base / "skills"
        _write_skill(user_dir, "local-only", "本地技能")
        service = SkillService(
            user_dir=user_dir,
            database_path=base / "app.db",
            override_repository=SqliteSkillOverrideRepository(_database(base)),
            usage_repository=SqliteSkillUsageRepository(_database(base, "u.db")),
            home_dir=base / "empty-home",
        )
        return SkillSearchTool(
            _NoBindingResolver(), service, ecosystem_search=searcher
        )

    def _call(self, **arguments) -> ToolCall:
        return ToolCall(
            id="call_search",
            conversation_id="conv_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            tool_name="skill_search",
            arguments=arguments,
            status=ToolCallStatus.CREATED,
            created_at="2026-09-10T00:00:00.000Z",
        )

    async def test_ecosystem_scope_returns_specs_and_install_hint(self) -> None:
        from endless_task.skills.ecosystem import EcosystemHit

        calls: list[tuple[str, int]] = []

        def fake_search(query: str, *, limit: int):
            calls.append((query, limit))

            class _Result:
                error = None
                hits = (
                    EcosystemHit(
                        spec="obra/superpowers@brainstorming",
                        owner="obra",
                        repo="superpowers",
                        skill="brainstorming",
                        installs="357.3K",
                        url="https://skills.sh/obra/superpowers/brainstorming",
                    ),
                )

            return _Result()

        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp), fake_search)
            result = await tool.execute(
                self._call(query="brainstorming", scope="ecosystem"),
                CancellationToken(),
            )
            self.assertEqual([("brainstorming", 8)], calls)
            self.assertIn("obra/superpowers@brainstorming", result.content)
            self.assertIn("357.3K", result.content)
            self.assertIn("skill_install", result.content)
            self.assertEqual("ecosystem", result.structured_content["scope"])
            self.assertEqual(
                "obra/superpowers@brainstorming",
                result.structured_content["items"][0]["spec"],
            )

    async def test_local_scope_never_touches_ecosystem(self) -> None:
        def exploding_search(query: str, *, limit: int):
            raise AssertionError("本地检索不应该联网")

        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp), exploding_search)
            result = await tool.execute(
                self._call(query="本地"), CancellationToken()
            )
            self.assertIn("local-only", result.content)

    async def test_ecosystem_failure_degrades_and_cools_down(self) -> None:
        attempts: list[str] = []

        def failing_search(query: str, *, limit: int):
            attempts.append(query)
            raise RuntimeError("npx 不存在")

        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp), failing_search)
            first = await tool.execute(
                self._call(query="podcast", scope="ecosystem"), CancellationToken()
            )
            self.assertIn("生态检索暂不可用", first.content)
            second = await tool.execute(
                self._call(query="podcast", scope="ecosystem"), CancellationToken()
            )
            self.assertIn("刚刚失败过", second.content)
            self.assertEqual(["podcast"], attempts)

    async def test_ecosystem_empty_result_suggests_other_keywords(self) -> None:
        def empty_search(query: str, *, limit: int):
            class _Result:
                error = None
                hits = ()

            return _Result()

        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(Path(tmp), empty_search)
            result = await tool.execute(
                self._call(query="量子计算", scope="ecosystem"), CancellationToken()
            )
            self.assertIn("生态里没有匹配", result.content)
            self.assertEqual([], result.structured_content["items"])


class _NoBindingResolver:
    def resolve_binding(self, conversation_id: str):
        del conversation_id
        return None


def _database(base: Path, name: str = "s.db") -> Database:
    database = Database(base / name)
    database.initialize()
    return database


class SkillSearchApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_search_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir(parents=True)
            user_dir = data / "skills"
            _write_skill(user_dir, "weekly-report", "写周报")
            _write_skill(user_dir, "diagram-tool", "画架构图")
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
                response = await client.post(
                    "/skills/search", json={"query": "画架构图"}
                )
                self.assertEqual(200, response.status_code, response.text)
                names = [item["name"] for item in response.json()["items"]]
                self.assertIn("diagram-tool", names)
                self.assertNotIn("weekly-report", names)

                empty = await client.post("/skills/search", json={"query": "zzz"})
                self.assertEqual([], empty.json()["items"])
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()


class SearchToolDescriptionTest(unittest.TestCase):
    """S6：工具描述必须告诉模型"中英文关键词都可以、中文查不到换英文"。"""

    def test_description_mentions_bilingual_retry(self) -> None:
        description = SkillSearchTool.definition.description
        self.assertIn("英文", description)
        self.assertIn("read_skill_file", description)
        self.assertIn("scope", description)
