"""v2 message-API test helpers.

The removed v1 HTTP surface exposed a submit endpoint
(``POST /conversations/{id}/turns``) plus a turn poll (``GET /turns/{id}``).
Runtime v2 replaces both with ``POST /api/v2/conversations/{id}/messages`` and
the snapshot/events endpoints.  These helpers recreate the same ergonomics so
existing feature tests can drive the v2 runtime with minimal churn.

Terminal waiting is done against the in-process container's v2 repository, which
mirrors how ``test_runtime_v2_api`` waits on ``get_run(...).status``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from endless_task.runtime_v2 import RunStatus

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


async def send_message(
    client: httpx.AsyncClient,
    conversation_id: str,
    content: str,
    *,
    idempotency_key: str,
    lane_id: Optional[str] = None,
) -> dict[str, Any]:
    """Submit a v2 message and return the created-run handle."""
    payload: dict[str, Any] = {"content": content}
    if lane_id is not None:
        payload["laneId"] = lane_id
    response = await client.post(
        f"/api/v2/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": idempotency_key},
        json=payload,
    )
    if response.status_code != 202:
        raise AssertionError(f"v2 message submit failed: {response.text}")
    return response.json()


async def wait_for_run_terminal(
    container: Any,
    run_id: str,
    *,
    timeout: float = 4.0,
    interval: float = 0.005,
) -> RunStatus:
    """Poll the in-process repository until the run reaches a terminal status."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status = container.runtime_v2_repository.get_run(run_id).status
        if status in TERMINAL_RUN_STATUSES:
            return status
        await asyncio.sleep(interval)
    raise AssertionError(f"Run {run_id} did not reach a terminal status in time")


async def run_snapshot(
    client: httpx.AsyncClient,
    conversation_id: str,
    *,
    lane_id: Optional[str] = None,
) -> dict[str, Any]:
    """Fetch the v2 conversation snapshot."""
    url = f"/api/v2/conversations/{conversation_id}/snapshot"
    if lane_id is not None:
        url = f"{url}?lane_id={lane_id}"
    response = await client.get(url)
    if response.status_code != 200:
        raise AssertionError(f"v2 snapshot failed: {response.text}")
    return response.json()


def assistant_text(snapshot: dict[str, Any]) -> str:
    """Concatenate assistant-role entry texts from a v2 snapshot.

    Entries carry an ``actor`` ("assistant"/"user") and a ``type``
    (e.g. "assistant_message"); assistant text lives under ``data.content``.
    """
    parts: list[str] = []
    for entry in snapshot.get("entries", ()):
        if entry.get("actor") != "assistant":
            continue
        data = entry.get("data", {}) or {}
        if isinstance(data.get("content"), str):
            parts.append(data["content"])
    return "".join(parts)
