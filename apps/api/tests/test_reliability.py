"""M3A: retry classification/budgets and progress-aware stop policy."""

from __future__ import annotations

import unittest

from endless_task.agent_platform import AgentPlatformError, EffectOutcome, EffectReceipt, EffectReceiptRef
from endless_task.reliability import (
    Deadline,
    DefaultRetryClassifier,
    ErrorCategory,
    ProgressSignal,
    RetryBudget,
    RetryScope,
    StopLevel,
    StopPolicyProfile,
    consume_budget,
    evaluate_no_progress,
    tool_auto_retry_allowed,
)
from endless_task.tool_platform import IdempotencyPolicy


class RetryClassifierTest(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = DefaultRetryClassifier()

    def test_never_retry_categories(self) -> None:
        for code, reason_part in (
            ("authentication_failed", "authentication"),
            ("permission_denied", "policy_denied"),
            ("invalid_provider_response", "invalid_request"),
            ("path_not_found", "tool_invalid_input"),
            ("approval_denied", "policy_denied"),
            ("unknown_outcome", "unknown_outcome"),
        ):
            with self.subTest(code=code):
                decision = self.classifier.classify(code)
                self.assertFalse(decision.retryable, code)
                self.assertIn(reason_part, decision.reason)

    def test_retryable_provider_categories(self) -> None:
        decision = self.classifier.classify("network_error")
        self.assertTrue(decision.retryable)
        self.assertEqual(RetryScope.PROVIDER, decision.scope)
        self.assertTrue(decision.failover_allowed)
        self.assertTrue(decision.consumes_budget)

    def test_rate_limit_respects_retry_after_with_cap(self) -> None:
        decision = self.classifier.classify("rate_limited", retry_after_ms=5_000)
        self.assertTrue(decision.retryable)
        self.assertEqual(5.0, decision.delay_seconds)
        capped = self.classifier.classify("rate_limited", retry_after_ms=2_000_000)
        self.assertLessEqual(capped.delay_seconds, 30.0)

    def test_context_overflow_is_turn_scoped(self) -> None:
        decision = self.classifier.classify("context_too_large")
        self.assertTrue(decision.retryable)
        self.assertEqual(RetryScope.TURN, decision.scope)
        self.assertEqual("compact_then_retry", decision.reason)

    def test_unknown_code_fails_closed(self) -> None:
        decision = self.classifier.classify("some_unknown_code")
        self.assertFalse(decision.retryable)
        self.assertEqual("unknown_error_category", decision.reason)

    def test_tool_timeout_requires_idempotency_key(self) -> None:
        decision = self.classifier.classify("tool_timeout")
        self.assertTrue(decision.retryable)
        self.assertEqual(RetryScope.TOOL, decision.scope)
        self.assertTrue(decision.requires_idempotency_key)

    def test_custom_code_map_overrides_defaults(self) -> None:
        classifier = DefaultRetryClassifier(
            code_map={"my_transient": ErrorCategory.TRANSIENT_NETWORK}
        )
        self.assertTrue(classifier.classify("my_transient").retryable)
        self.assertFalse(classifier.classify("network_error").retryable)


class ToolAutoRetryGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = DefaultRetryClassifier()

    def test_safe_tool_auto_retries_transient(self) -> None:
        decision = self.classifier.classify("mcp_tool_failed")
        self.assertTrue(
            tool_auto_retry_allowed(
                idempotency=IdempotencyPolicy.SAFE,
                decision=decision,
            )
        )

    def test_unsafe_and_unknown_tools_never_auto_retry(self) -> None:
        decision = self.classifier.classify("mcp_tool_failed")
        for policy in (IdempotencyPolicy.UNSAFE, IdempotencyPolicy.UNKNOWN):
            with self.subTest(policy=policy):
                self.assertFalse(
                    tool_auto_retry_allowed(idempotency=policy, decision=decision)
                )

    def test_timeout_requires_idempotency_key_with_key_required_tool(self) -> None:
        decision = self.classifier.classify("tool_timeout")
        self.assertFalse(
            tool_auto_retry_allowed(
                idempotency=IdempotencyPolicy.KEY_REQUIRED,
                decision=decision,
                has_idempotency_key=False,
            )
        )
        self.assertTrue(
            tool_auto_retry_allowed(
                idempotency=IdempotencyPolicy.KEY_REQUIRED,
                decision=decision,
                has_idempotency_key=True,
            )
        )

    def test_non_retryable_decision_never_passes_gate(self) -> None:
        decision = self.classifier.classify("path_not_found")
        self.assertFalse(
            tool_auto_retry_allowed(
                idempotency=IdempotencyPolicy.SAFE,
                decision=decision,
            )
        )


class RetryBudgetTest(unittest.TestCase):
    def test_consume_counts_and_caps(self) -> None:
        budget = RetryBudget()
        decision = DefaultRetryClassifier().classify("network_error")
        budget, allowed = consume_budget(
            budget,
            decision,
            max_provider_attempts=2,
            max_tool_attempts=2,
            max_failovers=1,
            max_retry_delay=60.0,
        )
        self.assertTrue(allowed)
        self.assertEqual(1, budget.provider_request_attempts)
        self.assertEqual(1, budget.provider_failovers)
        self.assertEqual(1.0, budget.total_retry_delay)

    def test_attempt_cap_rejects(self) -> None:
        decision = DefaultRetryClassifier().classify("network_error")
        budget = RetryBudget(provider_request_attempts=2)
        _, allowed = consume_budget(
            budget,
            decision,
            max_provider_attempts=2,
            max_tool_attempts=2,
            max_failovers=1,
            max_retry_delay=60.0,
        )
        self.assertFalse(allowed)

    def test_deadline_respected(self) -> None:
        decision = DefaultRetryClassifier().classify("network_error")
        budget = RetryBudget()
        _, allowed = consume_budget(
            budget,
            decision,
            max_provider_attempts=5,
            max_tool_attempts=5,
            max_failovers=1,
            max_retry_delay=60.0,
            deadline=Deadline(deadline_monotonic=1.0),
            now=1.0,
        )
        self.assertFalse(allowed)

    def test_context_overflow_counts_compactions(self) -> None:
        decision = DefaultRetryClassifier().classify("context_too_large")
        budget = RetryBudget()
        budget, allowed = consume_budget(
            budget,
            decision,
            max_provider_attempts=5,
            max_tool_attempts=5,
            max_failovers=1,
            max_retry_delay=60.0,
        )
        self.assertTrue(allowed)
        self.assertEqual(1, budget.context_overflow_compactions)

    def test_invalid_budget_fails_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            RetryBudget(provider_request_attempts=-1)
        with self.assertRaises(AgentPlatformError):
            Deadline(deadline_monotonic="soon")


class StopPolicyTest(unittest.TestCase):
    PROFILE = StopPolicyProfile(remind_after=1, restrict_after=2, stop_after=3)

    def signal(self, *, tool=None, outcome=None, error=None, fp="ctx_1", artifacts=0) -> ProgressSignal:
        return ProgressSignal(
            context_fingerprint=fp,
            tool_signature=tool,
            tool_outcome_fingerprint=outcome,
            artifact_changes=artifacts,
            unresolved_error=error,
        )

    def test_repeated_identical_tool_outcome_escalates(self) -> None:
        signals = (
            self.signal(tool="read_file", outcome="h1"),
            self.signal(tool="read_file", outcome="h2"),  # progress: outcome changed
            self.signal(tool="run_shell", outcome="h3"),
            self.signal(tool="run_shell", outcome="h3"),  # repeat
            self.signal(tool="run_shell", outcome="h3"),  # repeat
        )
        evaluation = evaluate_no_progress(signals, self.PROFILE)
        self.assertEqual(3, evaluation.consecutive)
        self.assertEqual(StopLevel.STOP, evaluation.level)
        self.assertEqual("identical_tool_outcome", evaluation.detector)

    def test_changed_outcome_is_progress(self) -> None:
        signals = (
            self.signal(tool="read_file", outcome="h1"),
            self.signal(tool="read_file", outcome="h2"),
        )
        evaluation = evaluate_no_progress(signals, self.PROFILE)
        self.assertEqual(StopLevel.NONE, evaluation.level)
        self.assertEqual(0, evaluation.consecutive)

    def test_repeated_failure_same_context_counts(self) -> None:
        signals = (
            self.signal(error="tool_error", fp="ctx_1"),
            self.signal(error="tool_error", fp="ctx_1"),
            self.signal(error="tool_error", fp="ctx_1"),
        )
        evaluation = evaluate_no_progress(signals, self.PROFILE)
        self.assertEqual(StopLevel.STOP, evaluation.level)
        self.assertEqual("repeated_failure", evaluation.detector)

    def test_artifact_change_breaks_the_run(self) -> None:
        signals = (
            self.signal(),
            self.signal(artifacts=1),
            self.signal(),
        )
        evaluation = evaluate_no_progress(signals, self.PROFILE)
        self.assertEqual(StopLevel.NONE, evaluation.level)
        self.assertEqual(0, evaluation.consecutive)

    def test_profile_validation(self) -> None:
        with self.assertRaises(AgentPlatformError):
            StopPolicyProfile(remind_after=3, restrict_after=2, stop_after=4)
        with self.assertRaises(AgentPlatformError):
            StopPolicyProfile(remind_after=0, restrict_after=2, stop_after=4)

    def test_empty_history_is_no_evaluation(self) -> None:
        evaluation = evaluate_no_progress((), self.PROFILE)
        self.assertEqual(StopLevel.NONE, evaluation.level)
        self.assertEqual("empty_history", evaluation.detector)


class EffectReceiptSemanticsTest(unittest.TestCase):
    def test_unknown_outcome_receipt_is_expressible(self) -> None:
        receipt = EffectReceipt(
            effect_id="effect_1",
            tool_call_id="call_1",
            effect_type="file_write",
            target="/workspace/a.txt",
            started_at="2026-09-03T00:00:00Z",
            outcome=EffectOutcome.UNKNOWN,
            backend="local",
            safe_summary="状态未知，需人工核对。",
        )
        ref = EffectReceiptRef(effect_id=receipt.effect_id, outcome=receipt.outcome)
        self.assertEqual(EffectOutcome.UNKNOWN, ref.outcome)
        # unknown outcome is never reported as "not executed"
        self.assertNotEqual(EffectOutcome.NOT_COMMITTED, receipt.outcome)

    def test_invalid_receipt_fails_closed(self) -> None:
        with self.assertRaises(AgentPlatformError):
            EffectReceipt(
                effect_id="",
                tool_call_id="call_1",
                effect_type="file_write",
                target="/x",
                started_at="2026-09-03T00:00:00Z",
                outcome=EffectOutcome.COMMITTED,
                backend="local",
                safe_summary="ok",
            )


if __name__ == "__main__":
    unittest.main()
