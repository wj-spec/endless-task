"""S6 技能检索：在本地技能根里按名称/描述匹配（纯字符串，不引入向量）。

设计（05 §2 Level 1）：

- ``skill_search`` 是**目录折叠后的兜底通路**——默认目录只列精选技能，
  其余技能靠这里检索出来，再用 ``read_skill_file(name)`` 读正文。
- 匹配是确定性的：名称精确 > 名称前缀 > 名称子串 > 描述命中；
  多词查询按命中词数加权，其次按名称排序。不调用 embedding，
  避免"检索漏了 = 技能不存在"这类新的失败模式。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

#: 一次最多返回多少条候选（避免把检索变成第二个目录）。
MAX_SEARCH_RESULTS = 8
#: 查询词上限（中文会展开成二元组，所以给到 12）。
MAX_QUERY_TOKENS = 12

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class SkillSearchHit:
    name: str
    description: str
    scope: str
    source: str
    score: float


def tokenize(query: str) -> Tuple[str, ...]:
    """把查询拆成小写词元。

    中文不能整段当一个词：用户问"画流程图"时，技能描述里写的是"流程图"，
    整段子串匹配会漏掉。所以中文片段额外切**重叠二元组**（"画流程图" →
    "画流"/"流程"/"程图"），既能命中"流程图"这类词，又不引入分词依赖。
    """
    seen: list[str] = []
    for raw in _TOKEN_RE.findall((query or "").lower()):
        if not raw:
            continue
        if raw.isascii():
            if raw not in seen:
                seen.append(raw)
            continue
        if len(raw) <= 2:
            if raw not in seen:
                seen.append(raw)
            continue
        for index in range(len(raw) - 1):
            gram = raw[index : index + 2]
            if gram not in seen:
                seen.append(gram)
        if raw not in seen:
            seen.append(raw)
    return tuple(seen[:MAX_QUERY_TOKENS])


def score_skill(
    *,
    name: str,
    description: str,
    tokens: Sequence[str],
    query: str,
) -> float:
    """0 表示不匹配；越大越相关。"""
    if not tokens:
        return 0.0
    lowered_name = name.lower()
    lowered_description = (description or "").lower()
    whole = (query or "").strip().lower()
    score = 0.0

    if whole and lowered_name == whole:
        score += 100.0
    if whole and whole in lowered_name:
        score += 40.0
    if whole and len(whole) >= 3 and whole in lowered_description:
        score += 20.0

    matched = 0
    for token in tokens:
        token_score = 0.0
        if token in lowered_name:
            token_score = 10.0 if lowered_name.startswith(token) else 6.0
        elif token in lowered_description:
            token_score = 3.0
        if token_score:
            matched += 1
            score += token_score
    if matched == 0:
        return 0.0
    # 命中词占比是主排序信号：问"有没有做 X 的技能"时，命中越多越可信。
    score += (matched / len(tokens)) * 15.0
    return score


def rank_skills(
    skills: Iterable[tuple[str, str, str, str]],
    *,
    query: str,
    limit: int = MAX_SEARCH_RESULTS,
) -> Tuple[SkillSearchHit, ...]:
    """按 (name, description, scope, source) 元组排序；返回前 ``limit`` 条。"""
    tokens = tokenize(query)
    if not tokens:
        return ()
    hits: list[SkillSearchHit] = []
    for name, description, scope, source in skills:
        score = score_skill(
            name=name, description=description, tokens=tokens, query=query
        )
        if score <= 0:
            continue
        hits.append(
            SkillSearchHit(
                name=name,
                description=description,
                scope=scope,
                source=source,
                score=score,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.name))
    return tuple(hits[: max(1, limit)])
