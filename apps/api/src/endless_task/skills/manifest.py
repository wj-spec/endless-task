"""SKILL.md manifest v2 parsing and v1 compatibility (M5 SK-0).

Current skills are prompt documents discovered from a plain-key
frontmatter parser. SK-0 upgrades the manifest without breaking v1:

- ``schema-version`` absent or ``1`` -> v1 frontmatter, adapted with
  default provenance/visibility (07 §10 SK-0),
- ``schema-version: 2`` -> parsed with the safe YAML subset and validated
  against the version-2 JSON Schema (Draft 2020-12) declared below.

Manifest fields are declarative only: they express what the skill needs
(``required-tools``/``required-capabilities``), never grant capabilities
(07 §2.2). No custom YAML tags are executed (``yaml.safe_load`` only);
schema violations produce stable, structured diagnostics.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .models import SkillDiagnostic

#: Manifest schema version this module understands.
SUPPORTED_SCHEMA_VERSION = 2

#: JSON Schema (Draft 2020-12) for version-2 skill manifests. Field names
#: mirror 07 §4.1; validation stays declarative (README 4.7: jsonschema,
#: never hand-rolled validator semantics).
SKILL_MANIFEST_V2_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 64},
        "description": {"type": "string", "minLength": 1, "maxLength": 1024},
        "version": {
            "type": "string",
            "pattern": r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$",
        },
        "schema-version": {
            "type": "integer",
            "const": SUPPORTED_SCHEMA_VERSION,
        },
        "model-invocable": {"type": "boolean"},
        "user-invocable": {"type": "boolean"},
        "required-tools": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required-capabilities": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "optional-tools": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "conflicts-with": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 64},
        },
        "resource-policy": {
            "type": "string",
            "enum": ["package-only"],
        },
        # S3：可选展示/筛选字段（不参与调用判定，也不进模型目录）。
        "whenToUse": {"type": "string", "maxLength": 500},
        "metadata": {
            "type": "object",
            "additionalProperties": {
                "type": ["string", "number", "boolean"],
            },
        },
    },
    "required": ["name", "description", "version", "schema-version"],
    "additionalProperties": False,
}

_VALIDATOR = Draft202012Validator(SKILL_MANIFEST_V2_SCHEMA)


@dataclass(frozen=True)
class SkillManifest:
    """Versioned declarative manifest of one skill package (SK-0).

    ``version`` follows SemVer; ``digest`` is the sha256 of the raw
    SKILL.md bytes so a revision is content-addressed (07 §4.2). The
    ``provenance`` shape arrives with SK-1; SK-0 keeps the fields the
    parser must carry today.
    """

    name: str
    description: str
    version: str
    schema_version: int
    digest: str
    body: str
    model_invocable: bool = True
    user_invocable: bool = True
    required_tools: Tuple[str, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    optional_tools: Tuple[str, ...] = ()
    conflicts_with: Tuple[str, ...] = ()
    resource_policy: Optional[str] = None
    #: S3：人工可读的适用场景与自由元数据（不进模型目录）。
    when_to_use: str = ""
    metadata: Tuple[Tuple[str, str], ...] = ()
    diagnostics: Tuple[SkillDiagnostic, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return not self.diagnostics


def parse_skill_manifest(
    path: Path,
    text: str,
) -> SkillManifest:
    """Parse SKILL.md into a versioned :class:`SkillManifest`.

    v1 (no ``schema-version`` / plain-key frontmatter) is adapted with
    default visibility; v2 is schema-validated. Both share the same name,
    description and body semantics so downstream behavior is unchanged.
    """
    raw_frontmatter, body, parse_error, schema_version = _extract_frontmatter(
        text
    )
    diagnostics: list[SkillDiagnostic] = []
    if parse_error is not None:
        diagnostics.append(
            SkillDiagnostic(
                path=path,
                code=parse_error,
                message="缺少或格式错误的 frontmatter。",
            )
        )
    digest = _content_digest(text)

    if schema_version != 1:
        # Any declared schema-version other than 1 goes through the
        # version-2 schema validator (which rejects unsupported values via
        # ``const``) instead of silently falling back to the v1 adapter.
        manifest = _validate_v2(
            path=path,
            raw=raw_frontmatter,
            body=body,
            digest=digest,
            diagnostics=diagnostics,
        )
        return manifest

    # v1 adapter: plain-key frontmatter with default provenance/visibility.
    name = str(raw_frontmatter.get("name") or path.parent.name)
    description = str(raw_frontmatter.get("description") or "")
    disable_model = _frontmatter_bool(
        raw_frontmatter.get("disable-model-invocation", "")
    )
    # S1：v1 也认 ``user-invocable``（与 ``disable-model-invocation`` 同一文法），
    # 让老技能无需升级 schema-version 就能声明"仅模型调用"。
    user_invocable = _frontmatter_bool(
        raw_frontmatter.get("user-invocable", ""), default=True
    )
    if not name.strip():
        diagnostics.append(
            SkillDiagnostic(path=path, code="invalid_name", message="技能名缺失。")
        )
    if not description.strip():
        diagnostics.append(
            SkillDiagnostic(
                path=path,
                code="missing_description",
                message="缺少 description。",
            )
        )
    return SkillManifest(
        name=name,
        description=description,
        version="0.0.0",
        schema_version=1,
        digest=digest,
        body=body,
        model_invocable=not disable_model,
        user_invocable=user_invocable,
        when_to_use=str(raw_frontmatter.get("whenToUse") or ""),
        diagnostics=tuple(diagnostics),
    )


def _frontmatter_bool(value: object, *, default: bool = False) -> bool:
    """frontmatter 布尔文法：YAML 真布尔直接采用；字符串按 true/1/yes/on 判定。

    注意：YAML 解析后拿到的可能是 ``False``，而 ``False or ""`` 会退化成空串，
    因此必须先判断类型，否则 ``user-invocable: false`` 会被当成未提供。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("true", "1", "yes", "on")


