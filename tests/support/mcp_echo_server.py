"""A tiny MCP server run as a stdio subprocess by the MCP client tests.

Run it with ``python mcp_echo_server.py``; it serves until its standard input
closes.
"""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool(description="Echo the text back.", structured_output=False)
def echo(text: str) -> str:
    return text


@server.tool(description="Return the server's process ID.", structured_output=False)
def pid() -> str:
    return str(os.getpid())


if __name__ == "__main__":
    server.run("stdio")
