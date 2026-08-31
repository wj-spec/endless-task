from __future__ import annotations

from typing import Protocol

from .sqlite_provider_profile_repository import ProviderModel, ProviderProfile


class ProviderProfileRepositoryProtocol(Protocol):
    def get_profile(self, profile_id: str) -> ProviderProfile: ...

    def get_default_profile_id(self) -> str | None: ...

    def get_model(self, profile_id: str, model_id: str) -> ProviderModel: ...
