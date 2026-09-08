"""C2 失败记忆：循环的外部状态（试过什么、为什么失败）。"""

from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from typing import Optional

from endless_task.runtime_v2 import ToolExecutionStatus
from endless_task.runtime_v2.failure_memory import (
    FailureMemory,
    FailureMemoryAccumulator,
    build_failure_memory,
)


@dataclass
class _Execution:
    """与 ToolExecutionRecord 同形的测试替身（字段名保持一致）。"""

    tool_name: str = "read_file"
    status: ToolExecutionStatus = ToolExecutionStatus.FAILED
    error_code: Optional[str] = "temporary_unavailable"
    safe_message: Optional[str] = "工具暂时不可用。"
    retryable: Optional[bool] = True
    model_turn_id: Optional[str] = "turn_1"
    id: str = "exec_1"
    arguments_hash: str = "hash_1"


def _failed(
    tool_name: str = "read_file",
    *,
    error_code: str = "temporary_unavailable",
    execution_id: str = "exec_1",
    turn_id: str = "turn_1",
    arguments_hash: str = "hash_1",
) -> _Execution:
    return _Execution(
        tool_name=tool_name,
        error_code=error_code,
        model_turn_id=turn_id,
        id=execution_id,
        arguments_hash=arguments_hash,
    )


def _succeeded(tool_name: str = "read_file") -> _Execution:
    return _Execution(
        tool_name=tool_name,
        status=ToolExecutionStatus.COMPLETED,
        error_code=None,
        safe_message=None,
        retryable=None,
    )


class FailureMemoryBuildTest(unittest.TestCase):
    def test_empty_history_has_no_attempts(self) -> None:
        memory = build_failure_memory(())
        self.assertEqual(0, memory.total)
        self.assertFalse(memory.is_repeating)
        self.assertEqual("", memory.guidance())

    def test_completed_executions_are_not_attempts(self) -> None:
        memory = build_failure_memory((_succeeded(), _succeeded("run_shell")))
        self.assertEqual(0, memory.total)
        self.assertFalse(memory.is_repeating)

    def test_single_failure_is_recorded_but_not_repeating(self) -> None:
        memory = build_failure_memory((_failed(),))
        self.assertEqual(1, memory.total)
        self.assertEqual("read_file", memory.attempts[0].tool_name)
        self.assertEqual("temporary_unavailable", memory.attempts[0].error_code)
        self.assertEqual("工具暂时不可用。", memory.attempts[0].safe_message)
        self.assertEqual(1, memory.attempts[0].attempt)
        self.assertFalse(memory.is_repeating)

    def test_same_tool_and_error_twice_is_repeating(self) -> None:
        memory = build_failure_memory(
            (
                _failed(execution_id="exec_1", turn_id="turn_1"),
                _failed(execution_id="exec_2", turn_id="turn_2"),
            )
        )
        self.assertTrue(memory.is_repeating)
        self.assertEqual(1, len(memory.repeated))
        self.assertEqual("read_file", memory.repeated[0].tool_name)
        self.assertEqual("temporary_unavailable", memory.repeated[0].error_code)
        self.assertEqual(2, memory.repeated[0].count)
        self.assertEqual(2, memory.attempts[-1].attempt)

    def test_different_error_code_does_not_join_the_same_streak(self) -> None:
        memory = build_failure_memory(
            (
                _failed(error_code="temporary_unavailable"),
                _failed(error_code="permission_denied", execution_id="exec_2"),
            )
        )
        self.assertFalse(memory.is_repeating)

    def test_success_of_same_tool_resets_its_streak(self) -> None:
        memory = build_failure_memory(
            (
                _failed(execution_id="exec_1"),
                _failed(execution_id="exec_2"),
                _succeeded(),
                _failed(execution_id="exec_3"),
            )
        )
        self.assertFalse(memory.is_repeating)
        self.assertEqual(3, memory.total)
        self.assertEqual(1, memory.attempts[-1].attempt)

    def test_success_of_other_tool_keeps_streak(self) -> None:
        memory = build_failure_memory(
            (
                _failed(execution_id="exec_1"),
                _succeeded("run_shell"),
                _failed(execution_id="exec_2"),
            )
        )
        self.assertTrue(memory.is_repeating)

    def test_digest_keeps_only_recent_attempts(self) -> None:
        records = tuple(
            _failed(execution_id=f"exec_{index}", turn_id=f"turn_{index}")
            for index in range(7)
        )
        memory = build_failure_memory(records)
        digest = memory.digest(limit=3)
        self.assertEqual(3, len(digest))
        self.assertEqual("exec_6", digest[-1]["toolExecutionId"])
        self.assertEqual("exec_4", digest[0]["toolExecutionId"])

    def test_guidance_names_the_tool_and_forbids_repeating(self) -> None:
        memory = build_failure_memory((_failed(), _failed(execution_id="exec_2")))
        guidance = memory.guidance()
        self.assertIn("read_file", guidance)
        self.assertIn("temporary_unavailable", guidance)
        self.assertIn("不要原样重复", guidance)

    def test_as_json_is_json_serializable(self) -> None:
        memory = build_failure_memory((_failed(), _failed(execution_id="exec_2")))
        payload = memory.as_json()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("repeated", encoded)
        self.assertEqual(2, payload["total"])

    def test_unknown_object_is_ignored(self) -> None:
        # 没有状态也没有错误码的记录不算失败尝试，且不应抛异常。
        memory = build_failure_memory((object(),))
        self.assertEqual(0, memory.total)
        self.assertFalse(memory.is_repeating)

    def test_failure_without_error_code_still_recorded(self) -> None:
        record = _Execution(error_code=None, safe_message=None)
        memory = build_failure_memory((record,))
        self.assertEqual(1, memory.total)
        self.assertEqual("tool_error", memory.attempts[0].error_code)


class FailureMemoryAccumulatorTest(unittest.TestCase):
    def test_accumulator_matches_batch_build(self) -> None:
        records = (
            _failed(execution_id="exec_1"),
            _failed(execution_id="exec_2"),
            _succeeded("run_shell"),
        )
        accumulator = FailureMemoryAccumulator()
        for record in records:
            accumulator.record(record)
        memory = accumulator.snapshot
        self.assertEqual(build_failure_memory(records), memory)

    def test_restore_rebuilds_from_history(self) -> None:
        records = (_failed(execution_id="exec_1"), _failed(execution_id="exec_2"))
        accumulator = FailureMemoryAccumulator()
        memory = accumulator.restore(records)
        self.assertTrue(memory.is_repeating)
        self.assertEqual(2, memory.total)

    def test_repeat_threshold_is_configurable(self) -> None:
        accumulator = FailureMemoryAccumulator(repeat_threshold=3)
        accumulator.record(_failed(execution_id="exec_1"))
        self.assertFalse(accumulator.snapshot.is_repeating)
        accumulator.record(_failed(execution_id="exec_2"))
        self.assertFalse(accumulator.snapshot.is_repeating)
        accumulator.record(_failed(execution_id="exec_3"))
        self.assertTrue(accumulator.snapshot.is_repeating)


if __name__ == "__main__":
    unittest.main()
