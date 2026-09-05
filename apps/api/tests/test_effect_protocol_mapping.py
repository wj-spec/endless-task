"""RS-6 slice 2b: workspace receipt -> protocol receipt mapping."""

from __future__ import annotations

import unittest

from endless_task.workspace_runtime.effect_log import EffectReceipt as WorkspaceReceipt
from endless_task.workspace_runtime.effect_protocol import to_protocol_fields


class EffectProtocolMappingTest(unittest.TestCase):
    def test_committed_file_write(self) -> None:
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="file_write",
                path="/ws/a.txt",
                sha256="abc",
                executed_at="2026-09-05T00:00:00Z",
            ),
            effect_id="fx_1",
            tool_call_id="call_1",
            backend="workspace_tool",
        )
        self.assertEqual("file_write", fields["effect_type"])
        self.assertEqual("committed", fields["outcome"])
        self.assertIsNotNone(fields["committed_at"])
        self.assertNotIn("sha256", fields)  # protocol has no content hash

    def test_committed_file_delete(self) -> None:
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="file_delete",
                path="/ws/gone.txt",
                executed_at="2026-09-05T00:00:00Z",
            ),
            effect_id="fx_2",
            tool_call_id="call_2",
            backend="workspace_tool",
        )
        self.assertEqual("file_delete", fields["effect_type"])
        self.assertEqual("committed", fields["outcome"])

    def test_unknown_outcome_maps_to_unknown_without_committed_at(self) -> None:
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="shell",
                path="rm -rf /tmp/x",
                exit_code=0,
                executed_at="2026-09-05T00:00:00Z",
                unknown_outcome=True,
            ),
            effect_id="fx_3",
            tool_call_id="call_3",
            backend="workspace_tool",
        )
        self.assertEqual("process", fields["effect_type"])
        self.assertEqual("unknown", fields["outcome"])
        self.assertIsNone(fields["committed_at"])

    def test_shell_timeout_no_exit_code_is_unknown(self) -> None:
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="shell",
                path="sleep 300",
                exit_code=None,
                timed_out=True,
                executed_at="2026-09-05T00:00:00Z",
            ),
            effect_id="fx_4",
            tool_call_id="call_4",
            backend="workspace_tool",
        )
        self.assertEqual("unknown", fields["outcome"])

    def test_shell_nonzero_exit_is_committed_business_failure(self) -> None:
        # A non-zero exit means the shell ran (committed); the business
        # failure is caller-visible via exit_code, not an unknown outcome.
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="shell",
                path="grep x",
                exit_code=1,
                executed_at="2026-09-05T00:00:00Z",
            ),
            effect_id="fx_5",
            tool_call_id="call_5",
            backend="workspace_tool",
        )
        self.assertEqual("committed", fields["outcome"])
        self.assertIsNotNone(fields["committed_at"])

    def test_unknown_kind_passes_through(self) -> None:
        fields = to_protocol_fields(
            WorkspaceReceipt(
                kind="custom_op",
                path="/x",
                executed_at="2026-09-05T00:00:00Z",
            ),
            effect_id="fx_6",
            tool_call_id="call_6",
            backend="workspace_tool",
        )
        self.assertEqual("custom_op", fields["effect_type"])


if __name__ == "__main__":
    unittest.main()