def _extract_frontmatter(
    text: str,
) -> tuple[Mapping[str, Any], str, Optional[str], int]:
    """Split frontmatter from body; return (parsed, body, error, schema_version).

    v1 与 v2 都优先用 ``yaml.safe_load``：真实世界的技能 frontmatter 常见
    列表、嵌套 metadata、注释（例如 ``~/.claude/skills`` 里的共享技能会有
    ``metadata.openclaw.requires.bins:`` 这样的嵌套块），手写的逐行解析会把
    这些完全合法的 YAML 判成格式错误。只有在 YAML 解析失败时才退回逐行
    plain-key 解析（保持历史兼容）。
    """
    if not text.startswith("---"):
        return {}, text, "missing_frontmatter", 1
    lines = text.splitlines()
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None or end == 1:
        return {}, text, "missing_frontmatter", 1
    frontmatter_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).strip()
    declares_schema = "schema-version:" in frontmatter_text

    loaded: Any = None
    yaml_failed = False
    try:
        loaded = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        yaml_failed = True
    if isinstance(loaded, Mapping):
        schema_version = loaded.get("schema-version")
        if isinstance(schema_version, int) and schema_version != 1:
            return dict(loaded), body, None, schema_version
        if declares_schema and not isinstance(schema_version, int):
            # 声明了 schema-version 但不是整数：保持"按 v2 处理并报错"的历史语义。
            return {}, body, "invalid_frontmatter", 2
        # 其余情况一律按 v1 适配（开放字段直接可用）。
        return dict(loaded), body, None, 1
    if declares_schema or not yaml_failed:
        return {}, body, "invalid_frontmatter", 2 if declares_schema else 1

    # YAML 解析失败：退回逐行 plain-key 解析。
    parsed: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            return {}, text, "invalid_frontmatter", 1
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip().strip("\"'")
    return parsed, body, None, 1


def _validate_v2(
    *,
    path: Path,
    raw: Mapping[str, Any],
    body: str,
    digest: str,
    diagnostics: list[SkillDiagnostic],
) -> SkillManifest:
    try:
        _VALIDATOR.validate(dict(raw))
    except ValidationError as error:
        diagnostics.append(
            SkillDiagnostic(
                path=path,
                code="invalid_manifest",
                message=f"manifest 不符合 schema-version=2 规范：{error.message}",
            )
        )
        return SkillManifest(
            name=str(raw.get("name") or path.parent.name),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or "0.0.0"),
            schema_version=SUPPORTED_SCHEMA_VERSION,
            digest=digest,
            body=body,
            diagnostics=tuple(diagnostics),
        )
    required_tools = raw.get("required-tools") or []
    required_capabilities = raw.get("required-capabilities") or []
    optional_tools = raw.get("optional-tools") or []
    conflicts = raw.get("conflicts-with") or []
    if not body.strip():
        diagnostics.append(
            SkillDiagnostic(path=path, code="empty_body", message="技能正文不能为空。")
        )
    return SkillManifest(
        name=str(raw["name"]),
        description=str(raw["description"]),
        version=str(raw["version"]),
        schema_version=SUPPORTED_SCHEMA_VERSION,
        digest=digest,
        body=body,
        model_invocable=bool(raw.get("model-invocable", True)),
        user_invocable=bool(raw.get("user-invocable", True)),
        required_tools=tuple(str(item) for item in required_tools),
        required_capabilities=tuple(str(item) for item in required_capabilities),
        optional_tools=tuple(str(item) for item in optional_tools),
        conflicts_with=tuple(str(item) for item in conflicts),
        resource_policy=raw.get("resource-policy"),
        when_to_use=str(raw.get("whenToUse") or ""),
        metadata=tuple(
            (str(key), str(value))
            for key, value in (raw.get("metadata") or {}).items()
        )
        if isinstance(raw.get("metadata"), Mapping)
        else (),
        diagnostics=tuple(diagnostics),
    )


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "SKILL_MANIFEST_V2_SCHEMA",
    "SkillManifest",
    "parse_skill_manifest",
]
