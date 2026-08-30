from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, TYPE_CHECKING

from endless_task.domain.repositories import ConflictError

from .domain import (
    CrashRecoveryAction,
    CrashRecoveryClassification,
    CrashRecoveryReason,
    ModelTurnRecord,
    ModelTurnStatus,
    RunRecord,
    RunStatus,
    RuntimeEventRecord,
    ToolExecutionRecord,
    ToolExecutionStatus,
    TranscriptEntryRecord,
)

if TYPE_CHECKING:
    from endless_task.storage.sqlite_runtime_v2_repository import SqliteRuntimeV2Repository


RUN_EVENT_STATUS = {
    "run_started": RunStatus.RUNNING,
    "run_completed": RunStatus.COMPLETED,
    "run_failed": RunStatus.FAILED,
    "run_cancelled": RunStatus.CANCELLED,
}

MODEL_TURN_EVENT_STATUS = {
    "model_turn_started": ModelTurnStatus.STREAMING,
    "model_turn_completed": ModelTurnStatus.COMPLETED,
    "model_turn_failed": ModelTurnStatus.FAILED,
    "model_turn_cancelled": ModelTurnStatus.CANCELLED,
}

TOOL_EXECUTION_EVENT_STATUS = {
    "tool_execution_started": ToolExecutionStatus.RUNNING,
    "tool_execution_completed": ToolExecutionStatus.COMPLETED,
    "tool_execution_failed": ToolExecutionStatus.FAILED,
    "tool_execution_cancelled": ToolExecutionStatus.CANCELLED,
    "tool_execution_rejected": ToolExecutionStatus.REJECTED,
    "tool_execution_expired": ToolExecutionStatus.EXPIRED,
}


CONVERSATION_SNAPSHOT_VERSION = 1

UNFINISHED_TOOL_STATUSES = frozenset(
    {
        ToolExecutionStatus.CREATED,
        ToolExecutionStatus.VALIDATING,
        ToolExecutionStatus.RUNNING,
    }
)

ACTIVE_MODEL_TURN_STATUSES = frozenset(
    {
        ModelTurnStatus.CREATED,
        ModelTurnStatus.PROJECTING_CONTEXT,
        ModelTurnStatus.WAITING_PROVIDER_SLOT,
        ModelTurnStatus.STREAMING,
        ModelTurnStatus.EXECUTING_TOOLS,
    }
)

TOOL_RESULT_TERMINAL_STATUSES = frozenset(
    {
        ToolExecutionStatus.COMPLETED,
        ToolExecutionStatus.FAILED,
        ToolExecutionStatus.CANCELLED,
        ToolExecutionStatus.REJECTED,
        ToolExecutionStatus.EXPIRED,
    }
)


