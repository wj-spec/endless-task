"""17 切片 ② read_workspace_file 健壮化单测。

覆盖：字符预算截断与续读提示、start_line/line_count 分段、单行超预算截断、
大文件（>max_file_bytes）流式窗口读取（不再 file_too_large 整体拒绝）、
小文件整读精确 totalLines 契约保持。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from endless_task.domain.repositories import ConflictError
from endless_task.runtime.cancellation import CancellationToken
from endless_task.tooling import ToolCall, ToolCallStatus, ToolError
from endless_task.workspace_runtime.fs_tools import ReadWorkspaceFileTool
from endless_task.workspace_runtime.resolver import WorkspaceBinding


class _StubResolver:
    def __init__(self, root: Path) -> None:
        self._binding = WorkspaceBinding(workspace_id="ws_1", root=root)

    def require_binding(self, conversation_id: str) -> WorkspaceBinding:
        return self._binding


def _call(**arguments) -> ToolCall:
    return ToolCall(
        id="call_1",
        conversation_id="conv_1",
        turn_id="turn_1",
        response_variant_id="variant_1",
        tool_name="read_workspace_file",
        arguments=arguments,
        status=ToolCallStatus.CREATED,
        created_at="2026-09-06T00:00:00.000Z",
    )


def _token() -> CancellationToken:
    return CancellationToken()


def _lines(count: int, *, prefix: str = "行") -> str:
    return "\n".join(f"{prefix}-{index}" for index in range(1, count + 1))


class ReadWorkspaceFileRobustnessTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.tool = ReadWorkspaceFileTool(_StubResolver(self.root))
        self._writer = ReadWorkspaceFileTool(_StubResolver(self.root))

    def tearDown(self) -> None:
        self._temp.cleanup()

    async def _read(self, path: str, **arguments) -> dict:
        result = await self.tool.execute(
            _call(path=path, **arguments), _token()
        )
        return result.structured_content or {}

    def _write(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    # ---- 预算截断与续读提示 ----

    async def test_budget_truncation_adds_resume_hint(self) -> None:
        # 每行 ~40 字符；默认 120 行远超 40k? 不够，制造超预算场景：
        # 用长行逼近预算。40k 预算 - 提示余量 → 约 39k 字符 ≈ 每条 200 字符 × 200 行。
        long_line = "x" * 200
        content = "\n".join(long_line for _ in range(500))
        self._write("big_lines.txt", content)
        info = await self._read("big_lines.txt")
        # 不应把 500 行全塞进单次输出：预算截断 + 续读提示。
        self.assertTrue(info["truncated"])
        resume = info.get("resumeStartLine")
        self.assertIsInstance(resume, int)
        self.assertGreater(resume, 1)
        self.assertLessEqual(resume, 500)

    async def test_resume_hint_tells_model_how_to_continue(self) -> None:
        long_line = "y" * 300
        content = "\n".join(long_line for _ in range(300))
        self._write("resume_target.txt", content)
        result = await self.tool.execute(
            _call(path="resume_target.txt"), _token()
        )
        self.assertIn("[内容已截断", result.content)
        self.assertIn("start_line=", result.content)

    async def test_budget_respected_content_length(self) -> None:
        long_line = "z" * 250
        content = "\n".join(long_line for _ in range(400))
        self._write("cap_check.txt", content)
        result = await self.tool.execute(
            _call(path="cap_check.txt"), _token()
        )
        # content 含来源标签与提示；正文部分受 max_output_characters 约束，
        # 截断提示在预算余量内追加，总长不应远超上限 + 提示长度。
        self.assertLess(
            len(result.content),
            self.tool.definition.max_output_characters + 400,
        )

    # ---- 分段读取 ----

    async def test_slice_read_with_start_line_and_count(self) -> None:
        self._write("numbered.txt", _lines(50))
        info = await self._read("numbered.txt", start_line=10, line_count=5)
        self.assertEqual(10, info["startLine"])
        self.assertEqual(14, info["endLine"])
        self.assertEqual(50, info["totalLines"])
        self.assertFalse(info["truncated"])

    async def test_default_reads_from_line_one(self) -> None:
        self._write("short.txt", _lines(5))
        info = await self._read("short.txt")
        self.assertEqual(1, info["startLine"])
        self.assertEqual(5, info["totalLines"])
        self.assertEqual(5, info["endLine"])
        self.assertFalse(info["truncated"])

    async def test_start_line_beyond_file_rejected(self) -> None:
        self._write("tiny.txt", _lines(3))
        with self.assertRaises(ToolError) as ctx:
            await self.tool.execute(
                _call(path="tiny.txt", start_line=9), _token()
            )
        self.assertEqual("file_line_out_of_range", ctx.exception.code)

    # ---- 大文件窗口读取（切片② 关键修复） ----

    async def test_large_file_reads_window_without_too_large_error(self) -> None:
        small_limit_tool = ReadWorkspaceFileTool(
            _StubResolver(self.root), max_file_bytes=200
        )
        # 300 行 ≈ >200 字节 → 大文件分支
        self._write("large_notes.txt", _lines(300))
        result = await small_limit_tool.execute(
            _call(path="large_notes.txt", start_line=50, line_count=3),
            _token(),
        )
        info = result.structured_content or {}
        self.assertTrue(info.get("largeFile"))
        self.assertEqual(50, info["startLine"])
        self.assertEqual(52, info["endLine"])
        self.assertIsNone(info.get("totalLines"))

    async def test_large_file_start_line_beyond_eof_rejected(self) -> None:
        small_limit_tool = ReadWorkspaceFileTool(
            _StubResolver(self.root), max_file_bytes=100
        )
        self._write("large_notes.txt", _lines(20))
        with self.assertRaises(ToolError) as ctx:
            await small_limit_tool.execute(
                _call(path="large_notes.txt", start_line=50), _token()
            )
        self.assertEqual("file_line_out_of_range", ctx.exception.code)

    # ---- 单行超预算 ----

    async def test_single_over_budget_line_is_clipped(self) -> None:
        # 构造单行超预算文件（> 40k 单行）——工具整读分支会 clip。
        huge_line = "q" * 60_000
        self._write("huge_line.txt", huge_line + "\n" + _lines(5))
        info = await self._read("huge_line.txt")
        # 单行 clip 后标记 truncated，且不再抛错。
        self.assertTrue(info["truncated"])

    # ---- 契约回归：小文件 totalLines 精确 ----

    async def test_small_file_keeps_exact_total_lines(self) -> None:
        self._write("doc.md", _lines(10, prefix="line"))
        info = await self._read("doc.md")
        self.assertEqual(10, info["totalLines"])
        self.assertFalse(info.get("largeFile", False))

    # ---- 未传 line_count 时按预算自动读满（切片② 关键语义） ----

    async def test_without_line_count_reads_until_budget_then_resumes(self) -> None:
        # 300 行 × 200 字符 = 60k 字符 > 40k 预算 → 未传 line_count 时应自动
        # 读到预算附近并给出续读提示，而不是停在固定 120 行。
        long_line = "w" * 200
        content = "\n".join(long_line for _ in range(300))
        self._write("auto_read.txt", content)
        info = await self._read("auto_read.txt")
        self.assertTrue(info["truncated"])
        self.assertIn("resumeStartLine", info)
        # 应远超默认 120 行（预算内自动多读），但未读完全部 300 行。
        self.assertGreater(info["endLine"], 120)
        self.assertLess(info["endLine"], 300)

    async def test_explicit_line_count_overrides_auto_budget_read(self) -> None:
        long_line = "v" * 200
        content = "\n".join(long_line for _ in range(300))
        self._write("explicit_count.txt", content)
        info = await self._read("explicit_count.txt", line_count=25)
        self.assertEqual(25, info["endLine"] - info["startLine"] + 1)
        self.assertFalse(info["truncated"])
        self.assertNotIn("resumeStartLine", info)

    async def test_large_file_budget_truncation_with_resume(self) -> None:
        small_limit_tool = ReadWorkspaceFileTool(
            _StubResolver(self.root), max_file_bytes=200
        )
        long_line = "u" * 200
        content = "\n".join(long_line for _ in range(400))
        self._write("large_auto.txt", content)
        result = await small_limit_tool.execute(
            _call(path="large_auto.txt"), _token()
        )
        info = result.structured_content or {}
        self.assertTrue(info.get("largeFile"))
        self.assertTrue(info["truncated"])
        self.assertIn("[内容已截断", result.content)


if __name__ == "__main__":
    unittest.main()
