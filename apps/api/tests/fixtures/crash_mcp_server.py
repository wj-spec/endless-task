from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return text


@mcp.tool()
def crash() -> str:
    os._exit(1)


if __name__ == "__main__":
    mcp.run()
