from __future__ import annotations

from __future__ import annotations
from endless_task.runtime.provider import ProviderToolDefinition
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional, Protocol, Sequence, TYPE_CHECKING
from endless_task.domain.models import Conversation
from endless_task.domain.repositories import ConflictError, InvalidStateError
from endless_task.runtime.background_tasks import BackgroundTaskSupervisor
from endless_task.runtime.cancellation import CancellationToken, RuntimeCancelled
from endless_task.runtime.provider import ModelProvider, ProviderMessage
from endless_task.tooling import (
    RegisteredTool,
    ToolApprovalMode,
    ToolApprovalPrompt,
    ToolCall,
    ToolEffect,
    ToolRegistry,
    ToolValidationError,
)
from ..domain import LaneKind, LaneEventRecord, LanePromotionRecord, LaneRecord, MemoryScope, ProductRuntimeEventRecord, RunRecord, RunStatus, RuntimeEventRecord, RuntimeV2MemoryPromotion, RuntimeV2MemoryRecord, ToolExecutionRecord, ToolExecutionStatus, TranscriptEntryRecord, TranscriptEntryType
from ..escalation import DEFAULT_BUDGET_RATIO
from ..usage_cost import build_run_usage_summary
from ..user_profile import UserProfileBlock
from ..lane import RuntimeV2LaneCreationResult, RuntimeV2LaneService
from ..memory import RuntimeV2MemoryService
from ..execution import AgentRunExecutor, ContextCompactionHook, RunExecutionResult, ToolApprovalDecision, ToolApprovalGate, ToolExecutionLimits
from ..metrics import ApprovalMetric, RuntimeV2MetricsCollector
from ..trace_observer import RunTraceObserver
from endless_task.runtime_ledger.pricing import PricingCatalog, make_default_catalog
from ..replay import RunReplayResult, CrashRecoveryReport, ConversationRuntimeSnapshot, RuntimeV2ReplayService
from ..domain import ProductRuntimeEventRecord, RuntimeEventRecord
from .models import AgentRuntimeCapabilities, V2_CAPABILITIES


class AgentSessionConnection:
    def __init__(self, gateway: "RuntimeV2SessionGateway", conversation_id: str):
        self._gateway = gateway
        self.conversation_id = conversation_id

    async def send(
        self,
        content: str,
        *,
        lane_id: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> RuntimeV2SendResult:
        return await self._gateway.send(
            self.conversation_id,
            content,
            lane_id=lane_id,
            client_request_id=client_request_id,
        )

    async def steer(self, run_id: str, content: str) -> bool:
        return await self._gateway.steer(run_id, content)

    async def cancel(self, run_id: str) -> bool:
        return await self._gateway.cancel(run_id)

    async def resolve_approval(
        self,
        approval_id: str,
        decision: ToolApprovalDecision,
        *,
        modified_arguments: Optional[Mapping[str, object]] = None,
    ) -> bool:
        return await self._gateway.resolve_approval(
            approval_id,
            decision,
            modified_arguments=modified_arguments,
        )

    def snapshot(self) -> dict[str, object]:
        return self._gateway.snapshot(self.conversation_id)

    def events(self) -> tuple[ProductRuntimeEventRecord, ...]:
        return self._gateway.project_events(self.conversation_id)


class AgentSessionDriver:
    driver_type = "endless-agent-v2"
    capabilities = AgentRuntimeCapabilities(
        driver_type="endless-agent-v2",
        capabilities=V2_CAPABILITIES,
    )

    def __init__(self, gateway: "RuntimeV2SessionGateway") -> None:
        self._gateway = gateway

    def validate_session(self, conversation_id: str) -> None:
        self._gateway.validate_session(conversation_id)

    def connect(self, conversation_id: str) -> AgentSessionConnection:
        self.validate_session(conversation_id)
        return AgentSessionConnection(self._gateway, conversation_id)
