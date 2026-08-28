from __future__ import annotations

import os
import sys
from pathlib import Path

marker = Path(sys.argv[1])
if marker.exists():
    sys.exit(2)
marker.write_text("started", encoding="utf-8")

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def crash() -> str:
    os._exit(1)


if __name__ == "__main__":
    mcp.run()
