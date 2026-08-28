from __future__ import annotations

import os
import threading
import time

from mcp.server.fastmcp import FastMCP


def exit_soon() -> None:
    time.sleep(0.05)
    os._exit(1)


threading.Thread(target=exit_soon, daemon=True).start()

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    mcp.run()
