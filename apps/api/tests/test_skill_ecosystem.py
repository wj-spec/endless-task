"""S8：生态技能检索、安装与权限门禁。"""

from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Optional

import httpx

from endless_task.api import AppSettings, create_app
from endless_task.runtime import FakeProvider
from endless_task.runtime.cancellation import CancellationToken
from endless_task.skills.ecosystem import (
    _locate_package,
    describe_repo_skills,
    download_skill_package,
    normalize_ecosystem_package,
    parse_find_output,
    search_ecosystem,
    validspec,
)
from endless_task.skills.manifest import parse_skill_manifest
from endless_task.storage import Database
from endless_task.storage.sqlite_skill_provenance_repository import (
    SqliteSkillProvenanceRepository,
)
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.skill_install_tool import (
    SkillInstallTool,
    SkillInstallTrustPolicy,
)

FIND_OUTPUT = """
\u001b[38;5;145mvercel-labs/agent-skills@vercel-react-best-practices\u001b[0m \u001b[36m701.7K installs\u001b[0m
\u001b[38;5;102m└ https://skills.sh/vercel-labs/agent-skills/vercel-react-best-practices\u001b[0m

\u001b[38;5;145mgoogle-labs-code/stitch-skills@react:components\u001b[0m \u001b[36m50.7K installs\u001b[0m
\u001b[38;5;102m└ https://skills.sh/google-labs-code/stitch-skills/react:components\u001b[0m
"""


def _tarball(files: dict[str, str], *, prefix: str = "repo-main") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            payload = content.encode("utf-8")
            info = tarfile.TarInfo(name=f"{prefix}/{name}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class EcosystemParsingTest(unittest.TestCase):
    def test_parse_find_output_strips_ansi_and_extracts_fields(self) -> None:
        hits = parse_find_output(FIND_OUTPUT)
        self.assertEqual(2, len(hits))
        first = hits[0]
        self.assertEqual("vercel-labs/agent-skills@vercel-react-best-practices", first.spec)
        self.assertEqual("vercel-labs", first.owner)
        self.assertEqual("701.7K", first.installs)
        self.assertTrue(first.url.startswith("https://skills.sh/"))
        self.assertEqual("react:components", hits[1].skill)

    def test_search_uses_injected_runner(self) -> None:
        result = search_ecosystem("react", runner=lambda argv: FIND_OUTPUT)
        self.assertEqual(2, len(result.hits))
        self.assertEqual("", result.error)

    def test_search_failure_is_reported_not_raised(self) -> None:
        def boom(argv):
            raise RuntimeError("network down")

        result = search_ecosystem("react", runner=boom)
        self.assertEqual((), result.hits)
        self.assertIn("失败", result.error)

    def test_empty_query(self) -> None:
        self.assertIn("不能为空", search_ecosystem("   ").error)

    def test_spec_validation(self) -> None:
        self.assertIsNotNone(validspec("owner/repo@skill"))
        self.assertIsNotNone(validspec("owner/repo"))
        for bad in ("", "../etc/passwd", "owner/repo --flag", "-rf", "owner", "a b/c"):
            self.assertIsNone(validspec(bad), bad)


class DownloadTest(unittest.TestCase):
    def test_download_extracts_named_skill(self) -> None:
        payload = _tarball(
            {
                "my-skill/SKILL.md": "---\nname: my-skill\ndescription: d\n---\n正文\n",
                "README.md": "# repo",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            package = download_skill_package(
                "owner/repo@my-skill",
                Path(tmp),
                downloader=lambda url: payload,
            )
            self.assertTrue((package / "SKILL.md").is_file())
            self.assertEqual("my-skill", package.name)

    def test_rejects_archive_with_traversal(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            payload = b"x"
            info = tarfile.TarInfo(name="../escape/SKILL.md")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                download_skill_package(
                    "owner/repo", Path(tmp), downloader=lambda url: buffer.getvalue()
                )

    def test_missing_skill_dir_is_reported(self) -> None:
        payload = _tarball({"README.md": "# nothing"})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                download_skill_package(
                    "owner/repo@nope", Path(tmp), downloader=lambda url: payload
                )


class LocatePackageTest(unittest.TestCase):
    """``@<skill>`` 是技能名而不是目录名：先目录名、再 frontmatter name 匹配。"""

    def _repo(self, root: Path, files: dict[str, str]) -> Path:
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return root

    def test_matches_frontmatter_name_when_directory_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                {
                    "skills/react-best-practices/SKILL.md": (
                        "---\nname: vercel-react-best-practices\ndescription: 最佳实践\n"
                        "---\n\nbody\n"
                    ),
                    "skills/composition-patterns/SKILL.md": (
                        "---\nname: composition-patterns\ndescription: 组合\n---\n\nbody\n"
                    ),
                },
            )
            package = _locate_package(repo, skill="vercel-react-best-practices")
            self.assertIsNotNone(package)
            self.assertEqual(("skills", "react-best-practices"), package.parts[-2:])

    def test_matches_directory_name_with_colon_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                {
                    "react/components/SKILL.md": (
                        "---\nname: react-components\ndescription: 组件\n---\n\nbody\n"
                    )
                },
            )
            package = _locate_package(repo, skill="react:components")
            self.assertIsNotNone(package)
            self.assertEqual("components", package.name)

    def test_single_skill_repo_resolves_without_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                {
                    "skills/only-one/SKILL.md": (
                        "---\nname: only-one\ndescription: 唯一\n---\n\nbody\n"
                    )
                },
            )
            package = _locate_package(repo, skill="")
            self.assertIsNotNone(package)
            self.assertEqual("only-one", package.name)

    def test_ambiguous_repo_without_name_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                {
                    "skills/a/SKILL.md": "---\nname: a\ndescription: A\n---\n\nbody\n",
                    "skills/b/SKILL.md": "---\nname: b\ndescription: B\n---\n\nbody\n",
                },
            )
            self.assertIsNone(_locate_package(repo, skill=""))
            self.assertEqual(["a", "b"], describe_repo_skills(repo))

    def test_dependency_directories_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                {
                    "skills/real/SKILL.md": "---\nname: real\ndescription: 真\n---\n\nbody\n",
                    "node_modules/pkg/SKILL.md": (
                        "---\nname: real\ndescription: 依赖里的干扰项\n---\n\nbody\n"
                    ),
                    ".git/hooks/SKILL.md": "---\nname: real\ndescription: git\n---\n\nbody\n",
                },
            )
            package = _locate_package(repo, skill="real")
            self.assertIsNotNone(package)
            self.assertEqual(("skills", "real"), package.parts[-2:])


