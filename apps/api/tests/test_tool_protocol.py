from __future__ import annotations

import unittest

from endless_task.tooling import (
    ApprovalRequest,
    ApprovalStatus,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolActivityCopy,
    ToolCall,
    ToolCallError,
    ToolCallStatus,
    ToolDefinition,
    ToolEffect,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolValidationError,
)
from endless_task.tooling.schema import ToolSchemaError, validate_tool_arguments


class StubTool:
    def __init__(self, definition: ToolDefinition) -> None:
        self.definition = definition

    async def execute(self, call, cancellation_token):
        del cancellation_token
        return ToolResult(tool_call_id=call.id, content="ok")


def read_definition(name: str = "read_uploaded_file") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="读取用户已经上传并授权的文本文件。",
        input_schema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
            "additionalProperties": False,
        },
    )


class ToolProtocolTest(unittest.IsolatedAsyncioTestCase):
    def test_read_only_definition_can_be_auto_approved(self) -> None:
        definition = read_definition()

        self.assertEqual(ToolEffect.READ_ONLY, definition.effect)
        self.assertEqual(ToolApprovalMode.AUTO, definition.approval_mode)
        self.assertEqual("object", definition.input_schema["type"])

    def test_write_and_external_actions_require_approval(self) -> None:
        # 与 pi 对齐：LOCAL_WRITE（写入已绑定工作区）可自动执行，不再强制确认。
        # EXTERNAL_ACTION（外壳/网络/删除等越出工作区副作用）仍需显式确认。
        auto_write = ToolDefinition(
            name="auto_write",
            description="写入绑定工作区文件。",
            input_schema={"type": "object"},
            effect=ToolEffect.LOCAL_WRITE,
            approval_mode=ToolApprovalMode.AUTO,
        )
        self.assertEqual(ToolApprovalMode.AUTO, auto_write.approval_mode)

        for effect in (ToolEffect.EXTERNAL_ACTION,):
            with self.assertRaises(ToolValidationError) as raised:
                ToolDefinition(
                    name="unsafe_action",
                    description="执行具有副作用的操作。",
                    input_schema={"type": "object"},
                    effect=effect,
                    approval_mode=ToolApprovalMode.AUTO,
                )
            self.assertEqual("approval_required", raised.exception.code)

            approved = ToolDefinition(
                name="approved_action",
                description="执行用户明确确认的操作。",
                input_schema={"type": "object"},
                effect=effect,
                approval_mode=ToolApprovalMode.REQUIRED,
            )
            self.assertEqual(ToolApprovalMode.REQUIRED, approved.approval_mode)

    def test_definition_accepts_draft_2020_12_schema_keywords(self) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {
                "non_empty_path": {
                    "type": "string",
                    "minLength": 1,
                }
            },
            "type": "object",
            "properties": {
                "path": {"$ref": "#/$defs/non_empty_path"},
                "mode": {"oneOf": [{"const": "quick"}, {"const": "full"}]},
            },
            "required": ["path", "mode"],
            "additionalProperties": False,
        }

        definition = ToolDefinition(
            name="advanced_schema",
            description="接受标准 JSON Schema 关键字。",
            input_schema=schema,
        )
        validate_tool_arguments(
            definition.input_schema,
            {"path": "notes.txt", "mode": "quick"},
        )

        with self.assertRaises(ToolSchemaError) as raised:
            validate_tool_arguments(
                definition.input_schema,
                {"path": "", "mode": "quick"},
            )
        self.assertTrue(str(raised.exception).startswith("$."))
        self.assertEqual("$.path", raised.exception.path)
        self.assertEqual("minLength", raised.exception.keyword)
        self.assertEqual(1, raised.exception.expected)

    def test_definition_rejects_invalid_name_schema_and_limits(self) -> None:
        invalid_cases = (
            {"name": "File.Read", "input_schema": {"type": "object"}},
            {"name": "read_file", "input_schema": {"type": "string"}},
            {
                "name": "read_file",
                "input_schema": {"type": "object", "oneOf": []},
            },
            {"name": "read_file", "input_schema": {"type": "object"}, "timeout_seconds": 0},
        )
        for values in invalid_cases:
            with self.assertRaises(ToolValidationError):
                ToolDefinition(
                    description="invalid",
                    **values,
                )

    def test_tool_call_has_stable_canonical_arguments(self) -> None:
        call = ToolCall(
            id="tool_call_1",
            conversation_id="conv_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            tool_name="read_uploaded_file",
            arguments={"line": 3, "file_id": "file_1"},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-22T00:00:00.000Z",
        )

        self.assertEqual(
            '{"file_id":"file_1","line":3}',
            call.canonical_arguments_json,
        )
        with self.assertRaises(TypeError):
            call.arguments["file_id"] = "changed"

    def test_tool_call_exposes_typed_arguments_without_coercion(self) -> None:
        call = ToolCall(
            id="tool_call_1",
            conversation_id="conv_1",
            turn_id="turn_1",
            response_variant_id="variant_1",
            tool_name="read_uploaded_file",
            arguments={"file_id": "file_1", "start_line": 3},
            status=ToolCallStatus.CREATED,
            created_at="2026-08-22T00:00:00.000Z",
        )

        self.assertEqual("file_1", call.require_argument("file_id", str))
        self.assertEqual(3, call.optional_argument("start_line", int, 1))
        self.assertEqual(120, call.optional_argument("line_count", int, 120))
        with self.assertRaises(ToolValidationError):
            call.require_argument("missing", str)
        with self.assertRaises(ToolValidationError):
            call.require_argument("start_line", str)

    def test_tool_result_accepts_any_json_structured_content(self) -> None:
        values = (
            {"ok": True, "nested": [1, None]},
            ["first", 2, False],
            "plain text",
            42,
            3.5,
            True,
            None,
        )

        for value in values:
            with self.subTest(value=value):
                result = ToolResult(
                    tool_call_id="tool_call_1",
                    content="ok",
                    structured_content=value,
                )
                if isinstance(value, dict):
                    self.assertEqual(value, dict(result.structured_content))
                else:
                    self.assertEqual(value, result.structured_content)

        for invalid in ({"not-json"}, float("nan"), {1: "non-text key"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ToolValidationError):
                    ToolResult(
                        tool_call_id="tool_call_1",
                        content="ok",
                        structured_content=invalid,
                    )

    def test_approval_request_never_requires_raw_arguments(self) -> None:
        request = ApprovalRequest(
            id="approval_1",
            tool_call_id="tool_call_1",
            summary="将结果写入本地文件",
            reason="这项操作会修改用户文件，需要确认。",
            created_at="2026-08-22T00:00:00.000Z",
            metadata={"pathLabel": "notes.md"},
        )
        self.assertEqual(ApprovalStatus.PENDING, request.status)
        self.assertNotIn("arguments", request.metadata)

        with self.assertRaises(ToolValidationError):
            ApprovalRequest(
                id="approval_2",
                tool_call_id="tool_call_2",
                summary="已批准",
                reason="状态不一致",
                status=ApprovalStatus.APPROVED,
                created_at="2026-08-22T00:00:00.000Z",
            )
        with self.assertRaises(ToolValidationError) as unsafe:
            ApprovalRequest(
                id="approval_3",
                tool_call_id="tool_call_3",
                summary="执行操作",
                reason="需要确认",
                created_at="2026-08-22T00:00:00.000Z",
                metadata={"display": {"raw_arguments": {"secret": "value"}}},
            )
        self.assertEqual("unsafe_approval_metadata", unsafe.exception.code)

        with self.assertRaises(ToolValidationError) as unsafe_prompt:
            ToolApprovalPrompt(
                summary="执行操作",
                reason="需要确认",
                metadata={"arguments": {"secret": "value"}},
            )
        self.assertEqual("unsafe_approval_metadata", unsafe_prompt.exception.code)

    def test_registry_is_unique_and_sorted(self) -> None:
        registry = ToolRegistry()
        second = StubTool(read_definition("read_second_file"))
        first = StubTool(read_definition("read_first_file"))
        registry.register(second)
        registry.register(first)

        self.assertEqual(
            ["read_first_file", "read_second_file"],
            [item.name for item in registry.definitions()],
        )
        self.assertIs(first, registry.resolve("read_first_file"))
        with self.assertRaises(ToolValidationError) as duplicate:
            registry.register(first)
        self.assertEqual("duplicate_tool", duplicate.exception.code)
        with self.assertRaises(ToolValidationError) as unknown:
            registry.resolve("missing_tool")
        self.assertEqual("unknown_tool", unknown.exception.code)

    def test_activity_copy_is_short_and_single_line(self) -> None:
        activity = ToolActivityCopy(
            running="正在读取\n需求文档",
            completed="已读取需求文档",
            failed="读取失败，可以重试",
        )

        self.assertEqual("正在读取 需求文档", activity.running)
        with self.assertRaises(ToolValidationError):
            ToolActivityCopy(
                running="x" * 161,
                completed="完成",
                failed="失败",
            )

    def test_tool_error_exposes_only_normalized_safe_fields(self) -> None:
        error = ToolError(
            "file_not_found",
            "找不到已授权的文件。",
            retryable=False,
            correlation_id="corr_1",
            path="$.file_id",
            keyword="required",
            expected=["file_id"],
        )

        self.assertEqual("file_not_found", error.code)
        self.assertEqual("找不到已授权的文件。", str(error))
        self.assertFalse(error.retryable)
        self.assertEqual("corr_1", error.correlation_id)
        self.assertEqual(
            {
                "path": "$.file_id",
                "keyword": "required",
                "expected": ["file_id"],
            },
            dict(error.failure.details),
        )
        self.assertFalse(hasattr(error, "raw_error"))

    def test_tool_result_can_return_normalized_failure(self) -> None:
        failure = ToolCallError(
            code="temporary_unavailable",
            safe_message="工具暂时不可用。",
            retryable=True,
            correlation_id="corr_tool_1",
        )

        result = ToolResult.failed(tool_call_id="call_1", error=failure)

        self.assertIs(failure, result.error)
        self.assertEqual("工具暂时不可用。", result.content)
        with self.assertRaises(ToolValidationError):
            ToolResult(
                tool_call_id="call_1",
                content="invalid",
                error=failure,
                terminate=True,
            )


if __name__ == "__main__":
    unittest.main()
