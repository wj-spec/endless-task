"""文本相似度小工具（stdlib only）。

知识去重（`knowledge/lifecycle.py`）与记忆巩固（`runtime/memory_consolidation.py`）
需要同一套"归一化 + 字符三元组 Jaccard"，放在这里避免两处各写一份，也避免
``runtime`` 反向依赖 ``knowledge`` 造成的导入环。
"""

from __future__ import annotations

import re
import unicodedata

#: 与知识检索一致的标点集合（ASCII 标点 + CJK/全角标点 + 通用空白符号）。
_PUNCTUATION_RE = re.compile(
    "[\u0021-\u002f\u003a-\u0040\u005b-\u0060\u007b-\u007e"
    "\u3000-\u303f\uff00-\uffef\u2000-\u206f]+"
)


def normalize_text(text: str) -> str:
    """归一化：NFKC 全半角统一、去标点、折叠空白、英文小写。"""
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = _PUNCTUATION_RE.sub(" ", normalized)
    normalized = " ".join(normalized.split())
    return normalized.casefold()


def char_trigrams(text: str) -> frozenset[str]:
    """归一化后按字符滑窗取三元组；短文本退化为整串。"""
    normalized = normalize_text(text).replace(" ", "")
    if not normalized:
        return frozenset()
    if len(normalized) < 3:
        return frozenset({normalized})
    return frozenset(
        normalized[index : index + 3] for index in range(len(normalized) - 2)
    )


def trigram_jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """两个三元组集合的 Jaccard 相似度（空集合返回 0）。"""
    if not left or not right:
        return 0.0
    union = len(left | right)
    if not union:
        return 0.0
    return len(left & right) / union


__all__ = ["char_trigrams", "normalize_text", "trigram_jaccard"]
