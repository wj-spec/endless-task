from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_STORED_PREFIX = "secret://provider/"
_MAX_SECRET_CHARS = 16_384


class ProviderSecretStore:
    """Application-private provider credentials stored outside the database."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def reference_for(profile_id: str) -> str:
        return f"{_STORED_PREFIX}{profile_id}"

    def has(self, reference: str) -> bool:
        value = (reference or "").strip()
        if not value:
            return False
        environment = _ENV_REF.match(value)
        if environment:
            return bool(os.environ.get(environment.group(1)))
        if value.startswith(_STORED_PREFIX):
            secret_id = value.removeprefix(_STORED_PREFIX)
            return bool(secret_id and self._read_all().get(secret_id))
        return True

    def resolve(self, reference: str) -> Optional[str]:
        value = (reference or "").strip()
        if not value:
            return None
        environment = _ENV_REF.match(value)
        if environment:
            return os.environ.get(environment.group(1))
        if value.startswith(_STORED_PREFIX):
            secret_id = value.removeprefix(_STORED_PREFIX)
            return self._read_all().get(secret_id) or None
        return value

    def put(self, profile_id: str, secret: str) -> str:
        value = secret.strip()
        if not value:
            raise ValueError("API key cannot be empty")
        if len(value) > _MAX_SECRET_CHARS:
            raise ValueError("API key is too long")
        with self._lock:
            secrets = self._read_all()
            secrets[profile_id] = value
            self._write_all(secrets)
        return self.reference_for(profile_id)

    def delete(self, profile_id: str) -> None:
        with self._lock:
            secrets = self._read_all()
            if profile_id not in secrets:
                return
            del secrets[profile_id]
            self._write_all(secrets)

    def _read_all(self) -> dict[str, str]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Provider credential store is unavailable") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Provider credential store is invalid")
        return {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(key, str) and isinstance(value, str)
        }

    def _write_all(self, secrets: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.parent / f".{self._path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                json.dump(secrets, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            try:
                self._path.chmod(0o600)
            except OSError:
                pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)