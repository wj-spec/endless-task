from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "endless_task"
PROTOCOL_FILES = (
    SOURCE_ROOT / "agent_platform" / "protocol.py",
    SOURCE_ROOT / "agent_platform" / "effects.py",
    SOURCE_ROOT / "agent_kernel" / "protocol.py",
    SOURCE_ROOT / "tool_platform" / "protocol.py",
    SOURCE_ROOT / "tool_platform" / "profiles.py",
    SOURCE_ROOT / "tool_platform" / "catalog.py",
    SOURCE_ROOT / "tool_platform" / "projection.py",
    SOURCE_ROOT / "tool_platform" / "scheduler.py",
    SOURCE_ROOT / "tool_platform" / "pipeline.py",
    SOURCE_ROOT / "tool_platform" / "middleware.py",
    SOURCE_ROOT / "tool_platform" / "spill.py",
    SOURCE_ROOT / "tool_platform" / "legacy_policy.py",
    SOURCE_ROOT / "tool_platform" / "surface.py",
    SOURCE_ROOT / "tool_platform" / "search_tools.py",
    SOURCE_ROOT / "extensions" / "protocol.py",
    SOURCE_ROOT / "extensions" / "decisions.py",
    SOURCE_ROOT / "extensions" / "bus.py",
    SOURCE_ROOT / "extensions" / "builtin.py",
    SOURCE_ROOT / "context_engine" / "protocol.py",
    SOURCE_ROOT / "context_engine" / "planner.py",
    SOURCE_ROOT / "context_engine" / "retention.py",
    SOURCE_ROOT / "context_engine" / "checkpoint.py",
    SOURCE_ROOT / "context_engine" / "relevance.py",
    SOURCE_ROOT / "reliability" / "retry.py",
    SOURCE_ROOT / "reliability" / "stop.py",
    SOURCE_ROOT / "reliability" / "retry_runtime.py",
    SOURCE_ROOT / "execution_env" / "protocol.py",
    SOURCE_ROOT / "execution_env" / "local.py",
    SOURCE_ROOT / "execution_env" / "checkpoint.py",
    SOURCE_ROOT / "execution_env" / "ledger.py",
    SOURCE_ROOT / "execution_env" / "seatbelt.py",
    SOURCE_ROOT / "delegation" / "protocol.py",
    SOURCE_ROOT / "runtime_ledger" / "protocol.py",
)
FORBIDDEN_IMPORT_PREFIXES = (
    "fastapi",
    "mcp",
    "sqlite3",
    "endless_task.api",
    "endless_task.mcp_runtime",
    "endless_task.storage",
    "endless_task.runtime_v2",
)


class AgentPlatformArchitectureTest(unittest.TestCase):
    def test_protocol_modules_do_not_import_frameworks_or_concrete_backends(self) -> None:
        violations: list[str] = []
        for path in PROTOCOL_FILES:
            self.assertTrue(path.is_file(), path)
            for imported in _imports(path):
                if imported.startswith(FORBIDDEN_IMPORT_PREFIXES):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} -> {imported}")

        self.assertEqual([], violations)

    def test_runtime_v2_domain_does_not_depend_on_agent_platform_implementations(self) -> None:
        domain = SOURCE_ROOT / "runtime_v2" / "domain.py"
        forbidden = (
            "endless_task.agent_kernel",
            "endless_task.tool_platform",
            "endless_task.extensions",
            "endless_task.context_engine",
            "endless_task.execution_env",
            "endless_task.delegation",
        )
        violations = [
            imported
            for imported in _imports(domain)
            if imported.startswith(forbidden)
        ]
        self.assertEqual([], violations)

    def test_protocol_packages_have_explicit_public_exports(self) -> None:
        package_names = (
            "agent_platform",
            "agent_kernel",
            "tool_platform",
            "extensions",
            "context_engine",
            "execution_env",
            "delegation",
            "runtime_ledger",
        )
        for package_name in package_names:
            init_path = SOURCE_ROOT / package_name / "__init__.py"
            module = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
            exported = any(
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
                for node in module.body
            )
            self.assertTrue(exported, f"{package_name} must define explicit __all__")


def _imports(path: Path) -> tuple[str, ...]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return tuple(imports)


if __name__ == "__main__":
    unittest.main()