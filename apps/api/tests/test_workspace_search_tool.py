"""17 切片 ① workspace_search 单测：结构化文件内容搜索工具。

覆盖：路径安全/逃逸拒绝、递归命中与行号、glob 限定、正则与大小写、
二进制与超限文件跳过、ignore 目录、zero-hit、单文件命中截断、max_results。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.models import PermissionMode
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import (
    ToolApprovalMode,
    ToolCall,
    ToolCallStatus,
    ToolEffect,
    ToolError,
)
from endless_task.workspace_runtime.resolver import WorkspaceBinding
from endless_task.workspace_runtime.search_tool import (
    MAX_HITS_PER_FILE,
    WorkspaceSearchTool,
    _DEFAULT_IGNORE_DIRS,
)


class _StubResolver:
    def __init__(self, root: Path, *, bound: bool = True) -> None:
        self._binding = WorkspaceBinding(workspace_id="ws_1", root=root)
        self._bound = bound

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        if not self._bound:
            raise ToolError(
                "workspace_not_bound",
                "当前会话所在的工作区未绑定本地目录。",
                retryable=False,
            )
        return self._binding


def _call(**arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="workspace_search",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-06T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


class WorkspaceSearchToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        (self.root / "docs").mkdir()
        (self.root / "docs" / "plan.md").write_text(
            "发布计划 v1\n- 周一评审\n- 周二发布\n", encoding="utf-8"
        )
        (self.root / "notes.txt").write_text(
            "日常记录\ntodo: 买牛奶\ntodo: 写周报\n", encoding="utf-8"
        )
        (self.root / "README.md").write_text(
            "# Endless Task\n\n本地优先的助手。\n", encoding="utf-8"
        )
        (self.root / "binary.dat").write_bytes(b"\x00\x01\x02todo\x00")
        # ignore 目录不应被扫
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "x.js").write_text("todo", encoding="utf-8")
        self.tool = WorkspaceSearchTool(_StubResolver(self.root))

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def _run(self, **arguments) -> dict:
        result = await self.tool.execute(_call(**arguments), _token())
        return result.structured_content or {}

    async def test_recursive_hits_with_line_numbers(self) -> None:
        info = await self._run(pattern="todo")
        self.assertFalse(info["zeroHit"])
        paths = {hit["path"] for hit in info["hits"]}
        self.assertEqual({"notes.txt"}, paths)
        matches = info["hits"][0]["matches"]
        self.assertEqual([2, 3], [m["line"] for m in matches])
        self.assertTrue(all(m["text"].startswith("todo") for m in matches))

    async def test_scope_path_limits_search_to_subdir(self) -> None:
        info = await self._run(pattern="发布", path="docs")
        self.assertFalse(info["zeroHit"])
        self.assertEqual({"docs/plan.md"}, {hit["path"] for hit in info["hits"]})

    async def test_glob_file_pattern(self) -> None:
        info = await self._run(pattern="本地", file_pattern="*.md")
        self.assertFalse(info["zeroHit"])
        self.assertEqual({"README.md"}, {hit["path"] for hit in info["hits"]})

    async def test_case_sensitive(self) -> None:
        info = await self._run(pattern="todo", case_sensitive=True)
        self.assertFalse(info["zeroHit"])
        self.assertTrue(info["scannedFiles"] >= 1)

        info_upper = await self._run(pattern="TODO", case_sensitive=True)
        # 只有 notes.txt 的行首是小写 todo；大写 TODO 无命中（文件内无大写 TODO）
        self.assertTrue(info_upper["zeroHit"])

    async def test_zero_hit_reports_explicitly(self) -> None:
        info = await self._run(pattern="不存在的词xyz")
        self.assertTrue(info["zeroHit"])
        self.assertEqual([], info["hits"])

    async def test_binary_and_ignore_dirs_skipped(self) -> None:
        info = await self._run(pattern="todo")
        paths = {hit["path"] for hit in info["hits"]}
        self.assertNotIn("binary.dat", paths)
        self.assertNotIn("node_modules/x.js", paths)

    async def test_path_escape_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            await self.tool.execute(_call(pattern="x", path="../escape"), _token())
        self.assertEqual("path_escape", ctx.exception.code)

    async def test_invalid_pattern_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            await self.tool.execute(_call(pattern="("), _token())
        self.assertEqual("invalid_pattern", ctx.exception.code)

    async def test_missing_path_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            await self.tool.execute(_call(pattern="x", path="missing_dir"), _token())
        self.assertEqual("path_not_found", ctx.exception.code)

    async def test_max_results_truncates_and_marks(self) -> None:
        # 生成一个命中超过 max_results 的文件集
        for index in range(5):
            (self.root / f"many_{index}.txt").write_text(
                "needle\n" * 100, encoding="utf-8"
            )
        info = await self._run(pattern="needle", max_results=3)
        self.assertTrue(info["truncated"])
        self.assertEqual(3, len(info["hits"]))

    async def test_single_file_hit_cap(self) -> None:
        (self.root / "big.txt").write_text(
            "needle\n" * (MAX_HITS_PER_FILE + 50), encoding="utf-8"
        )
        info = await self._run(pattern="needle", path="big.txt")
        self.assertEqual(1, len(info["hits"]))
        self.assertEqual(MAX_HITS_PER_FILE, info["hits"][0]["fileHitCount"])
        self.assertTrue(info["truncated"])

    async def test_unbound_conversation_rejected(self) -> None:
        tool = WorkspaceSearchTool(_StubResolver(self.root, bound=False))
        with self.assertRaises(ToolError) as ctx:
            await tool.execute(_call(pattern="x"), _token())
        self.assertEqual("workspace_not_bound", ctx.exception.code)

    async def test_content_label_has_source_path_and_lines(self) -> None:
        result = await self.tool.execute(_call(pattern="todo"), _token())
        self.assertIn("notes.txt", result.content)
        self.assertIn("L2", result.content)

    def test_definition_contract(self) -> None:
        definition = WorkspaceSearchTool.definition
        self.assertEqual("workspace_search", definition.name)
        self.assertEqual(ToolEffect.READ_ONLY, definition.effect)
        self.assertEqual(ToolApprovalMode.AUTO, definition.approval_mode)
        self.assertIn("pattern", definition.input_schema.get("properties", {}))
        self.assertEqual(
            ["pattern"], definition.input_schema.get("required")
        )
        self.assertTrue(definition.timeout_seconds >= 10)
        # ignore 默认含常见依赖/元数据目录
        self.assertIn(".git", _DEFAULT_IGNORE_DIRS)


if __name__ == "__main__":
    unittest.main()
