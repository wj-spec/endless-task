"""M5 SK-0: SKILL.md manifest v2 parsing + v1 compatibility."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.skills import SkillScope, discover_skills
from endless_task.skills.manifest import (
    SKILL_MANIFEST_V2_SCHEMA,
    SUPPORTED_SCHEMA_VERSION,
    SkillManifest,
    parse_skill_manifest,
)


def write_v2(
    directory: Path,
    name: str,
    *,
    version: str = "1.2.0",
    extra: str = "",
    body: str = "正文内容",
) -> Path:
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {name} 技能说明\n"
        f"version: {version}\n"
        "schema-version: 2\n"
        f"model-invocable: true\n"
        f"user-invocable: true\n"
        f"required-tools:\n  - read_workspace_file\n"
        f"required-capabilities:\n  - workspace.read\n"
        f"{extra}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def write_v1(directory: Path, name: str) -> Path:
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} 技能\n---\n\n正文：{name}\n",
        encoding="utf-8",
    )
    return path


class SkillManifestV2Test(unittest.TestCase):
    def test_v2_manifest_parses_all_declared_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_v2(
                Path(directory),
                "repo-review",
                extra=(
                    "optional-tools:\n  - run_shell\n"
                    "conflicts-with:\n  - other-skill\n"
                    "resource-policy: package-only\n"
                ),
            )
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertTrue(manifest.valid, manifest.diagnostics)
            self.assertEqual(2, manifest.schema_version)
            self.assertEqual("1.2.0", manifest.version)
            self.assertEqual("repo-review", manifest.name)
            self.assertTrue(manifest.model_invocable)
            self.assertTrue(manifest.user_invocable)
            self.assertEqual(("read_workspace_file",), manifest.required_tools)
            self.assertEqual(("workspace.read",), manifest.required_capabilities)
            self.assertEqual(("run_shell",), manifest.optional_tools)
            self.assertEqual(("other-skill",), manifest.conflicts_with)
            self.assertEqual("package-only", manifest.resource_policy)
            self.assertEqual("正文内容", manifest.body)
            self.assertEqual(64, len(manifest.digest))

    def test_v2_invalid_manifest_gets_stable_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_v2(
                Path(directory),
                "bad-version",
                version="not-semver",
            )
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertFalse(manifest.valid)
            self.assertTrue(
                any(d.code == "invalid_manifest" for d in manifest.diagnostics)
            )

    def test_v2_unknown_schema_version_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "future" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "name: future\n"
                "description: x\n"
                "version: 1.0.0\n"
                "schema-version: 3\n"
                "---\n\nbody\n",
                encoding="utf-8",
            )
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertFalse(manifest.valid)
            self.assertTrue(
                any(d.code == "invalid_manifest" for d in manifest.diagnostics)
            )

    def test_v2_manifest_without_declared_requirements_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "plain" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "name: plain\n"
                "description: 无依赖技能\n"
                "version: 1.0.0\n"
                "schema-version: 2\n"
                "---\n\nbody\n",
                encoding="utf-8",
            )
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertTrue(manifest.valid, manifest.diagnostics)
            self.assertEqual((), manifest.required_tools)
            self.assertEqual((), manifest.required_capabilities)

    def test_schema_is_draft_2020_12_valid(self) -> None:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(SKILL_MANIFEST_V2_SCHEMA)
        self.assertEqual(2, SUPPORTED_SCHEMA_VERSION)


class SkillManifestV1CompatTest(unittest.TestCase):
    def test_v1_manifest_adapted_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_v1(Path(directory), "weekly-report")
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertTrue(manifest.valid)
            self.assertEqual(1, manifest.schema_version)
            self.assertEqual("0.0.0", manifest.version)
            self.assertEqual("weekly-report", manifest.name)
            self.assertTrue(manifest.model_invocable)
            self.assertEqual((), manifest.required_tools)
            self.assertEqual(64, len(manifest.digest))

    def test_v1_disable_model_invocation_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "manual" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text(
                "---\n"
                "name: manual\n"
                "description: 手动技能\n"
                "disable-model-invocation: true\n"
                "---\n\n正文\n",
                encoding="utf-8",
            )
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertFalse(manifest.model_invocable)

    def test_discovery_still_works_over_mixed_v1_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_v2(root, "v2-skill")
            write_v1(root, "v1-skill")
            skills = discover_skills(root)
            self.assertEqual(
                {skill.name for skill in skills},
                {"v1-skill", "v2-skill"},
            )
            self.assertTrue(all(skill.valid for skill in skills))

    def test_v1_invalid_skill_keeps_historical_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "broken" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_text("没有 frontmatter\n", encoding="utf-8")
            manifest = parse_skill_manifest(path, path.read_text(encoding="utf-8"))
            self.assertFalse(manifest.valid)
            self.assertTrue(
                any(d.code == "missing_frontmatter" for d in manifest.diagnostics)
            )


class SkillLoaderDigestAndVersionTest(unittest.TestCase):
    def test_loader_exposes_v2_skill_but_skill_model_unchanged(self) -> None:
        # SK-0 keeps the public Skill model and prompt shape intact; the
        # version/digest live on the manifest layer for SK-1.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_v2(root, "repo-review")
            skills = discover_skills(root)
            self.assertEqual(1, len(skills))
            skill = skills[0]
            self.assertEqual(SkillScope.USER, skill.scope)
            self.assertEqual("repo-review", skill.name)
            self.assertFalse(skill.disable_model_invocation)


if __name__ == "__main__":
    unittest.main()


class SkillManifestOptionalFieldsTest(unittest.TestCase):
    """S3：whenToUse / metadata 解析（v2 与 v1 都支持）。"""

    def test_v2_when_to_use_and_metadata(self) -> None:
        from endless_task.skills.manifest import parse_skill_manifest

        text = (
            "---\nname: a\ndescription: d\nversion: 1.0.0\nschema-version: 2\n"
            "whenToUse: 当用户要求评审时\nmetadata:\n  owner: team-x\n---\n正文\n"
        )
        manifest = parse_skill_manifest(Path("pkg/SKILL.md"), text)
        self.assertTrue(manifest.valid)
        self.assertEqual("当用户要求评审时", manifest.when_to_use)
        self.assertEqual((("owner", "team-x"),), manifest.metadata)

    def test_v1_when_to_use(self) -> None:
        from endless_task.skills.manifest import parse_skill_manifest

        text = (
            "---\nname: a\ndescription: d\nwhenToUse: 只在写周报时用\n---\n正文\n"
        )
        manifest = parse_skill_manifest(Path("pkg/SKILL.md"), text)
        self.assertEqual("只在写周报时用", manifest.when_to_use)
        self.assertEqual((), manifest.metadata)