@dataclass(frozen=True)
class ToolExecutionReplayResult:
    record: ToolExecutionRecord
    derived_status: ToolExecutionStatus
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelTurnReplayResult:
    record: ModelTurnRecord
    derived_status: ModelTurnStatus
    partial_content: str
    tool_executions: tuple[ToolExecutionReplayResult, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunReplayResult:
    record: RunRecord
    derived_status: RunStatus
    derived_error_code: Optional[str]
    derived_safe_message: Optional[str]
    partial_content: str
    events: tuple[RuntimeEventRecord, ...]
    model_turns: tuple[ModelTurnReplayResult, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationRuntimeSnapshot:
    conversation_id: str
    active_lane_id: str
    main_lane_id: str
    running_lane_id: Optional[str]
    running_run_id: Optional[str]
    entries: tuple[TranscriptEntryRecord, ...]
    active_run: Optional[RunReplayResult]
    active_run_id: Optional[str]
    active_run_variant_id: Optional[str]
    input_tokens: int
    output_tokens: int
    snapshot_version: int = CONVERSATION_SNAPSHOT_VERSION
    last_event_seq: int = 0


@dataclass(frozen=True)
class CrashRecoveryFinding:
    reason: CrashRecoveryReason
    message: str
    model_turn_id: Optional[str] = None
    tool_execution_id: Optional[str] = None


@dataclass(frozen=True)
class CrashRecoveryReport:
    record: RunRecord
    classification: CrashRecoveryClassification
    action: CrashRecoveryAction
    findings: tuple[CrashRecoveryFinding, ...]
    replay: Optional[RunReplayResult] = None
    waiting_approval_tool_execution_ids: tuple[str, ...] = ()
    side_effect_uncertain_tool_execution_ids: tuple[str, ...] = ()
    can_auto_resume: bool = False


class RuntimeV2ReplayService:
    """Rebuilds process state from the v2 event journal.

    Persisted repository state is authoritative. Events restore derived execution
    detail and partial output, and expose inconsistencies as warnings.
    """

    def __init__(self, repository: "SqliteRuntimeV2Repository") -> None:
        self._repository = repository

    def replay_run(self, run_id: str) -> RunReplayResult:
        run = self._repository.get_run(run_id)
        events = self._repository.list_runtime_events(run_id)
        self._validate_event_sequence(events)

        warnings: list[str] = []
        derived_status = RunStatus.CREATED
        derived_error_code: Optional[str] = None
        derived_safe_message: Optional[str] = None
        partial_content = ""
        model_turn_states: dict[str, tuple[ModelTurnStatus, str]] = {}
        tool_states: dict[str, ToolExecutionStatus] = {}

        for event in events:
            event_type = event.event_type
            payload = event.payload
            if event_type in RUN_EVENT_STATUS:
                derived_status = RUN_EVENT_STATUS[event_type]
                if event_type == "run_failed":
                    derived_error_code = _optional_string(payload, "errorCode")
                    derived_safe_message = _optional_string(payload, "safeMessage")
                continue
            if event_type == "run_status_changed":
                status_value = _optional_string(payload, "status")
                if status_value is None:
                    warnings.append(f"Event {event.event_id} has no status")
                    continue
                try:
                    derived_status = RunStatus(status_value)
                except ValueError:
                    warnings.append(
                        f"Event {event.event_id} has unsupported status {status_value}"
                    )
                continue
            if event_type in MODEL_TURN_EVENT_STATUS and event.model_turn_id:
                current_partial = model_turn_states.get(
                    event.model_turn_id,
                    (ModelTurnStatus.CREATED, ""),
                )[1]
                model_turn_states[event.model_turn_id] = (
                    MODEL_TURN_EVENT_STATUS[event_type],
                    current_partial,
                )
                continue
            if event_type == "model_turn_status_changed" and event.model_turn_id:
                status_value = _optional_string(payload, "status")
                if status_value is None:
                    warnings.append(f"Event {event.event_id} has no status")
                    continue
                try:
                    current_partial = model_turn_states.get(
                        event.model_turn_id,
                        (ModelTurnStatus.CREATED, ""),
                    )[1]
                    model_turn_states[event.model_turn_id] = (
                        ModelTurnStatus(status_value),
                        current_partial,
                    )
                except ValueError:
                    warnings.append(
                        f"Event {event.event_id} has unsupported status {status_value}"
                    )
                continue
            if event_type == "model_text_delta" and event.model_turn_id:
                delta = _optional_string(payload, "delta")
                if delta is None:
                    delta = _optional_string(payload, "text") or ""
                partial_content += delta
                current = model_turn_states.get(
                    event.model_turn_id,
                    (ModelTurnStatus.CREATED, ""),
                )
                model_turn_states[event.model_turn_id] = (
                    current[0],
                    current[1] + delta,
                )
                continue
            if event_type in TOOL_EXECUTION_EVENT_STATUS:
                tool_execution_id = _optional_string(payload, "toolExecutionId")
                if tool_execution_id is None:
                    warnings.append(f"Event {event.event_id} has no toolExecutionId")
                    continue
                tool_states[tool_execution_id] = TOOL_EXECUTION_EVENT_STATUS[event_type]
                continue
            if event_type == "tool_execution_status_changed":
                tool_execution_id = _optional_string(payload, "toolExecutionId")
                status_value = _optional_string(payload, "status")
                if tool_execution_id is None or status_value is None:
                    warnings.append(
                        f"Event {event.event_id} has no toolExecutionId or status"
                    )
                    continue
                try:
                    tool_states[tool_execution_id] = ToolExecutionStatus(status_value)
                except ValueError:
                    warnings.append(
                        f"Event {event.event_id} has unsupported status {status_value}"
                    )

        if derived_status != run.status:
            warnings.append(
                f"Event-derived run status {derived_status.value} differs from persisted "
                f"status {run.status.value}; persisted status is authoritative"
            )

        model_turn_results: list[ModelTurnReplayResult] = []
        for turn in self._repository.list_model_turns(run_id):
            event_status, turn_partial = model_turn_states.get(
                turn.id,
                (ModelTurnStatus.CREATED, ""),
            )
            turn_warnings: list[str] = []
            if event_status != turn.status:
                turn_warnings.append(
                    f"Event-derived model turn status {event_status.value} differs from "
                    f"persisted status {turn.status.value}; persisted status is authoritative"
                )

            tool_results: list[ToolExecutionReplayResult] = []
            for tool in self._repository.list_tool_executions(turn.id):
                tool_status = tool_states.get(tool.id, ToolExecutionStatus.CREATED)
                tool_warnings: list[str] = []
                if tool_status != tool.status:
                    tool_warnings.append(
                        f"Event-derived tool status {tool_status.value} differs from "
                        f"persisted status {tool.status.value}; persisted status is authoritative"
                    )
                tool_results.append(
                    ToolExecutionReplayResult(
                        record=tool,
                        derived_status=tool_status,
                        warnings=tuple(tool_warnings),
                    )
                )

            model_turn_results.append(
                ModelTurnReplayResult(
                    record=turn,
                    derived_status=event_status,
                    partial_content=turn_partial,
                    tool_executions=tuple(tool_results),
                    warnings=tuple(turn_warnings),
                )
            )

        return RunReplayResult(
            record=run,
            derived_status=derived_status,
            derived_error_code=derived_error_code,
            derived_safe_message=derived_safe_message,
            partial_content=partial_content,
            events=events,
            model_turns=tuple(model_turn_results),
            warnings=tuple(warnings),
        )

    def build_conversation_snapshot(
        self,
        conversation_id: str,
        *,
        lane_id: Optional[str] = None,
    ) -> ConversationRuntimeSnapshot:
        pointer = self._repository.get_conversation_pointer(conversation_id)
        if pointer is None:
            raise ConflictError(f"Conversation has no v2 pointer: {conversation_id}")

        selected_lane_id = lane_id or pointer.active_lane_id
        selected_lane = self._repository.get_lane(selected_lane_id)
        if selected_lane.conversation_id != conversation_id:
            raise ConflictError(
                f"Lane does not belong to conversation: {selected_lane_id}"
            )

        entries = self._repository.list_lane_context_entries(selected_lane_id)
        conversation_runs = self._repository.list_runs(conversation_id=conversation_id)
        lane_runs = [
            run
            for run in conversation_runs
            if run.lane_id == selected_lane_id and run.is_active_variant
        ]
        active_run_record = lane_runs[-1] if lane_runs else None
        running_runs = [
            run
            for run in conversation_runs
            if run.status
            in {
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.COMPACTING,
                RunStatus.CANCELLING,
            }
        ]
        running_run = running_runs[-1] if running_runs else None
        active_run = (
            self.replay_run(active_run_record.id)
            if active_run_record is not None
            else None
        )
        input_tokens = 0
        output_tokens = 0
        if active_run is not None:
            for turn in active_run.model_turns:
                input_tokens += turn.record.input_tokens or 0
                output_tokens += turn.record.output_tokens or 0

        return ConversationRuntimeSnapshot(
            conversation_id=conversation_id,
            active_lane_id=selected_lane_id,
            main_lane_id=pointer.active_lane_id,
            running_lane_id=running_run.lane_id if running_run is not None else None,
            running_run_id=running_run.id if running_run is not None else None,
            entries=entries,
            active_run=active_run,
            active_run_id=active_run_record.id if active_run_record else None,
            active_run_variant_id=(
                active_run_record.id if active_run_record else None
            ),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            snapshot_version=CONVERSATION_SNAPSHOT_VERSION,
            last_event_seq=(
                active_run.events[-1].event_seq
                if active_run is not None and active_run.events
                else 0
            ),
        )

    def classify_crash_recovery(self, run_id: str) -> CrashRecoveryReport:
        run = self._repository.get_run(run_id)
        try:
            replay = self.replay_run(run_id)
        except ConflictError as error:
            return CrashRecoveryReport(
                record=run,
                classification=CrashRecoveryClassification.NON_RECOVERABLE,
                action=CrashRecoveryAction.MARK_FAILED,
                findings=(
                    CrashRecoveryFinding(
                        reason=CrashRecoveryReason.JOURNAL_SEQUENCE_CORRUPT,
                        message=str(error),
                    ),
                ),
            )

        findings: list[CrashRecoveryFinding] = [
            CrashRecoveryFinding(
                reason=CrashRecoveryReason.STATE_EVENT_CONFLICT,
                message=warning,
            )
            for warning in replay.warnings
        ]
        waiting_approval_ids: list[str] = []
        side_effect_uncertain_ids: list[str] = []

        for model_turn in replay.model_turns:
            for warning in model_turn.warnings:
                findings.append(
                    CrashRecoveryFinding(
                        reason=CrashRecoveryReason.STATE_EVENT_CONFLICT,
                        message=warning,
                        model_turn_id=model_turn.record.id,
                    )
                )
            for tool_execution in model_turn.tool_executions:
                for warning in tool_execution.warnings:
                    findings.append(
                        CrashRecoveryFinding(
                            reason=CrashRecoveryReason.STATE_EVENT_CONFLICT,
                            message=warning,
                            model_turn_id=model_turn.record.id,
                            tool_execution_id=tool_execution.record.id,
                        )
                    )
                tool = tool_execution.record
                if tool.status is ToolExecutionStatus.WAITING_APPROVAL:
                    waiting_approval_ids.append(tool.id)
                    findings.append(
                        CrashRecoveryFinding(
                            reason=CrashRecoveryReason.WAITING_APPROVAL,
                            message="Tool execution is waiting for an approval decision",
                            model_turn_id=model_turn.record.id,
                            tool_execution_id=tool.id,
                        )
                    )
                elif tool.status in UNFINISHED_TOOL_STATUSES:
                    side_effect_uncertain_ids.append(tool.id)
                    findings.append(
                        CrashRecoveryFinding(
                            reason=CrashRecoveryReason.TOOL_SIDE_EFFECT_UNCERTAIN,
                            message=(
                                "Tool execution did not reach a terminal state; "
                                "its side effects cannot be proven absent"
                            ),
                            model_turn_id=model_turn.record.id,
                            tool_execution_id=tool.id,
                        )
                    )
                elif (
                    tool.status in TOOL_RESULT_TERMINAL_STATUSES
                    and tool.result_entry_id is None
                ):
                    side_effect_uncertain_ids.append(tool.id)
                    findings.append(
                        CrashRecoveryFinding(
                            reason=CrashRecoveryReason.TOOL_RESULT_MISSING,
                            message=(
                                "Completed tool execution has no persisted result entry"
                            ),
                            model_turn_id=model_turn.record.id,
                            tool_execution_id=tool.id,
                        )
                    )

        if run.status is RunStatus.WAITING_APPROVAL or waiting_approval_ids:
            classification = CrashRecoveryClassification.NEEDS_USER_ACTION
            action = CrashRecoveryAction.AWAIT_USER_DECISION
        elif side_effect_uncertain_ids:
            classification = CrashRecoveryClassification.NEEDS_USER_ACTION
            action = CrashRecoveryAction.AWAIT_USER_DECISION
        else:
            classification = CrashRecoveryClassification.RECOVERABLE
            if run.status is RunStatus.CANCELLING:
                action = CrashRecoveryAction.FINALIZE_CANCELLATION
            else:
                action = CrashRecoveryAction.RESUME

        if run.status is RunStatus.CANCELLING:
            findings.append(
                CrashRecoveryFinding(
                    reason=CrashRecoveryReason.CANCELLATION_PENDING,
                    message="Run was interrupted while cancellation was pending",
                )
            )
        if any(
            model_turn.record.status in ACTIVE_MODEL_TURN_STATUSES
            for model_turn in replay.model_turns
        ):
            findings.append(
                CrashRecoveryFinding(
                    reason=CrashRecoveryReason.INTERRUPTED_MODEL_TURN,
                    message="Run was interrupted during a model turn",
                )
            )
        else:
            findings.append(
                CrashRecoveryFinding(
                    reason=CrashRecoveryReason.INTERRUPTED_RUN,
                    message="Run was interrupted before reaching a terminal state",
                )
            )

        return CrashRecoveryReport(
            record=run,
            classification=classification,
            action=action,
            findings=tuple(findings),
            replay=replay,
            waiting_approval_tool_execution_ids=tuple(waiting_approval_ids),
            side_effect_uncertain_tool_execution_ids=tuple(side_effect_uncertain_ids),
            can_auto_resume=(
                classification is CrashRecoveryClassification.RECOVERABLE
                and action is CrashRecoveryAction.RESUME
            ),
        )

    def audit_interrupted_runs(
        self,
        *,
        conversation_id: Optional[str] = None,
    ) -> tuple[CrashRecoveryReport, ...]:
        interrupted = self._repository.list_runs(
            conversation_id=conversation_id,
            statuses=(
                RunStatus.CREATED,
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING_APPROVAL,
                RunStatus.COMPACTING,
                RunStatus.CANCELLING,
            )
        )
        return tuple(self.classify_crash_recovery(run.id) for run in interrupted)

    @staticmethod
    def _validate_event_sequence(events: tuple[RuntimeEventRecord, ...]) -> None:
        for expected, event in enumerate(events, start=1):
            if event.event_seq != expected:
                raise ConflictError(
                    f"Runtime event sequence gap before {event.event_seq}; expected {expected}"
                )


def _optional_string(payload: Mapping[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    return value if isinstance(value, str) else None
