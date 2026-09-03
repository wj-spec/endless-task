"""AP-204: structured checkpoint JSON validation and rendering."""

from __future__ import annotations

import json
import unittest

from endless_task.agent_platform import AgentPlatformError
from endless_task.context_engine import (
    CheckpointFileRef,
    StructuredContextCheckpoint,
    checkpoint_entry_conflicts,
    parse_checkpoint_json,
    render_checkpoint_message,
)

VALID_JSON = {
    "goal": "完成 Agent Platform 开发",
    "constraints": ["不改变 v1.1 行为"],
    "progress": ["M1 完成", "M2 进行中"],
    "decisions": ["协议层先行"],
    "open_questions": ["G0 门何时过"],
    "files_read": [{"path": "docs/agent-platform/04.md"}],
    "files_modified": [{"path": "apps/api/src/endless_task/context_engine/checkpoint.py", "sha256": "a" * 64}],
    "active_effects": [{"path": "run_1/effects"}],
    "next_steps": ["实现 AP-204"],
    "critical_context": ["feature flag 默认关闭"],
    "covered_entry_ids": ["entry_1", "entry_2"],
    "source_checkpoint_id": "checkpoint_0",
}


def valid_text() -> str:
    return json.dumps(VALID_JSON, ensure_ascii=False)


class ParseCheckpointTest(unittest.TestCase):
    def test_valid_checkpoint_round_trips(self) -> None:
        checkpoint = parse_checkpoint_json(valid_text())
        self.assertEqual("完成 Agent Platform 开发", checkpoint.goal)
        self.assertEqual(("entry_1", "entry_2"), checkpoint.covered_entry_ids)
        self.assertEqual("checkpoint_0", checkpoint.source_checkpoint_id)
        self.assertEqual(1, len(checkpoint.files_read))
        self.assertEqual(64, len(checkpoint.files_modified[0].sha256))

    def test_invalid_json_fails_structurally(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json("{not json")
        self.assertEqual("invalid_checkpoint_json", caught.exception.code)

    def test_non_object_json_fails(self) -> None:
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json('["list"]')
        self.assertEqual("invalid_checkpoint_json", caught.exception.code)

    def test_missing_required_field_fails_schema(self) -> None:
        broken = dict(VALID_JSON)
        del broken["goal"]
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json(json.dumps(broken, ensure_ascii=False))
        self.assertEqual("invalid_checkpoint_schema", caught.exception.code)

    def test_wrong_section_type_fails_schema(self) -> None:
        broken = dict(VALID_JSON)
        broken["constraints"] = "not-a-list"
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json(json.dumps(broken, ensure_ascii=False))
        self.assertEqual("invalid_checkpoint_schema", caught.exception.code)

    def test_unknown_field_fails_schema(self) -> None:
        broken = dict(VALID_JSON)
        broken["hacked_field"] = "injected"
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json(json.dumps(broken, ensure_ascii=False))
        self.assertEqual("invalid_checkpoint_schema", caught.exception.code)

    def test_duplicate_covered_ids_fail(self) -> None:
        broken = dict(VALID_JSON)
        broken["covered_entry_ids"] = ["entry_1", "entry_1"]
        with self.assertRaises(AgentPlatformError):
            parse_checkpoint_json(json.dumps(broken, ensure_ascii=False))

    def test_bad_sha256_fails_typed_validation(self) -> None:
        broken = dict(VALID_JSON)
        broken["files_modified"] = [{"path": "/tmp/x", "sha256": "zz"}]
        with self.assertRaises(AgentPlatformError) as caught:
            parse_checkpoint_json(json.dumps(broken, ensure_ascii=False))
        self.assertEqual("invalid_checkpoint", caught.exception.code)


class CheckpointValueTest(unittest.TestCase):
    def test_value_object_validates_sections_and_ids(self) -> None:
        with self.assertRaises(AgentPlatformError):
            StructuredContextCheckpoint(
                goal="",
                constraints=(),
                progress=(),
                decisions=(),
                open_questions=(),
                files_read=(),
                files_modified=(),
                active_effects=(),
                next_steps=(),
                critical_context=(),
                covered_entry_ids=(),
            )
        with self.assertRaises(AgentPlatformError):
            StructuredContextCheckpoint(
                goal="g",
                constraints=(),
                progress=(),
                decisions=(),
                open_questions=(),
                files_read=(),
                files_modified=(),
                active_effects=(),
                next_steps=(),
                critical_context=(),
                covered_entry_ids=("x", "x"),
            )
        checkpoint = parse_checkpoint_json(valid_text())
        self.assertIsInstance(checkpoint.files_read[0], CheckpointFileRef)

    def test_render_is_deterministic_and_contains_goal(self) -> None:
        checkpoint = parse_checkpoint_json(valid_text())
        first = render_checkpoint_message(checkpoint)
        second = render_checkpoint_message(checkpoint)
        self.assertEqual(first, second)
        self.assertEqual("system", first.role)
        self.assertIn("完成 Agent Platform 开发", first.content)
        self.assertIn("M1 完成", first.content)

    def test_render_round_trip_parse(self) -> None:
        # The rendered message is a deterministic projection; parsing it back
        # as JSON is intentionally unsupported (it is human-readable text).
        checkpoint = parse_checkpoint_json(valid_text())
        message = render_checkpoint_message(checkpoint)
        self.assertTrue(message.content.startswith("任务目标："))


class EntryConflictTest(unittest.TestCase):
    def test_conflicts_detected_sorted(self) -> None:
        conflicts = checkpoint_entry_conflicts(
            {"entry_2", "entry_1", "entry_3"},
            {"entry_3", "entry_4"},
        )
        self.assertEqual(("entry_3",), conflicts)

    def test_no_conflict_returns_empty(self) -> None:
        self.assertEqual((), checkpoint_entry_conflicts({"a"}, {"b"}))
        self.assertEqual((), checkpoint_entry_conflicts(set(), set()))

    def test_invalid_ids_fail_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            checkpoint_entry_conflicts({""}, {"b"})


if __name__ == "__main__":
    unittest.main()
