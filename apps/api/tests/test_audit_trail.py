"""A8 决策解释 / 审计轨迹（纯逻辑）。"""

from __future__ import annotations

import json
import unittest

from endless_task.runtime_v2.audit_trail import (
    AuditFact,
    build_audit_trail,
    counterfactual_for,
    severity_for,
)


def _fact(**kwargs) -> AuditFact:
    base = {
        "fact_id": "f1",
        "kind": "tool",
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "title": "读取文件",
    }
    base.update(kwargs)
    return AuditFact(**base)


class BuildTrailTest(unittest.TestCase):
    def test_entries_are_ordered_by_time(self) -> None:
        facts = (
            _fact(fact_id="b", occurred_at="2026-01-01T00:00:02+00:00", title="后"),
            _fact(fact_id="a", occurred_at="2026-01-01T00:00:01+00:00", title="先"),
        )
        entries = build_audit_trail(facts)
        self.assertEqual(["a", "b"], [entry.fact_id for entry in entries])

    def test_empty_input_is_empty(self) -> None:
        self.assertEqual((), build_audit_trail(()))

    def test_write_tool_gets_rationale_and_counterfactual(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="tool",
                    tool_name="write_workspace_file",
                    effect="local_write",
                    title="写入工作区文件",
                ),
            )
        )
        entry = entries[0]
        self.assertEqual("warning", entry.severity)
        self.assertIn("工作区文件", entry.rationale)
        self.assertIn("撤销", entry.counterfactual)

    def test_read_only_tool_is_informational(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    tool_name="read_workspace_file",
                    effect="read_only",
                    title="读取文件",
                ),
            )
        )
        self.assertEqual("info", entries[0].severity)
        self.assertIn("只读", entries[0].rationale)

    def test_denied_approval_is_critical_with_reason(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="approval",
                    decision="deny",
                    tool_name="delete_workspace_file",
                    effect="local_write",
                    risk="high",
                    title="审批被拒绝",
                    summary="允许删除吗？",
                ),
            )
        )
        entry = entries[0]
        self.assertEqual("critical", entry.severity)
        self.assertIn("拒绝", entry.rationale)
        self.assertIn("如果你当时批准", entry.counterfactual)

    def test_modified_approval_mentions_parameter_change(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="approval",
                    decision="modify",
                    tool_name="write_workspace_file",
                    effect="local_write",
                    title="审批改为修改参数",
                ),
            )
        )
        self.assertIn("修改", entries[0].rationale)

    def test_escalation_carries_reason_and_options(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="escalation",
                    title="需要你决定下一步",
                    payload={
                        "reason": "no_progress",
                        "summary": "连续 4 轮没有实质进展。",
                        "options": ["continue", "change_approach", "take_over"],
                    },
                ),
            )
        )
        entry = entries[0]
        self.assertEqual("warning", entry.severity)
        self.assertIn("没有实质进展", entry.rationale)
        self.assertEqual(3, len(entry.options))

    def test_verification_entry_reports_uncertainty(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="verification",
                    title="独立验证未通过",
                    payload={
                        "verdict": "fail",
                        "reasons": ["报告未写入"],
                        "missing": ["report.md"],
                    },
                ),
            )
        )
        entry = entries[0]
        self.assertEqual("critical", entry.severity)
        self.assertIn("独立验证", entry.rationale)
        self.assertIn("报告未写入", entry.rationale)
        self.assertIn("复核", entry.uncertainty)

    def test_uncertain_verdict_is_warning(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="verification",
                    payload={"verdict": "uncertain", "reasons": ["无法解析"]},
                    title="独立验证：不确定",
                ),
            )
        )
        self.assertEqual("warning", entries[0].severity)
        self.assertIn("不确定", entries[0].uncertainty)

    def test_undo_entry_explains_rollback(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="undo",
                    title="撤销文件写入",
                    summary="覆盖了文件 notes.md",
                    payload={"target": "notes.md", "kind": "file_write"},
                ),
            )
        )
        entry = entries[0]
        self.assertIn("回滚", entry.rationale)
        self.assertIn("如果不撤销", entry.counterfactual)

    def test_failed_tool_is_critical_with_error_code(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    tool_name="run_shell",
                    effect="external_action",
                    error_code="tool_timeout",
                    title="工具执行失败",
                ),
            )
        )
        entry = entries[0]
        self.assertEqual("critical", entry.severity)
        self.assertIn("tool_timeout", entry.rationale)

    def test_entries_are_json_serializable(self) -> None:
        entries = build_audit_trail(
            (
                _fact(
                    kind="approval",
                    decision="approve",
                    tool_name="write_workspace_file",
                    effect="local_write",
                    risk="medium",
                    title="审批通过",
                ),
            )
        )
        payload = [entry.as_json() for entry in entries]
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("rationale", encoded)
        self.assertEqual("approval", payload[0]["kind"])

    def test_unknown_kind_stays_readable(self) -> None:
        entries = build_audit_trail((_fact(kind="mystery", title="未知事件"),))
        self.assertEqual("info", entries[0].severity)
        self.assertTrue(entries[0].rationale)


class SeverityTest(unittest.TestCase):
    def test_severity_ordering(self) -> None:
        self.assertEqual(
            "critical",
            severity_for(_fact(kind="approval", decision="deny")),
        )
        self.assertEqual(
            "warning",
            severity_for(_fact(effect="local_write")),
        )
        self.assertEqual("info", severity_for(_fact(effect="read_only")))


class CounterfactualTest(unittest.TestCase):
    def test_read_only_has_no_counterfactual(self) -> None:
        self.assertEqual(
            "",
            counterfactual_for(_fact(effect="read_only", kind="tool")),
        )

    def test_write_counterfactual_mentions_undo(self) -> None:
        text = counterfactual_for(
            _fact(effect="local_write", kind="tool", tool_name="write_workspace_file")
        )
        self.assertIn("撤销", text)

    def test_denied_approval_counterfactual(self) -> None:
        text = counterfactual_for(
            _fact(kind="approval", decision="deny", tool_name="delete_workspace_file")
        )
        self.assertIn("批准", text)


if __name__ == "__main__":
    unittest.main()
