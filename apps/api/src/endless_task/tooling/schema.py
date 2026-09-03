from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


class ToolSchemaError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        path: str | None = None,
        keyword: str | None = None,
        expected: Any = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.keyword = keyword
        self.expected = expected


def _plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _canonical_schema(schema: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            _plain_json(schema),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as error:
        raise ToolSchemaError("Tool input schema must be JSON-compatible") from error


@lru_cache(maxsize=256)
def _compiled_validator(canonical_schema: str) -> Draft202012Validator:
    schema = json.loads(canonical_schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ToolSchemaError(f"Invalid Draft 2020-12 schema: {error.message}") from error
    return Draft202012Validator(schema)


def check_tool_schema(schema: Mapping[str, Any], *, path: str = "$") -> None:
    del path
    _compiled_validator(_canonical_schema(schema))


def validate_tool_arguments(
    schema: Mapping[str, Any],
    value: Any,
    *,
    path: str = "$",
) -> None:
    validator = _compiled_validator(_canonical_schema(schema))
    try:
        errors = sorted(
            validator.iter_errors(_plain_json(value)),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator),
                error.message,
            ),
        )
    except Exception as error:
        raise ToolSchemaError("Tool input schema could not be evaluated") from error
    if not errors:
        return
    error = errors[0]
    validation_path = _validation_path(error, root=path)
    raise ToolSchemaError(
        f"{validation_path}: {error.message}",
        path=validation_path,
        keyword=(str(error.validator) if error.validator is not None else None),
        expected=_plain_json(error.validator_value),
    ) from error


def _validation_path(error: ValidationError, *, root: str) -> str:
    result = root
    for part in error.absolute_path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            result += f".{part}"
        else:
            result += f"[{json.dumps(part, ensure_ascii=False)}]"
    return result