"""B4 反思服务：从一次运行的情景证据归纳洞见，并走提案确认写入语义记忆。

流程（与 B2 巩固同构：**一切经提案确认，不直接写用户记忆**）：

1. ``reflect_run(run_id)``：读运行状态/事件，收集情景证据（连续同因失败、
   升级原因、运行失败），先过 ``should_reflect`` 闸门，再按规则归纳洞见；
2. 每条洞见生成一条记忆提案（kind=fact，reason 里写明"反思：…"），并写入
   ``memory_reflections`` 记录溯源与签名（UNIQUE 保证同一教训只提一次）；
3. 用户确认后 ``finalize()``：把洞见记忆的重要性提到 0.8（B3：不会被自动遗忘）
   并登记；拒绝则登记 rejected，不再重复打扰。

同时提供 ``ReflectionTerminalObserver``：挂到运行终态 fanout 上，任何终态运行
（含失败）都能触发反思，且失败不影响运行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from endless_task.domain.models import MemoryProposal
from endless_task.runtime_v2.reflection import (
    DEFAULT_INSIGHT_IMPORTANCE,
    ReflectionEpisode,
    ReflectionInsight,
    derive_insights,
    should_reflect,
)
from endless_task.storage import (
    SqliteMemoryProposalRepository,
    SqliteMemoryReflectionRepository,
    SqliteMemoryRepository,
)

CONSOLIDATION_REASON = "reflection"


@dataclass(frozen=True)
class ReflectionCandidate:
    insight: ReflectionInsight
    proposal: MemoryProposal
    run_id: str

    def as_json(self) -> dict[str, object]:
        return {
            **self.insight.as_json(),
            "proposalId": self.proposal.id,
            "runId": self.run_id,
        }


@dataclass(frozen=True)
class ReflectionRunReport:
    run_id: str
    reflected: bool = False
    created: tuple[ReflectionCandidate, ...] = ()
    skipped_signatures: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "reflected": self.reflected,
            "createdCount": len(self.created),
            "created": [item.as_json() for item in self.created],
            "skipped": list(self.skipped_signatures),
        }


class MemoryReflectionService:
    def __init__(
        self,
        *,
        runtime_repository,
        memory_repository: SqliteMemoryRepository,
        proposal_repository: SqliteMemoryProposalRepository,
        reflection_repository: SqliteMemoryReflectionRepository,
        hub_event_sink: Optional[Callable[[str, int], None]] = None,
        max_insights_per_run: int = 2,
    ) -> None:
        if max_insights_per_run <= 0:
            raise ValueError("max_insights_per_run must be positive")
        self._runtime_repository = runtime_repository
        self._memory_repository = memory_repository
        self._proposal_repository = proposal_repository
        self._reflection_repository = reflection_repository
        self._hub_event_sink = hub_event_sink
        self._max_insights_per_run = max_insights_per_run

    # ---------- 证据收集 ----------

    def _episodes(self, run_id: str, run) -> tuple[ReflectionEpisode, ...]:
        episodes: list[ReflectionEpisode] = []
        events = self._runtime_repository.list_runtime_events(run_id)
        failure_counts: dict[tuple[str, str], int] = {}
        failure_refs: dict[tuple[str, str], list[dict[str, Any]]] = {}
        escalation_reasons: list[str] = []
        for event in events:
            event_type = getattr(event, "event_type", "")
            payload = dict(getattr(event, "payload", {}) or {})
            if event_type == "tool_execution_failed":
                tool_name = str(payload.get("toolName") or "")
                execution_id = payload.get("toolExecutionId")
                if not tool_name and execution_id:
                    try:
                        tool_name = self._runtime_repository.get_tool_execution(
                            str(execution_id)
                        ).tool_name
                    except Exception:  # noqa: BLE001
                        tool_name = ""
                error_code = str(payload.get("errorCode") or "")
                key = (tool_name, error_code)
                failure_counts[key] = failure_counts.get(key, 0) + 1
                failure_refs.setdefault(key, []).append(
                    {
                        "runId": run_id,
                        "toolExecutionId": execution_id,
                        "toolName": tool_name,
                        "errorCode": error_code,
                    }
                )
            elif event_type == "run_awaiting_user":
                # run_stuck 的证据就是"同一工具连续失败"，已由 tool_failure 覆盖，
                # 这里只认升级原因，避免同一次失败出两条洞见。
                reason = str(payload.get("reason") or "")
                if reason:
                    escalation_reasons.append(reason)
                    episodes.append(
                        ReflectionEpisode(
                            kind="escalation",
                            reason=reason,
                            refs={
                                "runId": run_id,
                                "summary": payload.get("summary")
                                or payload.get("guidance")
                                or "",
                            },
                        )
                    )
        for (tool_name, error_code), count in sorted(failure_counts.items()):
            episodes.append(
                ReflectionEpisode(
                    kind="tool_failure",
                    tool_name=tool_name,
                    error_code=error_code,
                    count=count,
                    refs=tuple(failure_refs[(tool_name, error_code)])[0]
                    if failure_refs[(tool_name, error_code)]
                    else {},
                )
            )
        # 同一 (工具, 错误码) 的多条引用都带上（溯源）。
        grouped: list[ReflectionEpisode] = []
        for episode in episodes:
            if episode.kind == "tool_failure":
                refs = failure_refs.get((episode.tool_name, episode.error_code), [])
                grouped.append(
                    ReflectionEpisode(
                        kind=episode.kind,
                        tool_name=episode.tool_name,
                        error_code=episode.error_code,
                        count=episode.count,
                        refs={"refs": refs, "runId": run_id},
                    )
                )
            else:
                grouped.append(episode)
        status_value = getattr(run, "status", "")
        status_text = str(getattr(status_value, "value", status_value))
        if status_text == "failed":
            grouped.append(
                ReflectionEpisode(
                    kind="run_failure",
                    error_code=str(getattr(run, "error_code", "") or ""),
                    refs={"runId": run_id},
                )
            )
        return tuple(grouped)

    # ---------- 反思 ----------

    def reflect_run(self, run_id: str) -> ReflectionRunReport:
        try:
            run = self._runtime_repository.get_run(run_id)
        except Exception:  # noqa: BLE001 运行记录缺失时静默跳过
            return ReflectionRunReport(run_id=run_id)
        episodes = self._episodes(run_id, run)
        status = getattr(getattr(run, "status", ""), "value", getattr(run, "status", ""))
        escalation_reasons = [
            episode.reason for episode in episodes if episode.kind == "escalation"
        ]
        failure_streak = max(
            (
                episode.count
                for episode in episodes
                if episode.kind == "tool_failure"
            ),
            default=0,
        )
        if not should_reflect(
            run_status=str(status),
            escalation_reasons=escalation_reasons,
            failure_streak=failure_streak,
        ):
            return ReflectionRunReport(run_id=run_id)
        created: list[ReflectionCandidate] = []
        skipped: list[str] = []
        for insight in derive_insights(episodes):
            if len(created) >= self._max_insights_per_run:
                break
            if self._reflection_repository.find_by_signature(insight.signature):
                skipped.append(insight.signature)
                continue
            try:
                proposal = self._proposal_repository.create_proposal(
                    conversation_id=run.conversation_id,
                    turn_id=run_id,
                    kind="fact",
                    content=insight.content,
                    reason=insight.reason,
                )
            except Exception:  # noqa: BLE001 提案重复/写入失败都不影响运行
                skipped.append(insight.signature)
                continue
            self._reflection_repository.create(
                conversation_id=run.conversation_id,
                run_id=run_id,
                trigger=insight.trigger,
                signature=insight.signature,
                insight_content=insight.content,
                proposal_id=proposal.id,
                source_refs=insight.refs,
            )
            created.append(
                ReflectionCandidate(insight=insight, proposal=proposal, run_id=run_id)
            )
        if created and self._hub_event_sink is not None:
            try:
                self._hub_event_sink(run.conversation_id, len(created))
            except Exception:  # noqa: BLE001 事件推送失败不影响反思结果
                pass
        return ReflectionRunReport(
            run_id=run_id,
            reflected=True,
            created=tuple(created),
            skipped_signatures=tuple(skipped),
        )

    # ---------- 确认 ----------

    def finalize(
        self, proposal_id: str, insight_memory
    ) -> Optional[str]:
        """提案被接受：把洞见记忆标为"重要"，并登记来源。"""
        record = self._reflection_repository.find_by_proposal(proposal_id)
        if record is None:
            return None
        if record.status == "accepted":
            return record.id
        try:
            self._memory_repository.set_memory_importance(
                insight_memory.id, DEFAULT_INSIGHT_IMPORTANCE
            )
        except Exception:  # noqa: BLE001 记忆已删除等情况下不阻断
            pass
        self._reflection_repository.mark_resolved(
            record.id, status="accepted", insight_memory_id=insight_memory.id
        )
        return record.id

    def reject(self, proposal_id: str) -> Optional[str]:
        record = self._reflection_repository.find_by_proposal(proposal_id)
        if record is None:
            return None
        if record.status == "pending":
            self._reflection_repository.mark_resolved(
                record.id, status="rejected"
            )
        return record.id

    def list_records(
        self,
        *,
        conversation_id: Optional[str] = None,
        include_resolved: bool = True,
        limit: int = 50,
    ):
        return self._reflection_repository.list_records(
            conversation_id=conversation_id,
            include_resolved=include_resolved,
            limit=limit,
        )


class ReflectionTerminalObserver:
    """挂到运行终态 fanout：任何终态运行都能触发反思（fail-open）。"""

    def __init__(self, service: MemoryReflectionService) -> None:
        self._service = service

    async def on_run_terminal(self, run_id: str) -> None:
        self._service.reflect_run(run_id)


__all__ = [
    "CONSOLIDATION_REASON",
    "MemoryReflectionService",
    "ReflectionCandidate",
    "ReflectionRunReport",
    "ReflectionTerminalObserver",
]
