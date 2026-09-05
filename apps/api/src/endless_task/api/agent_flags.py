"""Agent Platform feature-flag registry + rollback drill (G1 运维).

09 §6 requires every feature flag to define default/valid values and a
delete condition; 09 §9 运维 requires a feature-flag rollback drill. This
module is the single registry the CLI and future UI read:

- :data:`AGENT_PLATFORM_FLAGS` enumerates the eight platform flags with
  their env name, settings field, current/default value and wiring state,
- :func:`rollback_dry_run` rebuilds the container with one flag forced to
  its rollback (off) value and reports what changed, so an operator can
  prove a rollback is safe before touching production env.

Flags whose wiring is not integrated yet (no settings field / no
consumer) are reported as ``wired=false`` and their rollback is a no-op
by definition — the drill never pretends a rollback matters for a flag
that does nothing today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

#: Settings-field -> off/rollback value for the platform flags we can flip.
_OFF_VALUES: dict[str, object] = {
    "tool_platform_v2_enabled": False,
    "context_engine_v2_enabled": False,
    "provider_retry_mode": "0",
    "delegation_mode": "0",
    "skill_packages_enabled": False,
    "runtime_trace_mode": "0",
    "otel_export_mode": "0",
}

#: Env-name -> settings field (wired flags only).
_ENV_TO_FIELD: dict[str, str] = {
    "ENDLESS_TASK_TOOL_PLATFORM_V2": "tool_platform_v2_enabled",
    "ENDLESS_TASK_CONTEXT_ENGINE_V2": "context_engine_v2_enabled",
    "ENDLESS_TASK_PROVIDER_RETRY_V2": "provider_retry_mode",
    "ENDLESS_TASK_DELEGATION": "delegation_mode",
    "ENDLESS_TASK_SKILL_PACKAGES": "skill_packages_enabled",
    "ENDLESS_TASK_RUNTIME_TRACE": "runtime_trace_mode",
    "ENDLESS_TASK_OTEL_EXPORT": "otel_export_mode",
}

#: Flags declared in 09 §6 with no production wiring yet (reported, not
#: pretended to be rollback-relevant).
_UNWIRED_FLAGS: tuple[str, ...] = (
    "ENDLESS_TASK_STOP_POLICY_V2",
    "ENDLESS_TASK_EXECUTION_BACKEND",
)


@dataclass(frozen=True)
class AgentPlatformFlag:
    env_name: str
    field: Optional[str]
    current_value: object
    rollback_value: object
    wired: bool


def agent_platform_flags(settings) -> tuple[AgentPlatformFlag, ...]:
    """Enumerate the eight platform flags with live settings values."""
    rows: list[AgentPlatformFlag] = []
    for env_name, field in _ENV_TO_FIELD.items():
        current = getattr(settings, field)
        rows.append(
            AgentPlatformFlag(
                env_name=env_name,
                field=field,
                current_value=current,
                rollback_value=_OFF_VALUES[field],
                wired=True,
            )
        )
    for env_name in _UNWIRED_FLAGS:
        rows.append(
            AgentPlatformFlag(
                env_name=env_name,
                field=None,
                current_value=None,
                rollback_value=None,
                wired=False,
            )
        )
    return tuple(rows)


def rollback_dry_run(settings, flag_env_name: str) -> dict:
    """Build the container with one flag forced off and describe the diff.

    Returns a dict with ``flag``, ``field``, ``before``/``after`` settings
    values, ``wired`` and ``containerBuilt``. Unwired flags report
    ``containerBuilt=False`` (nothing to exercise).
    """
    field = _ENV_TO_FIELD.get(flag_env_name)
    if field is None:
        return {
            "flag": flag_env_name,
            "field": None,
            "wired": False,
            "containerBuilt": False,
            "note": "flag 未接线（无生产消费），回滚演练为空操作",
        }
    before = getattr(settings, field)
    off_value = _OFF_VALUES[field]
    try:
        from dataclasses import replace

        rolled_back = replace(settings, **{field: off_value})
    except Exception as error:  # pragma: no cover - dataclass replace
        return {
            "flag": flag_env_name,
            "field": field,
            "wired": True,
            "containerBuilt": False,
            "note": f"无法构造关闭态 settings: {error}",
        }
    try:
        from endless_task.api.app import _build_container
        from endless_task.runtime import FakeProvider

        _build_container(rolled_back, FakeProvider(chunks=("ok",)), None)
        built = True
    except Exception as error:
        built = False
        note = f"关闭 {flag_env_name} 后容器构建失败: {error}"
    else:
        note = "关闭后容器可构建（回滚安全）"
    return {
        "flag": flag_env_name,
        "field": field,
        "before": before,
        "after": off_value,
        "wired": True,
        "containerBuilt": built,
        "note": note,
    }


__all__ = [
    "AGENT_PLATFORM_FLAGS_NAMES",
    "AgentPlatformFlag",
    "agent_platform_flags",
    "rollback_dry_run",
]

AGENT_PLATFORM_FLAGS_NAMES = tuple(_ENV_TO_FIELD) + _UNWIRED_FLAGS
