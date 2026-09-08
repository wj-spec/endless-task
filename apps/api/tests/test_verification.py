"""C1 制造者—检查者分离：独立验证环节（纯逻辑）。"""

from __future__ import annotations

import json
import unittest

from endless_task.runtime_v2.verification import (
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_UNCERTAIN,
    VerificationVerdict,
    build_verifier_messages,
    parse_verdict,
    should_verify,
    verifier_system_prompt,
)


class VerifierPromptTest(unittest.TestCase):
    def test_system_prompt_is_independent_and_strict(self) -> None:
        prompt = verifier_system_prompt()
        self.assertIn("验证", prompt)
        self.assertIn("JSON", prompt)
        self.assertIn("verdict", prompt)

    def test_messages_carry_only_goal_candidate_and_evidence(self) -> None:
        messages = build_verifier_messages(
            goal="把报告写进 report.md",
            candidate="已完成，report.md 已写入。",
            evidence=("write_file report.md（local_write）",),
        )
        self.assertEqual(2, len(messages))
        self.assertEqual("system", messages[0].role)
        self.assertIn("验证", messages[0].content)
        self.assertEqual("user", messages[1].role)
        self.assertIn("把报告写进 report.md", messages[1].content)
        self.assertIn("已完成，report.md 已写入。", messages[1].content)
        self.assertIn("write_file report.md", messages[1].content)

    def test_messages_do_not_leak_maker_history(self) -> None:
        # 验证器只看到目标/产出/证据，不共享制造者的对话历史（避免"自证"）。
        messages = build_verifier_messages(
            goal="目标",
            candidate="产出",
            evidence=(),
        )
        self.assertEqual(2, len(messages))
        self.assertNotIn("assistant", [message.role for message in messages])
        self.assertNotIn("tool", [message.role for message in messages])

    def test_long_candidate_is_truncated(self) -> None:
        messages = build_verifier_messages(
            goal="目标",
            candidate="x" * 5000,
            max_candidate_characters=100,
        )
        self.assertIn("已截断", messages[1].content)
        self.assertLess(len(messages[1].content), 1000)


class ParseVerdictTest(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        verdict = parse_verdict(
            json.dumps(
                {
                    "verdict": "fail",
                    "reasons": ["报告缺少结论"],
                    "missing": ["结论"],
                },
                ensure_ascii=False,
            ),
            model="verifier-model",
        )
        self.assertEqual(VERDICT_FAIL, verdict.verdict)
        self.assertEqual(("报告缺少结论",), verdict.reasons)
        self.assertEqual(("结论",), verdict.missing)
        self.assertEqual("verifier-model", verdict.model)

    def test_parses_json_inside_prose_or_fence(self) -> None:
        text = '结论如下：\n```json\n{"verdict": "pass", "reasons": []}\n```\n'
        verdict = parse_verdict(text)
        self.assertEqual(VERDICT_PASS, verdict.verdict)
        self.assertEqual((), verdict.reasons)

    def test_unknown_verdict_falls_back_to_uncertain(self) -> None:
        verdict = parse_verdict('{"verdict": "maybe", "reasons": ["看情况"]}')
        self.assertEqual(VERDICT_UNCERTAIN, verdict.verdict)
        self.assertEqual(("看情况",), verdict.reasons)

    def test_unparsable_output_is_uncertain_with_reason(self) -> None:
        verdict = parse_verdict("我不确定。")
        self.assertEqual(VERDICT_UNCERTAIN, verdict.verdict)
        self.assertTrue(verdict.reasons)
        self.assertIn("无法解析", verdict.reasons[0])

    def test_empty_output_is_uncertain(self) -> None:
        verdict = parse_verdict("   ")
        self.assertEqual(VERDICT_UNCERTAIN, verdict.verdict)

    def test_non_string_entries_are_filtered(self) -> None:
        verdict = parse_verdict(
            '{"verdict": "fail", "reasons": [1, "理由", null], "missing": "x"}'
        )
        self.assertEqual(("理由",), verdict.reasons)
        # 单个字符串按一条处理（宽容解析），数组里的非字符串条目丢弃。
        self.assertEqual(("x",), verdict.missing)

    def test_json_serializable(self) -> None:
        verdict = VerificationVerdict(
            verdict=VERDICT_FAIL,
            reasons=("理由",),
            missing=("缺少",),
            model="m",
            latency_ms=12,
            input_tokens=3,
            output_tokens=4,
        )
        payload = verdict.as_json()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertIn("理由", encoded)
        self.assertEqual("fail", payload["verdict"])
        self.assertEqual(12, payload["latencyMs"])


class ShouldVerifyTest(unittest.TestCase):
    def test_off_mode_never_verifies(self) -> None:
        self.assertFalse(
            should_verify("0", has_side_effects=True, candidate="答案")
        )

    def test_on_mode_verifies_any_answer(self) -> None:
        self.assertTrue(should_verify("1", has_side_effects=False, candidate="答案"))

    def test_side_effects_mode_only_verifies_effectful_runs(self) -> None:
        self.assertFalse(
            should_verify("side_effects", has_side_effects=False, candidate="答案")
        )
        self.assertTrue(
            should_verify("side_effects", has_side_effects=True, candidate="答案")
        )

    def test_empty_candidate_is_never_verified(self) -> None:
        self.assertFalse(should_verify("1", has_side_effects=True, candidate="  "))

    def test_unknown_mode_fails_loudly(self) -> None:
        with self.assertRaises(Exception):
            should_verify("maybe", has_side_effects=True, candidate="答案")


if __name__ == "__main__":
    unittest.main()
