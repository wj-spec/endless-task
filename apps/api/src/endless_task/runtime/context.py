from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, Tuple

from endless_task.domain.models import ResponseVariantStatus, TurnSnapshot
from endless_task.domain.repositories import ChatRepository, InvalidStateError

from .provider import ProviderMessage


@dataclass(frozen=True)
class IncludedTurn:
    turn_ordinal: int
    response_variant_id: str


@dataclass(frozen=True)
class ConversationSummaryRevision:
    id: str
    conversation_id: str
    through_turn_ordinal: int
    prompt_version: str
    content: str
    input_token_estimate: int
    created_at: str


@dataclass(frozen=True)
class ContextSnapshot:
    id: str
    turn_id: str
    response_variant_id: str
    system_prompt_version: str
    summary_revision_id: Optional[str]
    included_turns: Tuple[IncludedTurn, ...]
    input_token_estimate: int
    reserved_output_tokens: int
    created_at: str


@dataclass(frozen=True)
class BuiltContext:
    messages: Tuple[ProviderMessage, ...]
    included_turns: Tuple[tuple[int, str], ...]
    system_prompt_version: str
    input_token_estimate: int
    reserved_output_tokens: int
    summary_revision_id: Optional[str] = None
    snapshot: Optional[ContextSnapshot] = None


@dataclass(frozen=True)
class ContextSourceTurn:
    ordinal: int
    response_variant_id: str
    user_content: str
    assistant_content: str


class ContextBuildError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class TokenEstimator(Protocol):
    def estimate_text(self, content: str) -> int:
        ...

    def estimate_messages(self, messages: Sequence[ProviderMessage]) -> int:
        ...


class ContextRepository(Protocol):
    def get_summary_revision(
        self,
        *,
        conversation_id: str,
        through_turn_ordinal: int,
        prompt_version: str,
    ) -> Optional[ConversationSummaryRevision]:
        ...

    def save_summary_revision(
        self,
        *,
        conversation_id: str,
        through_turn_ordinal: int,
        prompt_version: str,
        content: str,
        input_token_estimate: int,
    ) -> ConversationSummaryRevision:
        ...

    def save_context_snapshot(
        self,
        *,
        turn_id: str,
        response_variant_id: str,
        system_prompt_version: str,
        summary_revision_id: Optional[str],
        included_turns: Sequence[IncludedTurn],
        input_token_estimate: int,
        reserved_output_tokens: int,
    ) -> ContextSnapshot:
        ...


class ApproximateTokenEstimator:
    """Deterministic, dependency-free estimate suitable for budget enforcement."""

    _pieces = re.compile(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]")

    def estimate_text(self, content: str) -> int:
        tokens = 0
        for piece in self._pieces.findall(content):
            if len(piece) == 1 and "\u3400" <= piece <= "\u9fff":
                tokens += 1
            elif piece.isascii() and (piece.isalnum() or "_" in piece):
                tokens += max(1, math.ceil(len(piece) / 4))
            else:
                tokens += 1
        return tokens

    def estimate_messages(self, messages: Sequence[ProviderMessage]) -> int:
        # Four tokens per message and two request-level tokens are a conservative
        # approximation of common chat templates. Provider usage remains canonical.
        return 2 + sum(4 + self.estimate_text(message.content) for message in messages)


class ExtractiveConversationSummarizer:
    """Creates a bounded local summary without making a hidden provider request."""

    _header = "较早对话摘要（仅供当前会话延续）：\n"

    def summarize(
        self,
        turns: Sequence[ContextSourceTurn],
        *,
        max_tokens: int,
        estimator: TokenEstimator,
    ) -> str:
        if not turns or max_tokens <= estimator.estimate_text(self._header):
            return ""

        lines = [self._header]
        for turn in turns:
            user = self._compact(turn.user_content)
            assistant = self._compact(turn.assistant_content)
            lines.append(f"第 {turn.ordinal} 轮｜用户：{user}｜助手：{assistant}\n")

        content = "".join(lines).rstrip()
        if estimator.estimate_text(content) <= max_tokens:
            return content
        return self._truncate(content, max_tokens, estimator)

    @staticmethod
    def _compact(content: str) -> str:
        return " ".join(content.split())

    @staticmethod
    def _truncate(content: str, max_tokens: int, estimator: TokenEstimator) -> str:
        suffix = "…"
        low, high = 0, len(content)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = content[:middle].rstrip() + suffix
            if estimator.estimate_text(candidate) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        return content[:low].rstrip() + suffix if low else ""


