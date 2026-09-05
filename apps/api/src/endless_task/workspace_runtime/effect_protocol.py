"""Protocol receipt mapping for workspace side effects (RS-6 slice 2b-C).

05 §RS-6 / G1 security: real tool effects (file write/delete, shell) must
surface as *protocol* :class:`EffectReceipt` (agent_platform) so the
committed/unknown outcome is auditable end-to-end (runtime_ledger records
them as safety-critical events). The workspace_runtime receipt (this
package) stays the audit-log schema the UI depends on.

:func:`to_protocol_fields` maps a workspace receipt plus execution
context into the fields a protocol receipt needs, without importing the
protocol types here (dependency direction: workspace_runtime stays free of
agent_platform/execution imports — callers assemble the protocol object).
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .effect_log import EffectReceipt as WorkspaceReceipt

#: workspace kind -> protocol effect_type vocabulary (05 §4.4).
_KIND_TO_EFFECT_TYPE = {
    "file_write": "file_write",
    "file_delete": "file_delete",
    "shell": "process",
}


def to_protocol_fields(
    receipt: WorkspaceReceipt,
    *,
    effect_id: str,
    tool_call_id: str,
    backend: str,
    safe_summary: Optional[str] = None,
) -> Mapping[str, Any]:
    """Map a workspace receipt to protocol EffectReceipt constructor fields.

    Outcome semantics (fail closed, never "not executed"):

    - ``unknown_outcome=True`` -> ``unknown`` (side effect may have
      happened but the audit log could not prove it),
    - ``exit_code is None`` (shell timeout/kill) -> ``unknown``,
    - otherwise -> ``committed`` (the operation did run).

    ``committed_at`` is only set when committed (protocol invariant).
    """
    effect_type = _KIND_TO_EFFECT_TYPE.get(receipt.kind, receipt.kind)
    # Shell timeout/kill leaves exit_code None -> unknown (cannot prove the
    # command finished). File ops carry no exit_code; only the audit-log
    # write failure (unknown_outcome) makes them unknown.
    unresolved_exit = receipt.kind == "shell" and receipt.exit_code is None
    unknown = bool(receipt.unknown_outcome) or unresolved_exit
    outcome_value = "unknown" if unknown else "committed"
    summary = safe_summary or _default_summary(receipt)
    return {
        "effect_id": effect_id,
        "tool_call_id": tool_call_id,
        "effect_type": effect_type,
        "target": receipt.path,
        "started_at": receipt.executed_at or "",
        "outcome": outcome_value,
        "backend": backend,
        "safe_summary": summary,
        "committed_at": None if unknown else (receipt.executed_at or None),
    }


def _default_summary(receipt: WorkspaceReceipt) -> str:
    if receipt.kind == "shell":
        if receipt.timed_out:
            return "已执行 shell 命令但超时，结果需人工核对。"
        if receipt.exit_code is not None and receipt.exit_code != 0:
            return f"已执行 shell 命令，退出码 {receipt.exit_code}。"
        return "已执行 shell 命令。"
    if receipt.kind == "file_delete":
        return f"已删除工作区文件：{receipt.path}。"
    return f"已写入工作区文件：{receipt.path}。"


__all__ = ["to_protocol_fields"]
