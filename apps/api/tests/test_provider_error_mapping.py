"""Provider 错误映射：让用户看到「真正的原因」，而不是笼统的「可以重试」。

事故背景：投影产生孤儿 tool 消息 → 服务端返回 400 invalid_request_error →
旧映射没有 400 分支，落到兜底 `provider_error`「模型服务返回错误，可以重试。」
（且 retryable=False），用户重试永远失败却看不出原因。
"""

from __future__ import annotations

import unittest

from endless_task.runtime.openai_compatible_provider import (
    OpenAICompatibleProvider,
)


class _FakeProviderError(Exception):
    def __init__(
        self,
        *,
        status_code=None,
        body=None,
        request_id=None,
        headers=None,
        message="",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.request_id = request_id
        if headers is not None:
            self.response = type("R", (), {"headers": headers})()


class ProviderErrorMappingTest(unittest.TestCase):
    def _normalize(self, error):
        return OpenAICompatibleProvider._normalize_error(error)

    def test_invalid_request_surfaces_provider_message(self) -> None:
        error = _FakeProviderError(
            status_code=400,
            body={
                "error": {
                    "message": (
                        "Messages with role 'tool' must be a response to a "
                        "preceding message with 'tool_calls'"
                    ),
                    "code": "invalid_request_error",
                }
            },
        )
        mapped = self._normalize(error)
        self.assertEqual("invalid_request", mapped.code)
        self.assertIn("tool_calls", mapped.safe_message)
        self.assertFalse(mapped.retryable)

    def test_unprocessable_entity_is_invalid_request(self) -> None:
        mapped = self._normalize(
            _FakeProviderError(
                status_code=422,
                body={"error": {"message": "schema mismatch"}},
            )
        )
        self.assertEqual("invalid_request", mapped.code)
        self.assertIn("schema mismatch", mapped.safe_message)

    def test_missing_model_is_reported(self) -> None:
        mapped = self._normalize(
            _FakeProviderError(
                status_code=404,
                body={"error": {"message": "Model Not Exist"}},
            )
        )
        self.assertEqual("model_not_found", mapped.code)
        self.assertIn("Model Not Exist", mapped.safe_message)
        self.assertFalse(mapped.retryable)

    def test_server_error_stays_retryable(self) -> None:
        mapped = self._normalize(_FakeProviderError(status_code=503))
        self.assertEqual("provider_unavailable", mapped.code)
        self.assertTrue(mapped.retryable)

    def test_rate_limit_stays_retryable(self) -> None:
        mapped = self._normalize(
            _FakeProviderError(status_code=429, headers={"retry-after": "3"})
        )
        self.assertEqual("rate_limited", mapped.code)
        self.assertTrue(mapped.retryable)
        self.assertEqual(3000, mapped.retry_after_ms)

    def test_unknown_error_is_honest_about_retry(self) -> None:
        mapped = self._normalize(_FakeProviderError(message="boom"))
        self.assertEqual("provider_error", mapped.code)
        # 兜底文案不能承诺「可以重试」——它标记为不可重试。
        self.assertNotIn("可以重试", mapped.safe_message)
        self.assertFalse(mapped.retryable)


if __name__ == "__main__":
    unittest.main()
