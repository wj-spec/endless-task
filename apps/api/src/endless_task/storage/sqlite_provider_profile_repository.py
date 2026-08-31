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
MAX_MODEL_ID_CHARS = 200
BUILTIN_PROFILE_ID = "provider_environment"
CONNECTION_STATES = {"untested", "ready", "failed"}
MODEL_SOURCES = {"discovered", "manual"}


@dataclass(frozen=True)
class ProviderModel:
    provider_profile_id: str
    model_id: str
    display_name: str
    source: str
    enabled: bool
    created_at: str
    updated_at: str
    last_seen_at: Optional[str]


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
    connection_state: str
    last_checked_at: Optional[str]
    last_error: Optional[str]
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
            if default_model.strip():
                self._upsert_model(
                    connection,
                    provider_profile_id=BUILTIN_PROFILE_ID,
                    model_id=default_model,
                    display_name=default_model,
                    source="manual",
                    enabled=True,
                    last_seen_at=None,
                    now=now,
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
            if str(values["default_model"]):
                self._upsert_model(
                    connection,
                    provider_profile_id=profile_id,
                    model_id=str(values["default_model"]),
                    display_name=str(values["default_model"]),
                    source="manual",
                    enabled=True,
                    last_seen_at=None,
                    now=now,
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
            preference = connection.execute(
                "SELECT default_provider_profile_id FROM preferences WHERE id = 1"
            ).fetchone()
            if (
                preference is not None
                and preference["default_provider_profile_id"] == profile_id
                and (
                    not bool(values["enabled"])
                    or not str(values["default_model"]).strip()
                )
            ):
                raise ValidationError(
                    "The default provider must stay enabled and have a default model."
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
            if str(values["default_model"]):
                self._upsert_model(
                    connection,
                    provider_profile_id=profile_id,
                    model_id=str(values["default_model"]),
                    display_name=str(values["default_model"]),
                    source="manual",
                    enabled=True,
                    last_seen_at=None,
                    now=now,
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
                """
                UPDATE conversations
                SET model_override = NULL
                WHERE provider_profile_id = ?
                """,
                (profile_id,),
            )
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
                """
                SELECT id, enabled, default_model
                FROM provider_profiles WHERE id = ?
                """,
                (profile_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            if not bool(row["enabled"]):
                raise ValidationError(
                    "A disabled provider profile cannot be the default."
                )
            if not str(row["default_model"]).strip():
                raise ValidationError(
                    "A provider without a default model cannot be the default."
                )
            default_model = connection.execute(
                """
                SELECT enabled FROM provider_models
                WHERE provider_profile_id = ? AND model_id = ?
                """,
                (profile_id, row["default_model"]),
            ).fetchone()
            if default_model is None or not bool(default_model["enabled"]):
                raise ValidationError(
                    "A provider without an enabled default model cannot be the default."
                )
            connection.execute(
                "UPDATE preferences SET default_provider_profile_id = ? WHERE id = 1",
                (profile_id,),
            )

    def list_models(
        self,
        profile_id: str,
        *,
        enabled_only: bool = False,
    ) -> tuple[ProviderModel, ...]:
        self.get_profile(profile_id)
        query = "SELECT * FROM provider_models WHERE provider_profile_id = ?"
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY display_name COLLATE NOCASE, model_id COLLATE NOCASE"
        with self._database.connect() as connection:
            rows = connection.execute(query, (profile_id,)).fetchall()
        return tuple(self._model(row) for row in rows)

    def get_model(self, profile_id: str, model_id: str) -> ProviderModel:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM provider_models
                WHERE provider_profile_id = ? AND model_id = ?
                """,
                (profile_id, model_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(
                f"Provider model not found: {profile_id}/{model_id}"
            )
        return self._model(row)

    def add_model(
        self,
        profile_id: str,
        *,
        model_id: str,
        display_name: str = "",
    ) -> ProviderModel:
        self.get_profile(profile_id)
        normalized_id = self._validated_model_id(model_id)
        normalized_name = " ".join((display_name or "").split()) or normalized_id
        now = self._clock()
        with self._database.transaction() as connection:
            self._upsert_model(
                connection,
                provider_profile_id=profile_id,
                model_id=normalized_id,
                display_name=normalized_name,
                source="manual",
                enabled=True,
                last_seen_at=None,
                now=now,
            )
        return self.get_model(profile_id, normalized_id)

    def set_model_enabled(
        self,
        profile_id: str,
        model_id: str,
        *,
        enabled: bool,
    ) -> ProviderModel:
        normalized_id = self._validated_model_id(model_id)
        with self._database.transaction() as connection:
            profile = connection.execute(
                "SELECT default_model FROM provider_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if profile is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            if not enabled and profile["default_model"] == normalized_id:
                raise ValidationError("The default model cannot be disabled.")
            updated = connection.execute(
                """
                UPDATE provider_models
                SET enabled = ?, updated_at = ?
                WHERE provider_profile_id = ? AND model_id = ?
                """,
                (1 if enabled else 0, self._clock(), profile_id, normalized_id),
            )
            if updated.rowcount == 0:
                raise NotFoundError(
                    f"Provider model not found: {profile_id}/{normalized_id}"
                )
        return self.get_model(profile_id, normalized_id)

    def delete_model(self, profile_id: str, model_id: str) -> None:
        normalized_id = self._validated_model_id(model_id)
        with self._database.transaction() as connection:
            profile = connection.execute(
                "SELECT default_model FROM provider_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if profile is None:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
            if profile["default_model"] == normalized_id:
                raise ValidationError("The default model cannot be deleted.")
            deleted = connection.execute(
                """
                DELETE FROM provider_models
                WHERE provider_profile_id = ? AND model_id = ?
                """,
                (profile_id, normalized_id),
            )
            if deleted.rowcount == 0:
                raise NotFoundError(
                    f"Provider model not found: {profile_id}/{normalized_id}"
                )

    def replace_discovered_models(
        self,
        profile_id: str,
        models: tuple[tuple[str, str], ...],
    ) -> tuple[ProviderModel, ...]:
        self.get_profile(profile_id)
        normalized: dict[str, str] = {}
        for model_id, display_name in models:
            normalized_id = self._validated_model_id(model_id)
            normalized[normalized_id] = (
                " ".join((display_name or "").split()) or normalized_id
            )
        now = self._clock()
        with self._database.transaction() as connection:
            if normalized:
                placeholders = ", ".join("?" for _ in normalized)
                connection.execute(
                    f"""
                    UPDATE provider_models
                    SET enabled = 0, updated_at = ?
                    WHERE provider_profile_id = ?
                      AND source = 'discovered'
                      AND model_id NOT IN ({placeholders})
                    """,
                    (now, profile_id, *normalized.keys()),
                )
            else:
                connection.execute(
                    """
                    UPDATE provider_models
                    SET enabled = 0, updated_at = ?
                    WHERE provider_profile_id = ? AND source = 'discovered'
                    """,
                    (now, profile_id),
                )
            for model_id, display_name in normalized.items():
                self._upsert_model(
                    connection,
                    provider_profile_id=profile_id,
                    model_id=model_id,
                    display_name=display_name,
                    source="discovered",
                    enabled=True,
                    last_seen_at=now,
                    now=now,
                    preserve_enabled=True,
                )
            profile = connection.execute(
                "SELECT default_model FROM provider_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            current_default = (
                connection.execute(
                    """
                    SELECT enabled FROM provider_models
                    WHERE provider_profile_id = ? AND model_id = ?
                    """,
                    (profile_id, profile["default_model"]),
                ).fetchone()
                if profile and profile["default_model"]
                else None
            )
            if current_default is None or not bool(current_default["enabled"]):
                replacement = connection.execute(
                    """
                    SELECT model_id FROM provider_models
                    WHERE provider_profile_id = ? AND enabled = 1
                    ORDER BY display_name COLLATE NOCASE, model_id COLLATE NOCASE
                    LIMIT 1
                    """,
                    (profile_id,),
                ).fetchone()
                connection.execute(
                    """
                    UPDATE provider_profiles
                    SET default_model = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        replacement["model_id"] if replacement is not None else "",
                        now,
                        profile_id,
                    ),
                )
        return self.list_models(profile_id)

    def set_default_model(self, profile_id: str, model_id: str) -> ProviderProfile:
        normalized_id = self._validated_model_id(model_id)
        with self._database.transaction() as connection:
            model = connection.execute(
                """
                SELECT enabled FROM provider_models
                WHERE provider_profile_id = ? AND model_id = ?
                """,
                (profile_id, normalized_id),
            ).fetchone()
            if model is None:
                raise NotFoundError(
                    f"Provider model not found: {profile_id}/{normalized_id}"
                )
            if not bool(model["enabled"]):
                raise ValidationError("A disabled model cannot be the default.")
            updated = connection.execute(
                """
                UPDATE provider_profiles
                SET default_model = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized_id, self._clock(), profile_id),
            )
            if updated.rowcount == 0:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
        return self.get_profile(profile_id)

    def set_connection_state(
        self,
        profile_id: str,
        *,
        state: str,
        error: Optional[str] = None,
    ) -> ProviderProfile:
        if state not in CONNECTION_STATES:
            raise ValidationError(f"Unsupported provider connection state: {state}")
        now = self._clock()
        with self._database.transaction() as connection:
            updated = connection.execute(
                """
                UPDATE provider_profiles
                SET connection_state = ?, last_checked_at = ?, last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (state, now, error, now, profile_id),
            )
            if updated.rowcount == 0:
                raise NotFoundError(f"Provider profile not found: {profile_id}")
        return self.get_profile(profile_id)

    def _validated_values(self, draft: ProviderProfileDraft) -> dict[str, object]:
        name = " ".join((draft.name or "").split())
        model = " ".join((draft.default_model or "").split())
        if not name or len(name) > MAX_NAME_CHARS:
            raise ValidationError("Provider name must contain 1-60 characters.")
        if model:
            self._validated_model_id(model)
        timeout = float(draft.timeout_seconds)
        if not 0 < timeout <= 600:
            raise ValidationError("Provider timeout must be between 1 and 600 seconds.")
        api_key_ref = (draft.api_key_ref or "").strip()
        is_environment_reference = (
            api_key_ref.startswith("${")
            and api_key_ref.endswith("}")
            and len(api_key_ref) > 3
            and api_key_ref[2:-1].isidentifier()
        )
        is_stored_reference = api_key_ref.startswith("secret://provider/") and len(
            api_key_ref
        ) > len("secret://provider/")
        if api_key_ref and not (is_environment_reference or is_stored_reference):
            raise ValidationError(
                "API key reference must use an environment or stored secret reference."
            )
        return {
            "name": name,
            "base_url": (draft.base_url or "").strip(),
            "api_key_ref": api_key_ref,
            "default_model": model,
            "timeout_seconds": timeout,
            "enabled": bool(draft.enabled),
        }

    @staticmethod
    def _validated_model_id(model_id: str) -> str:
        value = (model_id or "").strip()
        if (
            not value
            or len(value) > MAX_MODEL_ID_CHARS
            or any(character.isspace() for character in value)
        ):
            raise ValidationError(
                f"Model ID must contain 1-{MAX_MODEL_ID_CHARS} non-space characters."
            )
        return value

    def _upsert_model(
        self,
        connection,
        *,
        provider_profile_id: str,
        model_id: str,
        display_name: str,
        source: str,
        enabled: bool,
        last_seen_at: Optional[str],
        now: str,
        preserve_enabled: bool = False,
    ) -> None:
        normalized_id = self._validated_model_id(model_id)
        normalized_name = " ".join((display_name or "").split()) or normalized_id
        if source not in MODEL_SOURCES:
            raise ValidationError(f"Unsupported provider model source: {source}")
        current = connection.execute(
            """
            SELECT source, enabled FROM provider_models
            WHERE provider_profile_id = ? AND model_id = ?
            """,
            (provider_profile_id, normalized_id),
        ).fetchone()
        if current is None:
            connection.execute(
                """
                INSERT INTO provider_models (
                    provider_profile_id, model_id, display_name, source, enabled,
                    created_at, updated_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_profile_id,
                    normalized_id,
                    normalized_name,
                    source,
                    1 if enabled else 0,
                    now,
                    now,
                    last_seen_at,
                ),
            )
            return
        next_source = "manual" if current["source"] == "manual" else source
        next_enabled = bool(current["enabled"]) if preserve_enabled else enabled
        connection.execute(
            """
            UPDATE provider_models
            SET display_name = ?, source = ?, enabled = ?, updated_at = ?,
                last_seen_at = COALESCE(?, last_seen_at)
            WHERE provider_profile_id = ? AND model_id = ?
            """,
            (
                normalized_name,
                next_source,
                1 if next_enabled else 0,
                now,
                last_seen_at,
                provider_profile_id,
                normalized_id,
            ),
        )

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
            connection_state=row["connection_state"],
            last_checked_at=row["last_checked_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _model(row) -> ProviderModel:
        return ProviderModel(
            provider_profile_id=row["provider_profile_id"],
            model_id=row["model_id"],
            display_name=row["display_name"],
            source=row["source"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
        )
