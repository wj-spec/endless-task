"""Assistant and Agent Runtime with provider-neutral streaming contracts."""

from .agent_loop import AgentLoop, AgentLoopResult
from .approval import ApprovalCoordinator
from .assistant import AssistantRuntime, RuntimeConfiguration
from .cancellation import CancellationManager, CancellationToken
from .controller import TurnController, TurnHandle
from .context import (
    ApproximateTokenEstimator,
    ContextBuildError,
    ContextSnapshot,
    ConversationSummaryRevision,
    IncludedTurn,
    P0ContextBuilder,
)
from .events import RecordingEventPublisher, RuntimeEvent, RuntimeEventBroker
from .fake_provider import FakeProvider
from .openai_compatible_provider import OpenAICompatibleProvider, UnconfiguredProvider
from .provider import (
    ModelProvider,
    ProviderCompleted,
    ProviderError,
    ProviderMessage,
    ProviderRequest,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)

__all__ = [
    "AssistantRuntime",
    "AgentLoop",
    "AgentLoopResult",
    "ApproximateTokenEstimator",
    "ApprovalCoordinator",
    "CancellationManager",
    "CancellationToken",
    "ContextBuildError",
    "ContextSnapshot",
    "ConversationSummaryRevision",
    "FakeProvider",
    "IncludedTurn",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "P0ContextBuilder",
    "ProviderCompleted",
    "ProviderError",
    "ProviderMessage",
    "ProviderRequest",
    "ProviderTextDelta",
    "ProviderToolCall",
    "ProviderToolDefinition",
    "RecordingEventPublisher",
    "RuntimeConfiguration",
    "RuntimeEvent",
    "RuntimeEventBroker",
    "TurnController",
    "TurnHandle",
    "UnconfiguredProvider",
]
