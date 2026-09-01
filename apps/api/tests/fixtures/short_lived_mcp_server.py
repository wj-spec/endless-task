from __future__ import annotations

import os
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP


def exit_soon() -> None:
    # 给足时间建立 stdio 连接并进入健康检查后再退出；若在握手完成前退出，
    # start_all 尚未返回时进程已消失，连接丢失在 start_all 期间就被检测并重连，
    # 轮询循环来不及观察到 reconnecting 态。2s 保证 start_all 返回时进程仍存活。
    # 退出前显式关闭 stdin/stdout，让对端 MCP 客户端可靠观察到 EOF，从而稳定判定
    # 空闲连接丢失（仅 os._exit 偶发不被客户端读循环立即感知）。
    time.sleep(2.0)
    try:
        sys.stdout.flush()
        sys.stdout.close()
        sys.stderr.close()
        try:
            sys.stdin.close()
        except OSError:
            pass
    finally:
        os._exit(1)


threading.Thread(target=exit_soon, daemon=True).start()

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return text


if __name__ == "__main__":
    mcp.run()
