from __future__ import annotations

import unittest

from endless_task.runtime import ApproximateTokenEstimator
from endless_task.runtime.provider import ProviderMessage


class ContextBuilderTest(unittest.TestCase):
    def test_estimator_counts_chat_framing_and_chinese_text(self) -> None:
        estimator = ApproximateTokenEstimator()
        messages = (
            ProviderMessage(role="system", content="系统"),
            ProviderMessage(role="user", content="hello"),
        )
        self.assertEqual(14, estimator.estimate_messages(messages))


if __name__ == "__main__":
    unittest.main()