class NormalizeEcosystemPackageTest(unittest.TestCase):
    """生态技能多为 v1（只有 name/description），安装前必须规整成 v2。"""

    def _package(self, root: Path, text: str, name: str = "eco") -> Path:
        package = root / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(text, encoding="utf-8")
        return package

    def test_minimal_v1_package_becomes_valid_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = self._package(
                Path(tmp),
                "---\nname: brainstorming\ndescription: 头脑风暴\n---\n\n正文\n",
            )
            changed, folded = normalize_ecosystem_package(package)
            self.assertTrue(changed)
            self.assertEqual((), folded)
            text = (package / "SKILL.md").read_text(encoding="utf-8")
            manifest = parse_skill_manifest(package / "SKILL.md", text)
            self.assertTrue(manifest.valid, manifest.diagnostics)
            self.assertEqual(2, manifest.schema_version)
            self.assertEqual("brainstorming", manifest.name)
            self.assertIn("正文", manifest.body)
            # 上游原文摘要留在 metadata，可追溯
            self.assertIn("upstream-digest", text)

    def test_unknown_keys_and_v1_visibility_flags_are_adapted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = self._package(
                Path(tmp),
                "---\n"
                "name: tldraw\n"
                "description: 画图\n"
                "allowed-tools: Read, Write\n"
                "disable-model-invocation: true\n"
                "---\n\nbody\n",
            )
            changed, folded = normalize_ecosystem_package(package)
            self.assertTrue(changed)
            self.assertEqual(("allowed-tools",), folded)
            text = (package / "SKILL.md").read_text(encoding="utf-8")
            manifest = parse_skill_manifest(package / "SKILL.md", text)
            self.assertTrue(manifest.valid, manifest.diagnostics)
            self.assertFalse(manifest.model_invocable)
            self.assertIn("allowed-tools", text)

    def test_already_v2_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = (
                "---\nname: eco\ndescription: 生态\nversion: 2.1.0\n"
                "schema-version: 2\n---\n\nbody\n"
            )
            package = self._package(Path(tmp), original)
            changed, folded = normalize_ecosystem_package(package)
            self.assertFalse(changed)
            self.assertEqual((), folded)
            self.assertEqual(
                original, (package / "SKILL.md").read_text(encoding="utf-8")
            )

    def test_idempotent_and_overlong_description_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package = self._package(
                Path(tmp),
                "---\nname: long-desc\ndescription: "
                + "长" * 1500
                + "\n---\n\nbody\n",
            )
            normalize_ecosystem_package(package)
            self.assertEqual(
                (False, ()), normalize_ecosystem_package(package)
            )
            text = (package / "SKILL.md").read_text(encoding="utf-8")
            manifest = parse_skill_manifest(package / "SKILL.md", text)
            self.assertTrue(manifest.valid, manifest.diagnostics)
            self.assertLessEqual(len(manifest.description), 1024)

    def test_missing_frontmatter_or_description_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            no_frontmatter = self._package(
                Path(tmp), "# 只有正文\n", name="a"
            )
            with self.assertRaises(ValueError):
                normalize_ecosystem_package(no_frontmatter)
            no_description = self._package(
                Path(tmp), "---\nname: b\n---\n\nbody\n", name="b"
            )
            with self.assertRaises(ValueError):
                normalize_ecosystem_package(no_description)


