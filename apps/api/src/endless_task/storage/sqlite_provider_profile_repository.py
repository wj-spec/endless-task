"""R6.3 Provider profile repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from endless_task.domain.repositories import (
    ConflictError,
    NotFoundError,
    ValidationError,
)

from .database import Database
from .sqlite_chat_repository import new_id, utc_now

Clock = Callable[[], str]
IdFactory = Callable[[str], str]

MAX_NAME_CHARS = 60
BUILTIN_PROFILE_ID = "provider_environment"


@dataclass(frozen=True)
class ProviderProfile:
    id: str
    name: str
    kind: str
    base_url: str
    api_key_ref: str
    default_model: str
    timeout_seconds: float
    enabled: bool
    is_builtin: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ProviderProfileDraft:
    name: str
    default_model: str
    base_url: str = ""
    api_key_ref: str = ""
    timeout_seconds: float = 60.0
    enabled: bool = True


class SqliteProviderProfileRepository:
    def __init__(
        self,
        database: Database,
        *,
        clock: Clock = utc_now,
        id_factory: IdFactory = new_id,
    ) -> None:
        self._database = database
        self._clock = clock
        self._id_factory = id_factory

    def ensure_builtin_profile(
        self,
        *,
        name: str,
        default_model: str,
        base_url: str,
        api_key_ref: str,
        timeout_seconds: float,
    ) -> ProviderProfile:
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO provider_profiles (
                    id, name, kind, base_url, api_key_ref, default_model,
                    timeout_seconds, enabled, is_builtin, created_at, updated_at
                ) VALUES (?, ?, 'openai_compatible', ?, ?, ?, ?, 1, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    api_key_ref = excluded.api_key_ref,
                    default_model = excluded.default_model,
                    timeout_seconds = excluded.timeout_seconds,
                    updated_at = excluded.updated_at
                """,
                (
                    BUILTIN_PROFILE_ID,
                    name,
                    base_url,
                    api_key_ref,
                    default_model,
                    timeout_seconds,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE preferences
                SET default_provider_profile_id = ?
                WHERE id = 1 AND default_provider_profile_id IS NULL
                """,
                (BUILTIN_PROFILE_ID,),
            )
        return self.get_profile(BUILTIN_PROFILE_ID)

    def list_profiles(self) -> tuple[ProviderProfile, ...]:
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_profiles ORDER BY is_builtin DESC, name COLLATE NOCASE"
            ).fetchall()
        return tuple(self._profile(row) for row in rows)

    def get_profile(self, profile_id: str) -> ProviderProfile:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Provider profile not found: {profile_id}")
        return self._profile(row)

    def create_profile(self, draft: ProviderProfileDraft) -> ProviderProfile:
        values = self._validated_values(draft)
        profile_id = self._id_factory("provider")
        now = self._clock()
        with self._database.transaction() as connection:
            duplicate = connection.execute(
                "SELECT id FROM provider_profiles WHERE name = ?", (values["name"],)
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    f"A provider profile named {values['name']!r} already exists."
                )
            connection.execute(
                """
                INSERT INTO provider_profiles (
                    id, name, kind, base_url, api_key_ref, default_model,
                    timeout_seconds, enabled, is_builtin, created_at, updated_at
                ) VALUES (?, ?, 'openai_compatible', ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    profile_id,
                    values["name"],
                    values["base_url"],
                    values["api_key_ref"],
                    values["default_model"],
                    values["timeout_seconds"],
                    1 if values["enabled"] else 0,
                    now,
                    now,
                ),
            )
        return self.get_profile(profile_id)

    def update_profile(
        self, profile_id: str, draft: ProviderProfileDraft
    ) -> ProviderProfile:
        values = self._validated_values(draft)
        now = self._clock()
        with self._database.transaction() as connection:
            current = connection.execute(
                "SELECT * FROM provider_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            duplicate = connection.execute(
                "SELECT id FROM provider_profiles WHERE name = ? AND id != ?",
                (values["name"], profile_id),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError(
                    f"A provider profile named {values['name']!r} already exists."
                )
            connection.execute(
                """
                UPDATE provider_profiles SET
                    name = ?, base_url = ?, api_key_ref = ?, default_model = ?,
                    timeout_seconds = ?, enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["name"],
                    values["base_url"],
                    values["api_key_ref"],
                    values["default_model"],
                    values["timeout_seconds"],
                    1 if values["enabled"] else 0,
                    now,
                    profile_id,
                ),
            )
        return self.get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT is_builtin FROM provider_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            if bool(row["is_builtin"]):
                raise ValidationError("The built-in provider profile cannot be deleted.")
            connection.execute(
                "DELETE FROM provider_profiles WHERE id = ?", (profile_id,)
            )

    def get_default_profile_id(self) -> Optional[str]:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT default_provider_profile_id FROM preferences WHERE id = 1"
            ).fetchone()
        return (
            str(row["default_provider_profile_id"])
            if row and row["default_provider_profile_id"]
            else None
        )

    def set_default_profile_id(self, profile_id: str) -> None:
        with self._database.transaction() as connection:
            row = connection.execute(
                "SELECT id, enabled FROM provider_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            if not bool(row["enabled"]):
                raise ValidationError(
                    "A disabled provider profile cannot be the default."
                )
            connection.execute(
                "UPDATE preferences SET default_provider_profile_id = ? WHERE id = 1",
                (profile_id,),
            )

    def _validated_values(self, draft: ProviderProfileDraft) -> dict[str, object]:
        name = " ".join((draft.name or "").split())
        model = " ".join((draft.default_model or "").split())
        if not name or len(name) > MAX_NAME_CHARS:
            raise ValidationError("Provider name must contain 1-60 characters.")
        if not model:
            raise ValidationError("Provider default model is required.")
        timeout = float(draft.timeout_seconds)
        if not 0 < timeout <= 600:
            raise ValidationError("Provider timeout must be between 1 and 600 seconds.")
        api_key_ref = (draft.api_key_ref or "").strip()
        if api_key_ref and not (
            api_key_ref.startswith("${")
            and api_key_ref.endswith("}")
            and len(api_key_ref) > 3
            and api_key_ref[2:-1].isidentifier()
        ):
            raise ValidationError(
                "API key reference must use ${ENV_VAR}."
            )
        return {
            "name": name,
            "base_url": (draft.base_url or "").strip(),
            "api_key_ref": api_key_ref,
            "default_model": model,
            "timeout_seconds": timeout,
            "enabled": bool(draft.enabled),
        }

    def _profile(self, row) -> ProviderProfile:
        return ProviderProfile(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            base_url=row["base_url"],
            api_key_ref=row["api_key_ref"],
            default_model=row["default_model"],
            timeout_seconds=float(row["timeout_seconds"]),
            enabled=bool(row["enabled"]),
            is_builtin=bool(row["is_builtin"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
