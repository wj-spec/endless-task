from __future__ import annotations

import logging
import re
from typing import Iterable


class RedactingFormatter(logging.Formatter):
    """Formats application logs while removing common credential forms."""

    _patterns = (
        re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
        re.compile(r"(?i)((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    )

    def __init__(self, *args, secrets: Iterable[str] = (), **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(
            sorted((secret for secret in secrets if secret), key=len, reverse=True)
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        for pattern in self._patterns:
            rendered = pattern.sub(
                lambda match: (
                    f"{match.group(1)}[REDACTED]"
                    if match.lastindex
                    else "[REDACTED]"
                ),
                rendered,
            )
        return rendered


def configure_safe_logging(*, api_key: str | None = None) -> None:
    application_logger = logging.getLogger("endless_task")
    for handler in tuple(application_logger.handlers):
        if getattr(handler, "_endless_task_safe_handler", False):
            application_logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler._endless_task_safe_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            secrets=(api_key or "",),
        )
    )
    application_logger.addHandler(handler)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False
