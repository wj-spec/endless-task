from __future__ import annotations

import math
import re
from typing import Any, Mapping


class ToolSchemaError(ValueError):
    pass


_SUPPORTED_KEYWORDS = {
    "$schema",
    "title",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
}
_SUPPORTED_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def check_tool_schema(schema: Mapping[str, Any], *, path: str = "$") -> None:
    unknown = set(schema) - _SUPPORTED_KEYWORDS
    if unknown:
        raise ToolSchemaError(
            f"{path} uses unsupported schema keywords: {', '.join(sorted(unknown))}"
        )
    value_type = schema.get("type")
    if value_type not in _SUPPORTED_TYPES:
        raise ToolSchemaError(f"{path}.type must be one supported JSON type")

    if "enum" in schema and not isinstance(schema["enum"], (list, tuple)):
        raise ToolSchemaError(f"{path}.enum must be an array")
    for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ToolSchemaError(f"{path}.{keyword} must be a non-negative integer")
    for keyword in ("minimum", "maximum"):
        value = schema.get(keyword)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise ToolSchemaError(f"{path}.{keyword} must be a finite number")
    pattern = schema.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise ToolSchemaError(f"{path}.pattern must be text")
        try:
            re.compile(pattern)
        except re.error as error:
            raise ToolSchemaError(f"{path}.pattern must be a valid expression") from error

    if value_type == "object":
        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise ToolSchemaError(f"{path}.properties must be an object")
        required = schema.get("required", ())
        if not isinstance(required, (list, tuple)) or any(
            not isinstance(item, str) for item in required
        ):
            raise ToolSchemaError(f"{path}.required must be an array of names")
        if len(set(required)) != len(required):
            raise ToolSchemaError(f"{path}.required cannot contain duplicates")
        missing = set(required) - set(properties)
        if missing:
            raise ToolSchemaError(f"{path}.required refers to an unknown property")
        additional = schema.get("additionalProperties", True)
        if not isinstance(additional, (bool, Mapping)):
            raise ToolSchemaError(
                f"{path}.additionalProperties must be boolean or a schema"
            )
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, Mapping):
                raise ToolSchemaError(f"{path}.properties must contain schemas")
            check_tool_schema(child, path=f"{path}.properties.{name}")
        if isinstance(additional, Mapping):
            check_tool_schema(additional, path=f"{path}.additionalProperties")

    if value_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            raise ToolSchemaError(f"{path}.items must be a schema")
        check_tool_schema(items, path=f"{path}.items")


def validate_tool_arguments(
    schema: Mapping[str, Any],
    value: Any,
    *,
    path: str = "$",
) -> None:
    expected = schema["type"]
    if not _matches_type(expected, value):
        raise ToolSchemaError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolSchemaError(f"{path} is not an allowed value")
    if "const" in schema and value != schema["const"]:
        raise ToolSchemaError(f"{path} does not match the required value")

    if expected == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                raise ToolSchemaError(f"{path}.{name} is required")
        additional = schema.get("additionalProperties", True)
        for name, child_value in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                validate_tool_arguments(child_schema, child_value, path=f"{path}.{name}")
            elif additional is False:
                raise ToolSchemaError(f"{path}.{name} is not allowed")
            elif isinstance(additional, Mapping):
                validate_tool_arguments(additional, child_value, path=f"{path}.{name}")
    elif expected == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            raise ToolSchemaError(f"{path} has too few items")
        if maximum is not None and len(value) > maximum:
            raise ToolSchemaError(f"{path} has too many items")
        for index, item in enumerate(value):
            validate_tool_arguments(schema["items"], item, path=f"{path}[{index}]")
    elif expected == "string":
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < minimum:
            raise ToolSchemaError(f"{path} is too short")
        if maximum is not None and len(value) > maximum:
            raise ToolSchemaError(f"{path} is too long")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ToolSchemaError(f"{path} does not match the required pattern")
    elif expected in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ToolSchemaError(f"{path} is below the minimum")
        if maximum is not None and value > maximum:
            raise ToolSchemaError(f"{path} is above the maximum")


def _matches_type(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False
