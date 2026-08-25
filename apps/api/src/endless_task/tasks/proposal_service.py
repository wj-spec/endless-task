"""Task proposal extraction (R4.1).

Turns a completed turn into a pending task proposal when the user asked for a
recurring commitment and the assistant restated it. One-off timed matters are
reminders, never tasks (see docs/technical/p4-task-intent-protocol.md §3).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional, Tuple

from endless_task.domain.models import TaskProposal
from endless_task.domain.models import TaskProposalStatus
from endless_task.domain.repositories import RepositoryError
from endless_task.domain.task_schedule import (
    ReminderDue,
    TaskSchedule,
    parse_schedule_intent,
    serialize_task_schedule,
)
from endless_task.domain.task_schedule import describe_task_schedule
from endless_task.runtime import (
    CancellationToken,
    ModelProvider,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
)
from endless_task.storage import SqliteTaskProposalRepository

logger = logging.getLogger(__name__)

_EXTRACTION_SYSTEM_PROMPT = (
    "你是任务意图提取助手。阅读用户与 Assistant 的对话，"
    "判断 Assistant 的回答是否构成“周期执行承诺”："
    "1. 仅当用户明确要求周期性/长期行为（每天、每周、每月等），"
    "且 Assistant 在回答中明确复述并承担了该承诺时，才提出提案。"
    "2. 一次性定时事项（“明天下午整理”“周五前发我”）按提醒提案，"
    "schedule 输出 once 结构；用户没有明确要求时间的一次性事项不要提案。"
    "3. 闲聊、一次性问答、或 Assistant 只是顺带提到而用户没有要求的，不要提案。"
    "4. schedule 必须严格为四种结构之一："
    '{"kind":"daily","time":"HH:MM"}、'
    '{"kind":"weekly","weekday":1-7,"time":"HH:MM"}、'
    '{"kind":"monthly","day":1-28,"time":"HH:MM"}、'
    '{"kind":"once","at":"YYYY-MM-DDTHH:MM"}（本地时间）；'
    "周期或时间无法结构化时输出 null。"
    "5. 随附“已安排/待确认清单”：与已安排语义相同的输出 null；"
    "与待确认语义相同的照常输出新提案，并把旧提案 id 填入 supersedes"
    "（系统会取消旧提案、保留新提案）。"
    "只输出 JSON：{\"task\":{\"title\":\"短标题\","
    "\"commitment\":\"完整自然语言承诺\",\"schedule\":{...},"
    "\"reason\":\"为什么这是周期承诺\",\"supersedes\":\"旧提案 id 或省略\"}}；"
    "没有合适内容时输出 {\"task\":null}。"
)

DEFAULT_PROPOSAL_REASON = "用户要求周期性执行"

TASK_LIST_CAP = 10
PENDING_LIST_CAP = 10
LIST_SNIPPET_CHARS = 200


class TaskProposalService:
    """Generates pending task proposals from completed turns.

    The service never writes to ``tasks``; writing happens only when the user
    accepts a proposal through the resolve API (R4.2).
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        proposal_repository: SqliteTaskProposalRepository,
        model: str,
        task_repository=None,
        min_assistant_chars: int = 40,
        max_output_tokens: int = 2_000,
    ) -> None:
        if min_assistant_chars <= 0:
            raise ValueError("min_assistant_chars must be positive")
        self._provider = provider
        self._proposal_repository = proposal_repository
        self._model = model
        self._task_repository = task_repository
        self._min_assistant_chars = min_assistant_chars
        self._max_output_tokens = max_output_tokens

    async def generate_for_turn(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[TaskProposal, ...]:
        try:
            return await self._generate(
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_message=user_message,
                assistant_message=assistant_message,
            )
        except Exception as error:  # noqa: BLE001 - proposal generation must never fail a turn
            logger.warning(
                "Task proposal generation skipped for turn %s: %s",
                turn_id,
                error,
            )
            return ()

    async def _generate(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        user_message: str,
        assistant_message: str,
    ) -> Tuple[TaskProposal, ...]:
        stripped_answer = assistant_message.strip()
        if len(stripped_answer) < self._min_assistant_chars:
            return ()
        transcript = (
            f"用户：{user_message.strip()}\nAssistant：{stripped_answer}"
            f"{self._awareness_block(conversation_id)}"
        )
        request = ProviderRequest(
            request_id=f"taskp_{uuid.uuid4().hex}",
            model=self._model,
            messages=(
                ProviderMessage(role="system", content=_EXTRACTION_SYSTEM_PROMPT),
                ProviderMessage(role="user", content=transcript),
            ),
            max_output_tokens=self._max_output_tokens,
            temperature=0.0,
        )
        chunks: list[str] = []
        async for event in self._provider.stream(request, CancellationToken()):
            if isinstance(event, ProviderTextDelta):
                chunks.append(event.text)
        payload = self._parse_task("".join(chunks))
        if payload is None:
            return ()

        title = payload.get("title")
        commitment = payload.get("commitment")
        if not isinstance(title, str) or not title.strip():
            return ()
        if not isinstance(commitment, str) or not commitment.strip():
            return ()
        try:
            schedule = parse_schedule_intent(payload.get("schedule"))
        except RepositoryError:
            return ()
        reason = payload.get("reason")
        normalized_reason = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else DEFAULT_PROPOSAL_REASON
        )

        if isinstance(schedule, TaskSchedule) and self._matches_existing_task(
            commitment.strip(), schedule
        ):
            return ()

        try:
            proposal = self._proposal_repository.create_proposal(
                conversation_id=conversation_id,
                turn_id=turn_id,
                title=title,
                commitment=commitment,
                schedule=schedule,
                reason=normalized_reason,
            )
        except RepositoryError:
            return ()
        self._supersede(conversation_id, proposal, payload.get("supersedes"))
        return (proposal,)

    def _matches_existing_task(
        self, commitment: str, schedule: TaskSchedule
    ) -> bool:
        if self._task_repository is None:
            return False
        target_schedule = serialize_task_schedule(schedule)
        try:
            existing = self._task_repository.list_tasks()
        except RepositoryError:
            return False
        for record in existing:
            if record.commitment.strip() != commitment:
                continue
            if serialize_task_schedule(record.schedule) == target_schedule:
                return True
        return False

    def _awareness_block(self, conversation_id: str) -> str:
        lines: list[str] = []
        if self._task_repository is not None:
            for record in self._task_repository.list_tasks()[:TASK_LIST_CAP]:
                snippet = record.commitment.strip().replace("\n", " ")[
                    :LIST_SNIPPET_CHARS
                ]
                lines.append(
                    f"- 已安排：{snippet}"
                    f"（{describe_task_schedule(record.schedule)}）"
                )
        for proposal in self._proposal_repository.list_proposals(
            conversation_id=conversation_id
        )[:PENDING_LIST_CAP]:
            snippet = proposal.commitment.strip().replace("\n", " ")[
                :LIST_SNIPPET_CHARS
            ]
            lines.append(f"- 待确认 {proposal.id}：{snippet}")
        if not lines:
            return ""
        return (
            "\n以下是待确认或已安排的承诺：与“已安排”语义相同的输出 null；"
            "与“待确认”语义相同的照常输出新提案，并把对应旧 id 填入 supersedes。\n"
            + "\n".join(lines)
        )

    def _supersede(
        self,
        conversation_id: str,
        created: TaskProposal,
        supersedes: object,
    ) -> None:
        stale_ids: list[str] = []
        if isinstance(supersedes, str) and supersedes.strip():
            candidate = supersedes.strip()
            try:
                referenced = self._proposal_repository.get_proposal(candidate)
            except RepositoryError:
                referenced = None
            if (
                referenced is not None
                and referenced.status is TaskProposalStatus.PENDING
                and referenced.conversation_id == conversation_id
            ):
                stale_ids.append(candidate)
        created_schedule = serialize_task_schedule(created.schedule)
        for pending in self._proposal_repository.list_proposals(
            conversation_id=conversation_id
        ):
            if pending.id == created.id:
                continue
            if serialize_task_schedule(pending.schedule) == created_schedule:
                stale_ids.append(pending.id)
        for stale_id in dict.fromkeys(stale_ids):
            try:
                self._proposal_repository.cancel_proposal(stale_id)
            except RepositoryError:
                continue

    @staticmethod
    def _parse_task(raw: str) -> Optional[dict]:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(raw[start : end + 1])
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        item = payload.get("task")
        return item if isinstance(item, dict) else None
