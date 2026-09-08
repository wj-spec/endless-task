import unittest
from typing import Optional

from endless_task.domain.models import KnowledgeHit, KnowledgeScope
from endless_task.runtime.context import _source_quality


def _hit(
    scope: str,
    updated_at: Optional[str] = None,
    score: float = 0.0,
) -> KnowledgeHit:
    return KnowledgeHit(
        scope=KnowledgeScope(scope),
        ref_id="r1",
        title="t",
        snippet="s",
        updated_at=updated_at,
        score=score,
    )


class SourceQualityTest(unittest.TestCase):
    def test_authority_from_scope(self) -> None:
        self.assertEqual("高", _source_quality(_hit("source"))["authority"])
        self.assertEqual("中", _source_quality(_hit("memory"))["authority"])
        self.assertEqual("中", _source_quality(_hit("artifact"))["authority"])
        self.assertEqual("低", _source_quality(_hit("conversation"))["authority"])

    def test_relevance_from_score(self) -> None:
        self.assertEqual(0.123, _source_quality(_hit("source", score=0.1234))["relevance"])

    def test_recency_days(self) -> None:
        quality = _source_quality(_hit("source", updated_at="now"))
        # 无法解析（"now"）时 recency 为 None
        self.assertIsNone(quality["recencyDays"])
        self.assertTrue("recencyDays" in quality)
        self.assertIn("relevance", quality)


if __name__ == "__main__":
    unittest.main()
