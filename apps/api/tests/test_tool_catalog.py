from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.tool_platform import (
    BUILTIN_CAPABILITY_PROFILES,
    CapabilityContext,
    CapabilityProfile,
    FakeTool,
    InMemoryCapabilityProfileRegistry,
    InMemoryToolCatalog,
    ToolDefinitionV2,
    ToolEffect,
    ToolOutcome,
    ToolOutcomeStatus,
    ToolProvenance,
    ToolProvenanceKind,
    ToolScope,
    ToolSearchQuery,
    ToolSurfaceRequest,
    create_builtin_profile_registry,
    intersect_capability_layers,
)


def fake_tool(
    name: str,
    *,
    capabilities: frozenset[str] = frozenset(),
    effect: ToolEffect = ToolEffect.READ_ONLY,
    description: str | None = None,
) -> FakeTool:
    return FakeTool(
        definition=ToolDefinitionV2(
            name=name,
            description=description or f"Use {name}.",
            input_schema={"type": "object"},
            effect=effect,
            required_capabilities=capabilities,
        ),
        outcome=ToolOutcome(
            call_id="call_1",
            status=ToolOutcomeStatus.COMPLETED,
            content="ok",
        ),
    )


def provenance(
    kind: ToolProvenanceKind = ToolProvenanceKind.BUILTIN,
    source_id: str = "core",
) -> ToolProvenance:
    return ToolProvenance(kind=kind, source_id=source_id, revision="v1")


def context(
    profile: CapabilityProfile,
    **scope_ids: str,
) -> CapabilityContext:
    return CapabilityContext(profile=profile, **scope_ids)


class CapabilityProfileTest(unittest.TestCase):
    def test_builtin_profiles_are_stable_and_fail_closed(self) -> None:
        registry = create_builtin_profile_registry()

        self.assertEqual(
            [
                "chat",
                "subagent_readonly",
                "subagent_workspace",
                "unattended",
                "work",
            ],
            [profile.name for profile in registry.profiles()],
        )
        self.assertTrue(registry.resolve("chat").allows({"workspace.read"}))
        self.assertFalse(registry.resolve("chat").allows({"workspace.write"}))
        self.assertTrue(registry.resolve("work").allows({"external.action"}))
        self.assertFalse(
            registry.resolve("subagent_readonly").allows({"workspace.write"})
        )
        self.assertEqual(frozenset(), registry.resolve("unattended").capabilities)
        self.assertEqual(5, len(BUILTIN_CAPABILITY_PROFILES))

    def test_denies_and_runtime_layers_can_only_narrow(self) -> None:
        profile = CapabilityProfile(
            name="restricted_work",
            allowed_capabilities=frozenset(
                {"workspace.read", "workspace.write", "network.outbound"}
            ),
            denied_capabilities=frozenset({"network.outbound"}),
        )
        capability_context = CapabilityContext(
            profile=profile,
            requested_capabilities=frozenset(
                {"workspace.read", "workspace.write", "external.action"}
            ),
            deployment_capabilities=frozenset({"workspace.read"}),
        )

        self.assertEqual(
            frozenset({"workspace.read"}),
            capability_context.effective_capabilities,
        )
        self.assertEqual(
            frozenset({"workspace.read"}),
            intersect_capability_layers(
                profile.capabilities,
                {"workspace.read", "workspace.write"},
                {"workspace.read"},
            ),
        )
        self.assertTrue(capability_context.allows({"workspace.read"}))
        self.assertFalse(capability_context.allows({"workspace.write"}))

    def test_unknown_and_wildcard_capabilities_are_rejected(self) -> None:
        for capability in (
            "workspace.*",
            "unknown.execute",
            "credential.use:",
            "credential.use:staging prod",
        ):
            with self.subTest(capability=capability):
                with self.assertRaises(AgentPlatformError) as raised:
                    CapabilityProfile(
                        name="invalid",
                        allowed_capabilities=frozenset({capability}),
                    )
                self.assertEqual("unsupported_capability", raised.exception.code)

        credential_profile = CapabilityProfile(
            name="credentialed",
            allowed_capabilities=frozenset({"credential.use:staging"}),
        )
        self.assertTrue(credential_profile.allows({"credential.use:staging"}))

    def test_profile_registry_requires_explicit_override(self) -> None:
        registry = InMemoryCapabilityProfileRegistry()
        original = CapabilityProfile(
            name="custom",
            allowed_capabilities=frozenset({"workspace.read"}),
        )
        replacement = CapabilityProfile(
            name="custom",
            allowed_capabilities=frozenset({"workspace.write"}),
        )
        registry.register(original)

        with self.assertRaises(AgentPlatformError) as raised:
            registry.register(replacement)
        self.assertEqual("duplicate_capability_profile", raised.exception.code)
        self.assertIs(original, registry.resolve("custom"))

        registry.register(replacement, override=True)
        self.assertIs(replacement, registry.resolve("custom"))


class ToolCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = create_builtin_profile_registry()
        self.catalog = InMemoryToolCatalog()

    def _register_builtin(self, tool: FakeTool):
        return self.catalog.register(
            tool,
            scope=ToolScope.BUILTIN,
            provenance=provenance(),
        )

    def test_surface_depends_on_profile_and_is_deterministically_sorted(self) -> None:
        self._register_builtin(
            fake_tool("write_file", capabilities=frozenset({"workspace.write"}))
        )
        self._register_builtin(
            fake_tool("read_file", capabilities=frozenset({"workspace.read"}))
        )
        self._register_builtin(
            fake_tool(
                "send_message",
                capabilities=frozenset({"external.action"}),
                effect=ToolEffect.EXTERNAL_ACTION,
            )
        )

        chat = self.catalog.surface(
            ToolSurfaceRequest(context=context(self.profiles.resolve("chat")))
        )
        work = self.catalog.surface(
            ToolSurfaceRequest(context=context(self.profiles.resolve("work")))
        )
        readonly = self.catalog.surface(
            ToolSurfaceRequest(
                context=context(self.profiles.resolve("subagent_readonly"))
            )
        )

        self.assertEqual(["read_file"], [item.name for item in chat.definitions])
        self.assertEqual(
            ["read_file", "send_message", "write_file"],
            [item.name for item in work.definitions],
        )
        self.assertEqual(["read_file"], [item.name for item in readonly.definitions])
        self.assertEqual(self.catalog.generation, work.catalog_generation)

    def test_cross_scope_override_is_explicit_and_context_bound(self) -> None:
        builtin = fake_tool(
            "read_file",
            capabilities=frozenset({"workspace.read"}),
            description="Read from the default workspace.",
        )
        workspace = fake_tool(
            "read_file",
            capabilities=frozenset({"workspace.read"}),
            description="Read from workspace ws_1.",
        )
        self._register_builtin(builtin)

        with self.assertRaises(AgentPlatformError) as raised:
            self.catalog.register(
                workspace,
                scope=ToolScope.WORKSPACE,
                scope_id="ws_1",
                provenance=provenance(ToolProvenanceKind.WORKSPACE, "ws_1"),
            )
        self.assertEqual("explicit_tool_override_required", raised.exception.code)
        self.assertEqual(1, self.catalog.generation)

        registration = self.catalog.register(
            workspace,
            scope=ToolScope.WORKSPACE,
            scope_id="ws_1",
            provenance=provenance(ToolProvenanceKind.WORKSPACE, "ws_1"),
            override=True,
        )
        chat = self.profiles.resolve("chat")

        self.assertIs(
            workspace,
            self.catalog.resolve(
                "read_file",
                context(chat, workspace_id="ws_1"),
            ),
        )
        self.assertIs(
            builtin,
            self.catalog.resolve(
                "read_file",
                context(chat, workspace_id="ws_2"),
            ),
        )
        self.assertTrue(registration.override_declared)
        self.assertEqual("ws_1", registration.provenance.source_id)

    def test_scope_precedence_follows_the_active_context_chain(self) -> None:
        tools = {
            ToolScope.BUILTIN: fake_tool("read_file", description="builtin"),
            ToolScope.USER: fake_tool("read_file", description="user"),
            ToolScope.WORKSPACE: fake_tool("read_file", description="workspace"),
            ToolScope.CONVERSATION: fake_tool(
                "read_file",
                description="conversation",
            ),
            ToolScope.AGENT_RUN: fake_tool("read_file", description="run"),
        }
        self._register_builtin(tools[ToolScope.BUILTIN])
        for scope, scope_id, kind in (
            (ToolScope.USER, "user_1", ToolProvenanceKind.USER),
            (ToolScope.WORKSPACE, "ws_1", ToolProvenanceKind.WORKSPACE),
            (
                ToolScope.CONVERSATION,
                "conv_1",
                ToolProvenanceKind.CONVERSATION,
            ),
            (ToolScope.AGENT_RUN, "run_1", ToolProvenanceKind.AGENT_RUN),
        ):
            self.catalog.register(
                tools[scope],
                scope=scope,
                scope_id=scope_id,
                provenance=provenance(kind, scope_id),
                override=True,
            )

        chat = self.profiles.resolve("chat")
        active_context = context(
            chat,
            user_id="user_1",
            workspace_id="ws_1",
            conversation_id="conv_1",
            run_id="run_1",
        )
        other_run_context = context(
            chat,
            user_id="user_1",
            workspace_id="ws_1",
            conversation_id="conv_1",
            run_id="run_2",
        )
        other_conversation_context = context(
            chat,
            user_id="user_1",
            workspace_id="ws_1",
            conversation_id="conv_2",
        )

        self.assertIs(
            tools[ToolScope.AGENT_RUN],
            self.catalog.resolve("read_file", active_context),
        )
        self.assertIs(
            tools[ToolScope.CONVERSATION],
            self.catalog.resolve("read_file", other_run_context),
        )
        self.assertIs(
            tools[ToolScope.WORKSPACE],
            self.catalog.resolve("read_file", other_conversation_context),
        )

    def test_same_scope_replacement_preserves_registration_history(self) -> None:
        original = self._register_builtin(fake_tool("read_file"))
        replacement_tool = fake_tool("read_file", description="Replacement reader.")

        with self.assertRaises(AgentPlatformError):
            self._register_builtin(replacement_tool)
        replacement = self.catalog.register(
            replacement_tool,
            scope=ToolScope.BUILTIN,
            provenance=provenance(source_id="core-v2"),
            override=True,
        )

        self.assertEqual(original.registration_id, replacement.replaced_registration_id)
        self.assertEqual([original, replacement], list(self.catalog.history()))
        self.assertEqual((replacement,), self.catalog.registrations())

    def test_unauthorized_and_unknown_tools_share_safe_failure(self) -> None:
        self._register_builtin(
            fake_tool("read_file", capabilities=frozenset({"workspace.read"}))
        )
        self._register_builtin(
            fake_tool("write_file", capabilities=frozenset({"workspace.write"}))
        )
        chat = context(self.profiles.resolve("chat"))

        codes = []
        for name in ("write_file", "missing_tool"):
            with self.assertRaises(AgentPlatformError) as raised:
                self.catalog.resolve(name, chat)
            codes.append(raised.exception.code)
            self.assertEqual(
                "Tool is unavailable in this context",
                raised.exception.safe_message,
            )
        self.assertEqual(["tool_unavailable", "tool_unavailable"], codes)

    def test_allowlist_and_search_never_expand_capabilities(self) -> None:
        self._register_builtin(
            fake_tool(
                "read_workspace_file",
                capabilities=frozenset({"workspace.read"}),
                description="Read a workspace file.",
            )
        )
        self._register_builtin(
            fake_tool(
                "write_workspace_file",
                capabilities=frozenset({"workspace.write"}),
                description="Write a workspace file.",
            )
        )
        chat = context(self.profiles.resolve("chat"))

        surface = self.catalog.surface(
            ToolSurfaceRequest(
                context=chat,
                allowlist=frozenset(
                    {"read_workspace_file", "write_workspace_file"}
                ),
            )
        )
        results = self.catalog.search(
            ToolSearchQuery(context=chat, query="workspace file")
        )

        self.assertEqual(
            ["read_workspace_file"],
            [item.name for item in surface.definitions],
        )
        self.assertEqual(["read_workspace_file"], [item.name for item in results])

    def test_effectful_tool_without_capability_is_rejected_atomically(self) -> None:
        with self.assertRaises(AgentPlatformError) as raised:
            self._register_builtin(
                fake_tool("write_file", effect=ToolEffect.LOCAL_WRITE)
            )
        self.assertEqual("missing_tool_capability", raised.exception.code)
        self.assertEqual(0, self.catalog.generation)
        self.assertEqual((), self.catalog.registrations())

    def test_scope_identity_is_required_outside_builtin(self) -> None:
        with self.assertRaises(AgentPlatformError) as raised:
            self.catalog.register(
                fake_tool("read_file"),
                scope=ToolScope.CONVERSATION,
                provenance=provenance(ToolProvenanceKind.CONVERSATION, "conv_1"),
            )
        self.assertEqual("invalid_tool_scope", raised.exception.code)
        self.assertEqual(0, self.catalog.generation)


if __name__ == "__main__":
    unittest.main()