class P0ContextBuilder:
    """Builds a bounded canonical timeline without memory or tools."""

    def __init__(
        self,
        repository: ChatRepository,
        *,
        system_prompt: str,
        system_prompt_version: str = "p0-v1",
        max_context_tokens: int = 32_768,
        summary_token_limit: int = 1_024,
        context_repository: Optional[ContextRepository] = None,
        token_estimator: Optional[TokenEstimator] = None,
        summarizer: Optional[ExtractiveConversationSummarizer] = None,
    ) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if summary_token_limit < 0:
            raise ValueError("summary_token_limit cannot be negative")
        self._repository = repository
        self._system_prompt = system_prompt
        self._system_prompt_version = system_prompt_version
        self._max_context_tokens = max_context_tokens
        self._summary_token_limit = summary_token_limit
        self._context_repository = context_repository
        self._token_estimator = token_estimator or ApproximateTokenEstimator()
        self._summarizer = summarizer or ExtractiveConversationSummarizer()

    def build(
        self,
        turn_id: str,
        *,
        response_variant_id: Optional[str] = None,
        reserved_output_tokens: int = 2_048,
    ) -> BuiltContext:
        if reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens cannot be negative")
        input_budget = self._max_context_tokens - reserved_output_tokens
        if input_budget <= 0:
            raise ContextBuildError(
                "context_too_large",
                "模型上下文预算不足，请降低最大输出长度。",
            )

        current = self._repository.get_turn(turn_id)
        active_variant_id = current.turn.active_response_variant_id
        selected_variant_id = response_variant_id or active_variant_id
        if not selected_variant_id or selected_variant_id != active_variant_id:
            raise InvalidStateError("Context can only be built for the active response variant")

        conversation = self._repository.get_conversation_snapshot(
            current.turn.conversation_id
        )
        system_message = ProviderMessage(role="system", content=self._system_prompt)
        current_message = ProviderMessage(role="user", content=current.user_message.content)
        required_messages = (system_message, current_message)
        required_tokens = self._token_estimator.estimate_messages(required_messages)
        if required_tokens > input_budget:
            raise ContextBuildError(
                "context_too_large",
                "当前消息超过模型可用上下文，请缩短后重试。",
            )

        history = self._canonical_history(conversation.turns, current.turn.ordinal)
        all_history_messages = self._messages_for(history)
        if self._token_estimator.estimate_messages(
            (system_message, *all_history_messages, current_message)
        ) <= input_budget:
            recent = history
            summary_revision = None
        else:
            recent, omitted = self._select_recent_history(
                history,
                input_budget=input_budget,
                required_tokens=required_tokens,
            )
            summary_revision = self._summary_for(
                conversation_id=current.turn.conversation_id,
                turns=omitted,
                available_tokens=input_budget
                - self._token_estimator.estimate_messages(
                    (system_message, *self._messages_for(recent), current_message)
                ),
            )

        messages: list[ProviderMessage] = [system_message]
        if summary_revision is not None:
            messages.append(ProviderMessage(role="system", content=summary_revision.content))
        messages.extend(self._messages_for(recent))
        messages.append(current_message)
        input_token_estimate = self._token_estimator.estimate_messages(messages)
        if input_token_estimate > input_budget:
            raise ContextBuildError(
                "context_too_large",
                "当前会话无法在模型上下文预算内安全构建。",
            )

        included = tuple(
            IncludedTurn(turn.ordinal, turn.response_variant_id) for turn in recent
        )
        snapshot = None
        if self._context_repository is not None:
            snapshot = self._context_repository.save_context_snapshot(
                turn_id=turn_id,
                response_variant_id=selected_variant_id,
                system_prompt_version=self._system_prompt_version,
                summary_revision_id=(summary_revision.id if summary_revision else None),
                included_turns=included,
                input_token_estimate=input_token_estimate,
                reserved_output_tokens=reserved_output_tokens,
            )

        return BuiltContext(
            messages=tuple(messages),
            included_turns=tuple(
                (item.turn_ordinal, item.response_variant_id) for item in included
            ),
            system_prompt_version=self._system_prompt_version,
            input_token_estimate=input_token_estimate,
            reserved_output_tokens=reserved_output_tokens,
            summary_revision_id=summary_revision.id if summary_revision else None,
            snapshot=snapshot,
        )

    def _canonical_history(
        self,
        turns: Sequence[TurnSnapshot],
        current_ordinal: int,
    ) -> tuple[ContextSourceTurn, ...]:
        history: list[ContextSourceTurn] = []
        for snapshot in turns:
            if snapshot.turn.ordinal >= current_ordinal:
                break
            active_variant_id = snapshot.turn.active_response_variant_id
            selected = next(
                (
                    item
                    for item in snapshot.response_variants
                    if item.variant.id == active_variant_id
                ),
                None,
            )
            if selected is None:
                raise InvalidStateError("Historical turn has no active response variant")
            if selected.variant.status is not ResponseVariantStatus.COMPLETED:
                continue
            history.append(
                ContextSourceTurn(
                    ordinal=snapshot.turn.ordinal,
                    response_variant_id=selected.variant.id,
                    user_content=snapshot.user_message.content,
                    assistant_content=selected.assistant_message.content,
                )
            )
        return tuple(history)

    @staticmethod
    def _messages_for(turns: Sequence[ContextSourceTurn]) -> tuple[ProviderMessage, ...]:
        messages: list[ProviderMessage] = []
        for turn in turns:
            messages.extend(
                (
                    ProviderMessage(role="user", content=turn.user_content),
                    ProviderMessage(role="assistant", content=turn.assistant_content),
                )
            )
        return tuple(messages)

    def _select_recent_history(
        self,
        history: Sequence[ContextSourceTurn],
        *,
        input_budget: int,
        required_tokens: int,
    ) -> tuple[tuple[ContextSourceTurn, ...], tuple[ContextSourceTurn, ...]]:
        remaining = input_budget - required_tokens
        summary_allowance = min(self._summary_token_limit + 4, remaining // 4)
        recent_budget = max(0, remaining - summary_allowance)
        selected: list[ContextSourceTurn] = []
        used = 0
        for turn in reversed(history):
            turn_cost = self._token_estimator.estimate_messages(
                self._messages_for((turn,))
            ) - 2
            if used + turn_cost > recent_budget:
                break
            selected.append(turn)
            used += turn_cost
        selected.reverse()
        omitted_count = len(history) - len(selected)
        return tuple(selected), tuple(history[:omitted_count])

    def _summary_for(
        self,
        *,
        conversation_id: str,
        turns: Sequence[ContextSourceTurn],
        available_tokens: int,
    ) -> Optional[ConversationSummaryRevision]:
        if not turns or available_tokens <= 4 or self._summary_token_limit == 0:
            return None
        through_ordinal = turns[-1].ordinal
        if self._context_repository is not None:
            existing = self._context_repository.get_summary_revision(
                conversation_id=conversation_id,
                through_turn_ordinal=through_ordinal,
                prompt_version=self._system_prompt_version,
            )
            if existing is not None and existing.input_token_estimate + 4 <= available_tokens:
                return existing
            if existing is not None:
                return None

        content_budget = min(self._summary_token_limit, available_tokens - 4)
        content = self._summarizer.summarize(
            turns,
            max_tokens=content_budget,
            estimator=self._token_estimator,
        )
        if not content:
            return None
        estimate = self._token_estimator.estimate_text(content)
        if self._context_repository is None:
            return ConversationSummaryRevision(
                id="transient-summary",
                conversation_id=conversation_id,
                through_turn_ordinal=through_ordinal,
                prompt_version=self._system_prompt_version,
                content=content,
                input_token_estimate=estimate,
                created_at="",
            )
        return self._context_repository.save_summary_revision(
            conversation_id=conversation_id,
            through_turn_ordinal=through_ordinal,
            prompt_version=self._system_prompt_version,
            content=content,
            input_token_estimate=estimate,
        )
