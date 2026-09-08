"""C1 制造者—检查者分离：把"验证"从制造者手里拿走。

《Agentic AI Guide》第 27 章「循环工程 — 子 Agent（制造者—检查者分离）」：
让产出输出的 Agent 自己打分，激励是错位的（学生自己批改考卷）。正确做法是
**独立验证子 Agent**：独立 prompt、独立上下文，只依据「目标 + 产出 + 证据」
给出结构化判定。

本模块是纯逻辑部分：构造验证器 prompt、解析结构化判定、按模式决定是否验证。
真正的模型调用（I/O）在执行循环里，本模块不认识 provider。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError
from endless_task.runtime.provider import ProviderMessage

VERIFICATION_PROTOCOL_VERSION = 1

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_UNCERTAIN = "uncertain"

_VERDICTS = (VERDICT_PASS, VERDICT_FAIL, VERDICT_UNCERTAIN)

#: 验证模式：关闭 / 所有运行 / 仅"有副作用"的关键运行。
VERIFIER_MODES = ("0", "1", "side_effects")

#: 默认最多送进验证器的候选产出长度（超出截断，避免验证本身烧掉上下文）。
DEFAULT_CANDIDATE_CHARACTERS = 12_000

_VERIFIER_SYSTEM_PROMPT = """你是独立的验证者（critic），不参与产出，只负责判定。

你会看到：任务目标、制造者给出的最终产出、以及本次运行实际执行过的工具证据。
你**看不到**制造者的推理过程与对话历史，这是刻意的：你要独立判断，不要替它辩护。

请严格按以下 JSON 输出，不要输出任何其他内容：
{"verdict": "pass|fail|uncertain", "reasons": ["简短理由"], "missing": ["未完成/缺失的部分"]}

判定标准：
- pass：目标已达成，产出与证据一致，没有明显的安全问题；
- fail：目标未达成、证据与产出矛盾、或存在安全问题；
- uncertain：信息不足，无法判定（说明缺什么）。
理由要具体、可执行，指出"哪里不满足"而不是笼统评价。"""


def verifier_system_prompt() -> str:
    return _VERIFIER_SYSTEM_PROMPT


@dataclass(frozen=True)
class VerificationVerdict:
    """验证结论（pass/fail/uncertain + 理由）。"""

    verdict: str
    reasons: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    model: str = ""
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    schema_version: int = VERIFICATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.verdict not in _VERDICTS:
            raise AgentPlatformError(
                "invalid_verification_verdict",
                f"unknown verdict: {self.verdict!r}",
            )

    @property
    def passed(self) -> bool:
        return self.verdict == VERDICT_PASS

    @property
    def failed(self) -> bool:
        return self.verdict == VERDICT_FAIL

    def as_json(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "missing": list(self.missing),
            "model": self.model,
            "latencyMs": self.latency_ms,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
        }


def should_verify(
    mode: str,
    *,
    has_side_effects: bool,
    candidate: str,
) -> bool:
    """是否对该次运行做独立验证（空产出永不验证；未知模式直接报错）。"""
    if mode not in VERIFIER_MODES:
        raise AgentPlatformError(
            "invalid_verifier_mode",
            f"unknown verifier mode: {mode!r}",
        )
    if not candidate.strip():
        return False
    if mode == "0":
        return False
    if mode == "side_effects":
        return bool(has_side_effects)
    return True


def build_verifier_messages(
    *,
    goal: str,
    candidate: str,
    evidence: Sequence[str] = (),
    max_candidate_characters: int = DEFAULT_CANDIDATE_CHARACTERS,
) -> tuple[ProviderMessage, ...]:
    """构造验证器的消息：只有目标/产出/证据，没有制造者的历史。"""
    if max_candidate_characters < 1:
        raise AgentPlatformError(
            "invalid_verification_input",
            "max_candidate_characters must be positive",
        )
    trimmed = candidate
    truncated = False
    if len(trimmed) > max_candidate_characters:
        trimmed = trimmed[:max_candidate_characters]
        truncated = True
    parts = [f"## 任务目标\n{goal.strip() or '（未提供）'}", "## 制造者产出"]
    parts.append(trimmed if trimmed.strip() else "（空）")
    if truncated:
        parts.append("（产出过长，已截断）")
    evidence_lines = [line for line in evidence if line.strip()]
    if evidence_lines:
        parts.append("## 本次运行的工具证据")
        parts.extend(f"- {line}" for line in evidence_lines)
    return (
        ProviderMessage(role="system", content=_VERIFIER_SYSTEM_PROMPT),
        ProviderMessage(role="user", content="\n\n".join(parts)),
    )


def parse_verdict(text: str, *, model: str = "") -> VerificationVerdict:
    """把验证器输出解析成结论；解析不了就降级为 uncertain（绝不默认 pass）。"""
    payload = _extract_json_object(text)
    if payload is None:
        return VerificationVerdict(
            verdict=VERDICT_UNCERTAIN,
            reasons=("验证器输出无法解析为 JSON，无法确认结果。",),
            model=model,
        )
    raw_verdict = payload.get("verdict")
    verdict = (
        raw_verdict
        if isinstance(raw_verdict, str) and raw_verdict in _VERDICTS
        else VERDICT_UNCERTAIN
    )
    return VerificationVerdict(
        verdict=verdict,
        reasons=_string_tuple(payload.get("reasons")),
        missing=_string_tuple(payload.get("missing")),
        model=model,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _extract_json_object(text: str) -> Optional[dict[str, Any]]:
    if not isinstance(text, str):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "DEFAULT_CANDIDATE_CHARACTERS",
    "VERDICT_FAIL",
    "VERDICT_PASS",
    "VERDICT_UNCERTAIN",
    "VERIFIER_MODES",
    "VerificationVerdict",
    "build_verifier_messages",
    "parse_verdict",
    "should_verify",
    "verifier_system_prompt",
]
