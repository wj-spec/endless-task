from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.tool_platform import (
    DEFAULT_ANNOTATION_KEYWORDS,
    ApprovalPolicy,
    CapabilityContext,
    CapabilityProfile,
    FakeTool,
    InMemoryToolCatalog,
    ProjectedToolSchema,
    ProjectionDecision,
    ProviderSchemaProfile,
    ToolDefinitionV2,
    ToolEffect,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolProvenance,
    ToolProvenanceKind,
    ToolScope,
    ToolSurface,
    ToolSurfaceRequest,
    project_tool_surface,
    schema_fingerprint,
    tool_may_degrade,
)

READ_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path."},
        "lines": {"type": "integer", "minimum": 1},
    },
    "required": ["path"],
    "additionalProperties": False,
}

WRITE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
}


def fake_tool(
    name: str,
    *,
    input_schema: dict,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    capabilities: frozenset[str] = frozenset(),
    approval: ApprovalPolicy = ApprovalPolicy.AUTO,
    description: str | None = None,
) -> FakeTool:
    return FakeTool(
        definition=ToolDefinitionV2(
            name=name,
            description=description or f"Use {name}.",
            input_schema=input_schema,
            effect=effect,
            approval=approval,
            required_capabilities=capabilities,
        ),
        outcome=ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
        ),
    )


def provenance() -> ToolProvenance:
    return ToolProvenance(kind=ToolProvenanceKind.BUILTIN, source_id="core")


WORK_PROFILE = CapabilityProfile(
    name="projection_work",
    allowed_capabilities=frozenset(
        {
            "workspace.read",
            "workspace.write",
            "workspace.delete",
            "process.spawn",
            "process.signal",
            "network.outbound",
            "external.action",
            "session.query",
            "memory.read",
        }
    ),
)


def context() -> CapabilityContext:
    return CapabilityContext(profile=WORK_PROFILE)


def surface(*tools: FakeTool) -> ToolSurface:
    catalog = InMemoryToolCatalog()
    for tool in tools:
        catalog.register(tool, scope=ToolScope.BUILTIN, provenance=provenance())
    return catalog.surface(ToolSurfaceRequest(context=context()))


def default_profile(**overrides) -> ProviderSchemaProfile:
    values = {"name": "test_provider"}
    values.update(overrides)
    return ProviderSchemaProfile(**values)


