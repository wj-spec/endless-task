from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    mcp.run()
