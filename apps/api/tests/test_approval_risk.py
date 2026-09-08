import unittest

from endless_task.runtime_v2.gateway import (
    PendingApproval,
    _approval_json,
    derive_approval_risk,
)


class DeriveApprovalRiskTest(unittest.TestCase):
    def test_effect_maps_to_risk(self) -> None:
        self.assertEqual("low", derive_approval_risk("read_only", "read_text_file"))
        self.assertEqual("medium", derive_approval_risk("local_write", "write_workspace_file"))
        self.assertEqual("high", derive_approval_risk("external_action", "run_shell"))

    def test_destructive_tool_name_is_high(self) -> None:
        self.assertEqual("high", derive_approval_risk("local_write", "delete_workspace_file"))
        self.assertEqual("high", derive_approval_risk("local_write", "remove_file"))
        self.assertEqual("high", derive_approval_risk("", "drop_table"))

    def test_defaults_to_low(self) -> None:
        self.assertEqual("low", derive_approval_risk("", ""))


class ApprovalJsonRiskTest(unittest.TestCase):
    def _approval(self, **overrides) -> PendingApproval:
        return PendingApproval(
            approval_id="a1",
            run_id="r1",
            model_turn_id="mt1",
            tool_execution_id="te1",
            tool_name=overrides.pop("tool_name", "write_workspace_file"),
            summary="执行写入？",
            reason="会修改本机数据。",
            metadata=overrides.pop("metadata", {}),
        )

    def test_risk_from_metadata(self) -> None:
        payload = _approval_json(
            self._approval(metadata={"effect": "external_action", "risk": "high"})
        )
        self.assertEqual("high", payload["risk"])
        self.assertEqual("external_action", payload["effect"])

    def test_risk_derived_when_missing(self) -> None:
        payload = _approval_json(
            self._approval(tool_name="delete_workspace_file", metadata={"effect": "local_write"})
        )
        self.assertEqual("high", payload["risk"])


if __name__ == "__main__":
    unittest.main()