class ToolSchemaProjectionTest(unittest.TestCase):
    def test_passthrough_profile_includes_tools_with_original_schemas(self) -> None:
        read_tool = fake_tool("read_file", input_schema=READ_TOOL_SCHEMA)
        write_tool = fake_tool(
            "write_file",
            input_schema=WRITE_TOOL_SCHEMA,
            effect=ToolEffect.LOCAL_WRITE,
            capabilities=frozenset({"workspace.write"}),
        )
        report = project_tool_surface(surface(read_tool, write_tool), default_profile())

        self.assertEqual(["read_file", "write_file"], list(report.projected_names))
        self.assertEqual((), report.excluded)
        self.assertEqual("test_provider", report.profile_name)
        self.assertEqual("draft2020-12", report.dialect)
        for projected in report.projected:
            self.assertEqual(ProjectionDecision.INCLUDED, projected.decision)
            self.assertIsNone(projected.diagnostic)
            self.assertEqual(
                READ_TOOL_SCHEMA if projected.name == "read_file" else WRITE_TOOL_SCHEMA,
                dict(projected.input_schema),
            )
            self.assertEqual(64, len(projected.fingerprint))

    def test_fingerprint_is_stable_across_runs_and_key_order(self) -> None:
        schema_a = {
            "type": "object",
            "properties": {"alpha": {"type": "string"}},
            "required": ["alpha"],
        }
        schema_b = {
            "required": ["alpha"],
            "properties": {"alpha": {"type": "string"}},
            "type": "object",
        }
        tool_a = fake_tool("stable_a", input_schema=schema_a)
        tool_b = fake_tool("stable_b", input_schema=schema_b)

        first = project_tool_surface(surface(tool_a, tool_b), default_profile())
        second = project_tool_surface(surface(tool_a, tool_b), default_profile())

        self.assertEqual(first.fingerprint, second.fingerprint)
        by_name = {item.name: item for item in first.projected}
        self.assertEqual(by_name["stable_a"].fingerprint, by_name["stable_b"].fingerprint)
        self.assertEqual(
            schema_fingerprint(schema_a),
            schema_fingerprint(schema_b),
        )

    def test_fingerprint_changes_when_surface_or_schema_changes(self) -> None:
        tool = fake_tool(
            "mutable",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        )
        baseline = project_tool_surface(surface(tool), default_profile())

        extra = fake_tool("extra", input_schema={"type": "object"})
        grown = project_tool_surface(surface(tool, extra), default_profile())
        self.assertNotEqual(baseline.fingerprint, grown.fingerprint)

        changed = fake_tool(
            "mutable",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
            },
        )
        altered = project_tool_surface(surface(changed), default_profile())
        self.assertNotEqual(baseline.fingerprint, altered.fingerprint)

    def test_description_over_limit_is_excluded(self) -> None:
        tool = fake_tool(
            "chatty",
            input_schema={"type": "object"},
            description="A very long description that the provider cannot accept.",
        )
        report = project_tool_surface(
            surface(tool),
            default_profile(max_description_characters=10),
        )

        self.assertEqual((), report.projected)
        self.assertEqual(["chatty"], list(report.excluded_names))
        diagnostic = report.excluded[0]
        self.assertEqual("description_too_long", diagnostic.code)
        self.assertEqual(
            10,
            dict(diagnostic.details)["max_description_characters"],
        )

    def test_schema_over_size_limit_is_excluded(self) -> None:
        tool = fake_tool(
            "huge",
            input_schema={
                "type": "object",
                "properties": {
                    "value_with_a_very_long_name_that_consumes_bytes": {
                        "type": "string"
                    }
                },
            },
        )
        report = project_tool_surface(
            surface(tool),
            default_profile(max_parameters_bytes=32),
        )

        self.assertEqual(["huge"], list(report.excluded_names))
        self.assertEqual("schema_too_large", report.excluded[0].code)

    def test_schema_over_depth_limit_is_excluded(self) -> None:
        tool = fake_tool(
            "deep",
            input_schema={
                "type": "object",
                "properties": {
                    "level_one": {
                        "type": "object",
                        "properties": {
                            "level_two": {
                                "type": "object",
                                "properties": {"leaf": {"type": "string"}},
                            }
                        },
                    }
                },
            },
        )
        report = project_tool_surface(
            surface(tool),
            default_profile(max_parameters_depth=2),
        )

        self.assertEqual(["deep"], list(report.excluded_names))
        self.assertEqual("schema_too_deep", report.excluded[0].code)

    def test_unsupported_annotation_keywords_are_dropped_with_diagnostic(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The path.",
                    "examples": ["/tmp/a.txt"],
                    "default": "/tmp/a.txt",
                }
            },
        }
        profile = default_profile(
            supported_keywords=frozenset(
                {"type", "object", "string", "properties", "description"}
            )
        )
        report = project_tool_surface(surface(fake_tool("annotated", input_schema=schema)), profile)

        self.assertEqual(["annotated"], list(report.projected_names))
        projected = report.projected[0]
        self.assertEqual(ProjectionDecision.INCLUDED, projected.decision)
        self.assertIsNotNone(projected.diagnostic)
        self.assertEqual("annotations_removed", projected.diagnostic.code)
        self.assertEqual(
            ("default", "examples"),
            projected.diagnostic.removed_keywords,
        )
        nested = dict(projected.input_schema)["properties"]["path"]
        self.assertNotIn("examples", nested)
        self.assertNotIn("default", nested)
        self.assertEqual("The path.", nested["description"])

    def test_effectful_tool_with_unsupported_constraint_is_excluded_fail_closed(
        self,
    ) -> None:
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "pattern": "^/tmp/"} },
        }
        write_tool = fake_tool(
            "destructive_write",
            input_schema=schema,
            effect=ToolEffect.LOCAL_WRITE,
            capabilities=frozenset({"workspace.write"}),
        )
        # Even with degradation enabled, effectful tools must never be loosened.
        profile = default_profile(
            supported_keywords=frozenset({"type", "object", "string", "properties"}),
            degrade_safe_tool_constraints=True,
        )
        report = project_tool_surface(surface(write_tool), profile)

        self.assertEqual((), report.projected)
        self.assertEqual(["destructive_write"], list(report.excluded_names))
        diagnostic = report.excluded[0]
        self.assertEqual("incompatible_provider_schema", diagnostic.code)
        self.assertEqual(["pattern"], list(dict(diagnostic.details)["unsupported_keywords"]))

    def test_safe_read_only_tool_degrades_when_opt_in(self) -> None:
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "pattern": "^/tmp/"}},
        }
        read_tool = fake_tool("safe_read", input_schema=schema)
        profile = default_profile(
            supported_keywords=frozenset({"type", "object", "string", "properties"}),
            degrade_safe_tool_constraints=True,
        )
        report = project_tool_surface(surface(read_tool), profile)

        self.assertEqual(["safe_read"], list(report.projected_names))
        projected = report.projected[0]
        self.assertEqual(ProjectionDecision.DEGRADED, projected.decision)
        self.assertEqual("constraints_removed", projected.diagnostic.code)
        self.assertEqual(("pattern",), projected.diagnostic.removed_keywords)
        nested = dict(projected.input_schema)["properties"]["path"]
        self.assertNotIn("pattern", nested)
        self.assertIsInstance(projected, ProjectedToolSchema)

    def test_safe_read_only_tool_is_not_degraded_without_opt_in(self) -> None:
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "pattern": "^/tmp/"}},
        }
        read_tool = fake_tool("safe_read", input_schema=schema)
        profile = default_profile(
            supported_keywords=frozenset({"type", "object", "string", "properties"}),
        )
        report = project_tool_surface(surface(read_tool), profile)

        self.assertEqual((), report.projected)
        self.assertEqual(["safe_read"], list(report.excluded_names))
        self.assertEqual("incompatible_provider_schema", report.excluded[0].code)

    def test_vendor_extension_keyword_follows_semantic_rule(self) -> None:
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "x-ui-group": "files",
        }
        read_tool = fake_tool("extension_read", input_schema=schema)
        write_tool = fake_tool(
            "extension_write",
            input_schema=schema,
            effect=ToolEffect.LOCAL_WRITE,
            capabilities=frozenset({"workspace.write"}),
        )
        profile = default_profile(
            supported_keywords=frozenset({"type", "object", "string", "properties"}),
            degrade_safe_tool_constraints=True,
        )
        report = project_tool_surface(surface(read_tool, write_tool), profile)

        self.assertEqual(["extension_read"], list(report.projected_names))
        self.assertEqual(["extension_write"], list(report.excluded_names))
        degraded = report.projected[0]
        self.assertEqual(ProjectionDecision.DEGRADED, degraded.decision)
        self.assertIn("x-ui-group", degraded.diagnostic.removed_keywords)
        self.assertNotIn("x-ui-group", dict(degraded.input_schema))

    def test_tool_may_degrade_policy(self) -> None:
        enabled = default_profile(degrade_safe_tool_constraints=True)
        disabled = default_profile(degrade_safe_tool_constraints=False)
        safe = fake_tool("safe", input_schema={"type": "object"})
        approved_ask = fake_tool(
            "ask_tool",
            input_schema={"type": "object"},
            approval=ApprovalPolicy.ASK,
        )
        write_tool = fake_tool(
            "write_tool",
            input_schema={"type": "object"},
            effect=ToolEffect.LOCAL_WRITE,
            capabilities=frozenset({"workspace.write"}),
        )
        network_tool = fake_tool(
            "network_tool",
            input_schema={"type": "object"},
            effect=ToolEffect.NETWORK,
            capabilities=frozenset({"network.outbound"}),
        )

        self.assertTrue(tool_may_degrade(safe.definition, enabled))
        self.assertFalse(tool_may_degrade(safe.definition, disabled))
        self.assertFalse(tool_may_degrade(approved_ask.definition, enabled))
        self.assertFalse(tool_may_degrade(write_tool.definition, enabled))
        self.assertFalse(tool_may_degrade(network_tool.definition, enabled))

    def test_report_ordering_and_disjoint_sets(self) -> None:
        over_limit = fake_tool(
            "z_oversized",
            input_schema={
                "type": "object",
                "properties": {"a_very_long_property_name_pushing_the_schema_over": {"type": "string"}},
            },
        )
        fine = fake_tool("a_fine", input_schema={"type": "object"})
        report = project_tool_surface(
            surface(fine, over_limit),
            default_profile(max_parameters_bytes=64),
        )

        self.assertEqual(["a_fine"], list(report.projected_names))
        self.assertEqual(["z_oversized"], list(report.excluded_names))
        self.assertFalse(set(report.projected_names) & set(report.excluded_names))
        self.assertEqual(["z_oversized"], [entry.tool_name for entry in report.excluded])
        self.assertEqual("schema_too_large", report.excluded[0].code)

    def test_deterministic_report_across_catalog_build_order(self) -> None:
        first_tool = fake_tool("alpha", input_schema={"type": "object"})
        second_tool = fake_tool(
            "beta",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "pattern": "^/"}},
            },
        )
        narrow = default_profile(
            supported_keywords=frozenset({"type", "object", "string", "properties"}),
        )
        report_a = project_tool_surface(surface(first_tool, second_tool), narrow)
        report_b = project_tool_surface(surface(second_tool, first_tool), narrow)
        self.assertEqual(report_a.fingerprint, report_b.fingerprint)
        self.assertEqual(report_a.projected_names, report_b.projected_names)
        self.assertEqual(report_a.excluded_names, report_b.excluded_names)

    def test_engine_validates_input_types(self) -> None:
        tool = fake_tool("typed", input_schema={"type": "object"})
        tool_surface = surface(tool)
        with self.assertRaises(AgentPlatformError):
            project_tool_surface(tool_surface, "not-a-profile")
        with self.assertRaises(AgentPlatformError):
            project_tool_surface("not-a-surface", default_profile())

    def test_profile_validation_rejects_bad_values(self) -> None:
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(name="", supported_keywords=frozenset())
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(
                name="p",
                max_parameters_bytes=0,
                supported_keywords=frozenset(),
            )
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(
                name="p",
                max_parameters_depth=0,
                supported_keywords=frozenset(),
            )
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(
                name="p",
                max_description_characters=-1,
                supported_keywords=frozenset(),
            )
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(name="p", supported_keywords="type")
        with self.assertRaises(AgentPlatformError):
            ProviderSchemaProfile(name="p", annotation_keywords="title")

    def test_annotation_vocabulary_has_only_advisory_keywords(self) -> None:
        self.assertEqual(
            frozenset(
                {
                    "$comment",
                    "default",
                    "deprecated",
                    "description",
                    "examples",
                    "readOnly",
                    "title",
                    "writeOnly",
                }
            ),
            DEFAULT_ANNOTATION_KEYWORDS,
        )

    def test_schema_fingerprint_rejects_invalid_schema_root(self) -> None:
        with self.assertRaises(AgentPlatformError):
            schema_fingerprint({"type": "string"})


