"""P0 Assistant Runtime and provider-neutral streaming contracts."""

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
)

__all__ = [
    "AssistantRuntime",
    "ApproximateTokenEstimator",
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
    "RecordingEventPublisher",
    "RuntimeConfiguration",
    "RuntimeEvent",
    "RuntimeEventBroker",
    "TurnController",
    "TurnHandle",
    "UnconfiguredProvider",
]
