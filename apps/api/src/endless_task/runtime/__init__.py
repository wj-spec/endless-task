"""Assistant and Agent Runtime with provider-neutral streaming contracts."""

from .background_tasks import BackgroundTaskSupervisor
from .cancellation import CancellationManager, CancellationToken
from .context import (
    ApproximateTokenEstimator,
    P0ContextBuilder,
)
from .events import RecordingEventPublisher, RuntimeEvent, RuntimeEventBroker
from .fake_provider import FakeProvider
from .knowledge_query_rewriter import KnowledgeQueryRewriter
from .knowledge_reranker import KnowledgeReranker
from .openai_compatible_provider import OpenAICompatibleProvider, UnconfiguredProvider
from .recency import (
    DEFAULT_DECAY_TAU_DAYS,
    DEFAULT_RECENCY_WEIGHT,
    blend_score,
    decay_score,
    parse_timestamp,
)
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
    "ApproximateTokenEstimator",
    "DEFAULT_DECAY_TAU_DAYS",
    "DEFAULT_RECENCY_WEIGHT",
    "blend_score",
    "decay_score",
    "parse_timestamp",
    "BackgroundTaskSupervisor",
    "CancellationManager",
    "CancellationToken",
    "FakeProvider",
    "KnowledgeQueryRewriter",
    "KnowledgeReranker",
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
    "RuntimeEvent",
    "RuntimeEventBroker",
    "UnconfiguredProvider",
]