if __name__ == "__main__":
    unittest.main()


class OpenAiCompatibleProfileTest(unittest.TestCase):
    def test_builtin_profile_is_calibrated_to_production_limits(self) -> None:
        from endless_task.tool_platform import (
            OPENAI_COMPATIBLE_PROFILE_LIMITS,
            create_openai_compatible_profile,
        )

        profile = create_openai_compatible_profile()
        self.assertEqual("openai_compatible_default", profile.name)
        self.assertIsNone(profile.supported_keywords)  # full dialect passthrough
        self.assertEqual(
            (65_536, 32, 1_024),
            (
                profile.max_parameters_bytes,
                profile.max_parameters_depth,
                profile.max_description_characters,
            ),
        )
        self.assertEqual(OPENAI_COMPATIBLE_PROFILE_LIMITS, (65_536, 32, 1_024))

    def test_builtin_profile_rejects_oversized_schema_like_production_caps(self) -> None:
        from endless_task.tool_platform import create_openai_compatible_profile

        oversized = {
            "type": "object",
            "properties": {
                "value": {"type": "string", "minLength": 1},
                "padding": {
                    "type": "string",
                    "description": "x" * (70_000),
                },
            },
        }
        tool = fake_tool("oversized_schema", input_schema=oversized)
        report = project_tool_surface(
            surface(tool),
            create_openai_compatible_profile(),
        )
        self.assertEqual(["oversized_schema"], list(report.excluded_names))
        self.assertEqual("schema_too_large", report.excluded[0].code)
