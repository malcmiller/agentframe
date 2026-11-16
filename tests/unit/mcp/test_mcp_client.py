"""``MCPClient``: configuration checks, tool selection and the connection.

Most tests use an in-process SDK ``MCPServer``. Two run a real stdio
subprocess (``tests/support/mcp_echo_server.py``): it stops on ``aclose``,
and when it dies the run fails and the next use reconnects. One serves
streamable HTTP on localhost.
"""

import asyncio
import math
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import Any

import pytest
from mcp import StdioServerParameters
from mcp.server.mcpserver import MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

from agentframe import (
    Agent,
    APIKeyCredential,
    DefinitionError,
    MCPClient,
    MCPServerError,
    ModelCapabilities,
    RunContext,
    Runtime,
    ToolCall,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from local_server import serve_locally

ECHO_SERVER = Path(__file__).parents[2] / "support" / "mcp_echo_server.py"


def echo_server() -> MCPServer:
    server = MCPServer("echo")

    @server.tool(description="Echo the text back.", structured_output=False)
    def echo(text: str) -> str:
        return text

    return server


def stdio(*args: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=list(args))


@agent(name="Caller", description="Calls one MCP tool.", version="0.1.0")
class Caller(Agent):
    """Offers one MCP tool to a model scripted to call it once."""

    def __init__(self, client: MCPClient, tool: str, arguments: str) -> None:
        call = ToolCall("1", f"{client.name}_{tool}", arguments)
        model = ScriptedModel(
            [call, "done"],
            capabilities=ModelCapabilities(context_window=128_000, tools=True),
        )
        super().__init__(model=model)
        self.client = client
        self.tool = tool
        self.scripted = model

    @skill(id="call", description="Call the tool.", tags=["mcp"])
    async def call(self, ctx: RunContext) -> str:
        return await ctx.generate("Go", tools=await self.client.tools(self.tool))


async def call_tool(client: MCPClient, tool: str, arguments: str = "{}") -> str:
    """Return the result the model is given for one call to ``tool``."""
    caller = Caller(client, tool, arguments)
    await Runtime().invoke(caller, skill="call")
    [result] = [
        m.content for m in caller.scripted.requests[1].messages if m.role == "tool"
    ]
    return result


def process_exists(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=True,
        )
        return str(pid) in listed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize(
    ("server", "options", "message"),
    [
        (echo_server(), {"name": "two words"}, "must be letters"),
        ("ftp://tools.example.com/mcp", {}, "must be StdioServerParameters"),
        ("https://user:pw@tools.example.com/mcp", {}, "without credentials"),
        ("http://tools.example.com/mcp", {}, "must use https"),
        (
            "http://tools.example.com/mcp",
            {"allow_http_hosts": "tools.example.com"},
            "not a single string",
        ),
        (echo_server(), {"credential": APIKeyCredential("k")}, "only for a server URL"),
        (echo_server(), {"permissions": {"echo": "files.write"}}, "not a single"),
        (echo_server(), {"call_timeout_seconds": 0}, "greater than 0"),
        (echo_server(), {"connect_timeout_seconds": math.inf}, "must be finite"),
        (echo_server(), {"max_result_bytes": 1.5}, "whole number"),
        (echo_server(), {"max_result_bytes": True}, "whole number"),
    ],
)
def test_invalid_configuration_is_refused_without_connecting(
    server: Any, options: dict[str, Any], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        MCPClient(server, **{"name": "tools", **options})


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://tools.example.com/mcp", ()),
        ("http://127.0.0.1:8000/mcp", ()),
        ("http://localhost:8000/mcp", ()),
        ("http://tools:8000/mcp", ["tools"]),
    ],
)
def test_https_loopback_and_allowed_http_urls_are_accepted(
    url: str, allowed: Collection[str]
) -> None:
    MCPClient(url, name="tools", allow_http_hosts=allowed)


@pytest.fixture
async def client() -> AsyncIterator[MCPClient]:
    client = MCPClient(echo_server(), name="echo")
    yield client
    await client.aclose()


async def test_tools_returns_entries_with_the_qualified_name(
    client: MCPClient,
) -> None:
    [entry] = await client.tools("echo")

    assert entry.name == "echo.echo"
    assert entry.tool == "echo"
    assert entry.description == "Echo the text back."
    assert entry.input_schema["required"] == ["text"]
    assert entry.requires_permissions == frozenset()


@pytest.mark.parametrize(
    ("names", "message"),
    [
        ((), "at least one name"),
        (("echo", "echo"), "selected more than once"),
        (("echo", "delete_everything"), "no tools named 'delete_everything'"),
    ],
)
async def test_invalid_selections_are_refused(
    client: MCPClient, names: tuple[str, ...], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        await client.tools(*names)


async def test_permissions_for_a_tool_the_server_lacks_are_refused() -> None:
    client = MCPClient(echo_server(), name="echo", permissions={"ecko": ["x"]})

    try:
        with pytest.raises(DefinitionError, match="no tools named 'ecko'"):
            await client.tools("echo")
    finally:
        await client.aclose()


async def test_a_stdio_server_runs_until_aclose_and_reconnects_after() -> None:
    client = MCPClient(stdio(str(ECHO_SERVER)), name="echo")
    try:
        pid = int(await call_tool(client, "pid"))
        assert await call_tool(client, "echo", '{"text": "hi"}') == "hi"
        assert int(await call_tool(client, "pid")) == pid

        await client.aclose()

        assert not process_exists(pid)
        assert int(await call_tool(client, "pid")) != pid
    finally:
        await client.aclose()


async def test_a_server_that_dies_fails_the_run_and_the_next_use_reconnects() -> None:
    client = MCPClient(stdio(str(ECHO_SERVER)), name="echo")
    try:
        pid = int(await call_tool(client, "pid"))
        os.kill(pid, signal.SIGTERM)
        while process_exists(pid):
            await asyncio.sleep(0.05)

        with pytest.raises(MCPServerError, match="connection failed"):
            await call_tool(client, "pid")

        assert int(await call_tool(client, "pid")) != pid
    finally:
        await client.aclose()


async def test_a_server_that_exits_raises_mcp_server_error() -> None:
    client = MCPClient(stdio("-c", "pass"), name="gone")

    with pytest.raises(MCPServerError, match="could not be connected"):
        await client.connect()


async def test_a_server_that_never_answers_times_out() -> None:
    silent = stdio("-c", "import time; time.sleep(30)")
    client = MCPClient(silent, name="silent", connect_timeout_seconds=0.5)

    with pytest.raises(MCPServerError, match=r"did not connect within 0\.5 seconds"):
        await client.connect()


class RecordHeaders:
    """ASGI middleware that records each HTTP request's headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.headers: list[dict[str, str]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            self.headers.append({k.decode(): v.decode() for k, v in scope["headers"]})
        await self.app(scope, receive, send)


async def test_a_streamable_http_server_gets_the_credential_on_every_request() -> None:
    recorder = RecordHeaders(echo_server().streamable_http_app())

    async with serve_locally(lambda _: recorder) as base_url:  # type: ignore[arg-type,return-value]
        client = MCPClient(
            f"{base_url}/mcp", name="echo", credential=APIKeyCredential("k1")
        )
        try:
            assert await call_tool(client, "echo", '{"text": "hi"}') == "hi"
        finally:
            await client.aclose()

    assert recorder.headers
    assert all(headers["x-api-key"] == "k1" for headers in recorder.headers)
