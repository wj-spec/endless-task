import unittest
from endless_task.runtime_v2.gateway import _context_budget_json

class ContextBudgetJsonTest(unittest.TestCase):
    def test_ratio_and_remaining(self):
        b = _context_budget_json(10000, 32768)
        self.assertEqual(32768, b["limitTokens"])
        self.assertEqual(10000, b["usedTokens"])
        self.assertAlmostEqual(0.3052, b["usedRatio"], places=3)
        self.assertEqual(22768, b["remainingTokens"])

    def test_clamps_to_one(self):
        b = _context_budget_json(40000, 32768)
        self.assertEqual(1.0, b["usedRatio"])
        self.assertEqual(0, b["remainingTokens"])

    def test_default_limit_when_none(self):
        b = _context_budget_json(0, None)
        self.assertEqual(32768, b["limitTokens"])
        self.assertEqual(0, b["usedRatio"])

if __name__ == "__main__":
    unittest.main()
