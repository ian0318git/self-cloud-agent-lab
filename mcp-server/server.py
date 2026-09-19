"""mcp-test-server —— 最小的 Streamable HTTP MCP server，為第二階段 MCP 清單而存在。

提供兩個工具：
- echo(text)        —— 原樣回傳，驗證最基本的工具呼叫
- roll_die(sides=6) —— 回傳 1..sides 隨機整數；「擲兩次骰子並加總」就是
                      現成的多輪（連續兩次工具呼叫）測試題

刻意不設認證：這個 server 只存在於 compose 的 ai-net 內網（不發布埠），
只有 open-webui 容器碰得到 —— 與 ollama 不發布 11434 埠同一個思路（D-003）。
"""

import random

from mcp.server.fastmcp import FastMCP

# host 必須是 0.0.0.0：mcp SDK 的 FastMCP 預設綁 127.0.0.1，
# 那會讓 open-webui（在別的容器、走 ai-net）連不上 —— 與 WEBUI_BIND_ADDR
# 同一類的 bind 位址陷阱（D-015）。綁 0.0.0.0 只影響容器內網：compose
# 不發布任何埠，外界照樣碰不到（D-003）。
mcp = FastMCP("mcp-test-server", host="0.0.0.0")


@mcp.tool()
def echo(text: str) -> str:
    """原樣回傳輸入的字串。"""
    return text


@mcp.tool()
def roll_die(sides: int = 6) -> int:
    """擲一顆 sides 面的骰子，回傳 1 到 sides 之間的整數。"""
    return random.randint(1, sides)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