class InstallToolTest(unittest.IsolatedAsyncioTestCase):
    def _tool(
        self,
        *,
        mode: str,
        base: Path,
        target_root: Optional[Path] = None,
        shared_root: Optional[Path] = None,
    ) -> SkillInstallTool:
        database = Database(base / "prov.db")
        database.initialize()
        tool = SkillInstallTool(
            _ServiceStub(base / "skills", shared_root=shared_root),
            SqliteSkillProvenanceRepository(database),
            mode=mode,
            target_root=target_root,
        )
        return tool

    def _call(self, **arguments) -> ToolCall:
        return ToolCall(
            id="call_install",
            conversation_id="conv_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            tool_name="skill_install",
            arguments=arguments,
            status=ToolCallStatus.CREATED,
            created_at="2026-09-10T00:00:00.000Z",
        )

    async def test_deny_mode_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(mode="deny", base=Path(tmp))
            with self.assertRaises(ToolError) as ctx:
                await tool.execute(self._call(source="owner/repo"), CancellationToken())
            self.assertEqual("skill_install_disabled", ctx.exception.code)
            self.assertTrue(tool.requires_explicit_confirmation(self._call(source="o/r")))

    def test_allow_mode_waives_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(mode="allow", base=Path(tmp))
            self.assertFalse(
                tool.requires_explicit_confirmation(self._call(source="o/r"))
            )
            policy = SkillInstallTrustPolicy(mode="allow")
            self.assertTrue(policy.allows("skill_install"))
            self.assertFalse(policy.allows("run_shell"))
            self.assertFalse(
                SkillInstallTrustPolicy(mode="ask").allows("skill_install")
            )

    async def test_tool_install_flow_with_injected_download(self) -> None:
        from unittest import mock

        payload = _tarball(
            {
                "demo-skill/SKILL.md": (
                    "---\nname: demo-skill\ndescription: 演示\nversion: 1.0.0\n"
                    "schema-version: 2\n---\n正文\n"
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            tool = self._tool(mode="allow", base=Path(tmp))
            with mock.patch(
                "endless_task.skills.ecosystem._download_tarball",
                return_value=payload,
            ):
                result = await tool.execute(
                    self._call(source="owner/repo@demo-skill"), CancellationToken()
                )
            self.assertIn("已安装技能 demo-skill", result.content)
            self.assertEqual("demo-skill", result.structured_content["name"])
            self.assertIsNotNone(result.structured_content["provenance"])
            installed = Path(tmp) / "skills" / "demo-skill" / "SKILL.md"
            self.assertTrue(installed.is_file())


    async def test_install_targets_given_root_not_first_shared_root(self) -> None:
        """共享目录（~/.claude/skills 等）在 S4 里只读，安装必须落到应用可写目录。"""
        from unittest import mock

        payload = _tarball(
            {
                "demo-skill/SKILL.md": (
                    "---\nname: demo-skill\ndescription: 演示\n---\n正文\n"
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shared = base / "shared" / "claude" / "skills"
            target = base / "app" / "skills"
            tool = self._tool(
                mode="allow", base=base, target_root=target, shared_root=shared
            )
            prompt = tool.approval_prompt(self._call(source="owner/repo@demo-skill"))
            self.assertIn(str(target), prompt.reason)
            with mock.patch(
                "endless_task.skills.ecosystem._download_tarball",
                return_value=payload,
            ):
                result = await tool.execute(
                    self._call(source="owner/repo@demo-skill"), CancellationToken()
                )
            self.assertTrue((target / "demo-skill" / "SKILL.md").is_file())
            self.assertFalse(shared.exists())
            self.assertIn("规整为 v2", result.content)
            provenance = result.structured_content["provenance"]
            self.assertEqual("owner/repo@demo-skill", provenance["spec"])
            self.assertEqual("user", provenance["scope"])
            self.assertTrue(result.structured_content["normalized"])


class _ServiceStub:
    """``skill_roots()[0]`` 故意指向"共享目录"，用来验证工具不往那儿装。"""

    def __init__(self, root: Path, *, shared_root: Optional[Path] = None) -> None:
        self._root = root
        self._shared_root = shared_root if shared_root is not None else root

    def skill_roots(self, workspace_root=None):
        del workspace_root
        return (self._shared_root, self._root)


class InstallApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_install_endpoint_and_permissions(self) -> None:
        from unittest import mock

        payload = _tarball(
            {
                "eco-skill/SKILL.md": (
                    "---\nname: eco-skill\ndescription: 生态技能\nversion: 1.0.0\n"
                    "schema-version: 2\n---\n正文\n"
                )
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir(parents=True)
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
                with mock.patch(
                    "endless_task.skills.ecosystem._download_tarball",
                    return_value=payload,
                ):
                    response = await client.post(
                        "/skills/install", json={"source": "owner/repo@eco-skill"}
                    )
                self.assertEqual(201, response.status_code, response.text)
                self.assertEqual("eco-skill", response.json()["skill"]["name"])
                self.assertIn("provenance", response.json())

                listed = (await client.get("/skills")).json()["items"]
                entry = [item for item in listed if item["name"] == "eco-skill"][0]
                self.assertEqual("owner/repo", entry["provenance"]["source"])

                bad = await client.post(
                    "/skills/install", json={"source": "../etc/passwd"}
                )
                self.assertEqual(400, bad.status_code)
                self.assertEqual(
                    "skill_install_failed", bad.json()["error"]["code"]
                )
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)

    async def test_deny_mode_blocks_api_install(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir(parents=True)
            with mock.patch.dict(
                os.environ, {"ENDLESS_TASK_SKILL_INSTALL": "deny"}, clear=False
            ):
                app = create_app(
                    settings=AppSettings(
                        database_path=data / "app.db",
                        skill_install_mode="deny",
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
                    "/skills/install", json={"source": "owner/repo@x"}
                )
                self.assertEqual(403, response.status_code)
                self.assertEqual(
                    "skill_install_disabled", response.json()["error"]["code"]
                )
                definitions = app.state.container.tool_registry.definitions()
                self.assertNotIn(
                    "skill_install", [item.name for item in definitions]
                )
            finally:
                await client.aclose()
                await lifespan.__aexit__(None, None, None)


class EnvironmentSettingsApiTest(unittest.IsolatedAsyncioTestCase):
    """回归：真实启动路径是 ``create_app()``（无显式 settings）。

    路由里若误用被遮蔽的参数 ``settings``（默认 None），只有真实启动才会 500，
    显式传 settings 的单测发现不了 —— 这组用例就走环境变量路径。
    """

    async def test_routes_work_when_settings_come_from_environment(self) -> None:
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir(parents=True)
            environment = {
                "ENDLESS_TASK_DATA_DIR": str(data),
                "ENDLESS_TASK_DB_PATH": str(data / "app.db"),
                "ENDLESS_TASK_SKILL_INSTALL": "deny",
                "ENDLESS_TASK_MEMORY_PROPOSALS": "0",
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                app = create_app(provider=FakeProvider(chunks=("ok",)))
                lifespan = app.router.lifespan_context(app)
                await lifespan.__aenter__()
                client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                )
                try:
                    packages = await client.get("/api/v2/skills/packages")
                    self.assertEqual(200, packages.status_code, packages.text)
                    skills = await client.get("/skills")
                    self.assertEqual(200, skills.status_code, skills.text)
                    installed = await client.post(
                        "/skills/install", json={"source": "owner/repo@x"}
                    )
                    self.assertEqual(403, installed.status_code, installed.text)
                    self.assertEqual(
                        "skill_install_disabled",
                        installed.json()["error"]["code"],
                    )
                finally:
                    await client.aclose()
                    await lifespan.__aexit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
