from __future__ import annotations

import hashlib
import re

_NORMALIZE = re.compile(r"[^a-z0-9_]")


def public_tool_name(server_name: str, raw_name: str) -> str:
    """Deterministically namespace an MCP tool for the model-visible registry."""
    joined = f"mcp__{server_name}__{raw_name}"
    normalized = _NORMALIZE.sub("_", joined.lower())
    if normalized == joined and len(normalized) <= 64:
        return normalized
    digest = hashlib.sha256(f"{server_name}\0{raw_name}".encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:51]}_{digest}"
