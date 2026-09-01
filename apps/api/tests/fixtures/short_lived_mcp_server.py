from __future__ import annotations

import os
import threading
import time

from mcp.server.fastmcp import FastMCP


def exit_soon() -> None:
    # 先给足时间建立 stdio 连接并进入健康检查，再退出；否则进程在握手前退出，
    # 导致连接丢失要么不被健康检查检测到、要么在握手期就失败，测试结果不稳定。
    time.sleep(0.5)
    os._exit(1)


threading.Thread(target=exit_soon, daemon=True).start()

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    mcp.run()
