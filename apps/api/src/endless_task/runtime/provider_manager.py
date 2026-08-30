"""R6.3 Provider selection: global default plus per-conversation override."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from .openai_compatible_provider import OpenAICompatibleProvider, UnconfiguredProvider
from .provider import ModelProvider
from ..storage.provider_profile_protocol import ProviderProfileRepositoryProtocol
from ..storage.sqlite_provider_profile_repository import (
    BUILTIN_PROFILE_ID,
    ProviderProfile,
)

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class SelectedProvider:
    provider: ModelProvider
    model: str
    profile: ProviderProfile
    fallback_reason: Optional[str] = None


class ProviderManager:
    def __init__(
        self,
        *,
        repository: ProviderProfileRepositoryProtocol,
        fallback_provider: ModelProvider,
    ) -> None:
        self._repository = repository
        self._fallback_provider = fallback_provider
        self._providers: dict[str, ModelProvider] = {}

    def resolve(self, conversation) -> SelectedProvider:
        profile_id = conversation.provider_profile_id
        fallback_reason: Optional[str] = None
        profile: Optional[ProviderProfile] = None
        if profile_id:
            try:
                profile = self._repository.get_profile(profile_id)
            except Exception:
                fallback_reason = "missing"
        if profile is not None and not profile.enabled:
            fallback_reason = "disabled"
            profile = self._default_profile()
        if profile is None:
            profile = self._default_profile()
        elif fallback_reason is None and isinstance(
            self._provider_for_profile(profile), UnconfiguredProvider
        ):
            default_profile = self._default_profile()
            if default_profile.id != profile.id and not isinstance(
                self._provider_for_profile(default_profile), UnconfiguredProvider
            ):
                fallback_reason = "unconfigured"
                profile = default_profile
        model = conversation.model_override or profile.default_model
        return SelectedProvider(
            provider=self._provider_for_profile(profile),
            model=model,
            profile=profile,
            fallback_reason=fallback_reason,
        )

    def default_selection(self) -> SelectedProvider:
        profile = self._default_profile()
        return SelectedProvider(
            provider=self._provider_for_profile(profile),
            model=profile.default_model,
            profile=profile,
        )

    async def invalidate(self, profile_id: str) -> None:
        provider = self._providers.pop(profile_id, None)
        if provider is None:
            return
        close = getattr(provider, "close", None)
        if close is None:
            return
        result = close()
        if hasattr(result, "__await__"):
            await result

    async def close(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if close is None:
                continue
            result = close()
            if hasattr(result, "__await__"):
                await result
        self._providers.clear()

    def _default_profile(self) -> ProviderProfile:
        profile_id = self._repository.get_default_profile_id()
        if profile_id:
            try:
                return self._repository.get_profile(profile_id)
            except Exception:
                pass
        return self._repository.get_profile(BUILTIN_PROFILE_ID)

    def _provider_for_profile(self, profile: ProviderProfile) -> ModelProvider:
        if profile.id == BUILTIN_PROFILE_ID:
            return self._fallback_provider
        cached = self._providers.get(profile.id)
        if cached is not None:
            return cached
        api_key = self._resolve_api_key(profile.api_key_ref)
        if not api_key and not profile.base_url.strip():
            provider: ModelProvider = UnconfiguredProvider(profile.name)
        else:
            provider = OpenAICompatibleProvider(
                name=profile.name,
                api_key=api_key or "local-endpoint",
                base_url=profile.base_url or None,
                timeout_seconds=profile.timeout_seconds,
            )
        self._providers[profile.id] = provider
        return provider

    @staticmethod
    def _resolve_api_key(reference: str) -> Optional[str]:
        value = (reference or "").strip()
        if not value:
            return None
        match = _ENV_REF.match(value)
        if match:
            return os.environ.get(match.group(1))
        return value
