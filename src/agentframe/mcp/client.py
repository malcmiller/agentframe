"""A connection to one MCP server, and the tools a skill selects from it.

``MCPClient`` is application-configured and shared by every run, like any
other injected service. Connecting offers nothing: a skill selects tools by
name with ``await client.tools(...)`` and passes the returned ``MCPTool``
entries to ``ctx.generate(tools=[...])``.

The official MCP SDK's transports are anyio context managers that must be
entered and left by the same task, so the connection lives in a background
task the client owns. Runs send their calls through it from their own tasks.
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, NoReturn, Self
from urllib.parse import urlsplit

from agentframe.discovery.identity import OutboundCredential
from agentframe.errors import DefinitionError, MCPServerError
from agentframe.orchestration.card_text import (
    MAX_SCHEMA_BYTES,
    bounded_schema,
    bounded_text,
    schema_size,
)
from agentframe.orchestration.catalog import safe_tool_name
from agentframe.runtime.tools import error_result
from agentframe.urls import is_loopback_host, is_plain_http_url

if TYPE_CHECKING:
    # The SDK is the optional ``mcp`` extra, so it is imported only when used.
    from mcp import Client, StdioServerParameters
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult, ContentBlock, Tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULT_BYTES = 1_048_576
"""The largest tool result, in UTF-8 bytes, a model is given: 1 MiB."""

MAX_TOOL_PAGES = 10
"""The most pages of a server's tool list read before giving up."""

_MAX_TOOL_NAME = 64
"""Providers accept tool names of 1-64 letters, digits, ``_`` or ``-``."""

_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")

# The SDK's own defaults for its HTTP client: a server may keep a response
# stream open, so reads wait longer than connecting does.
_HTTP_TIMEOUT_SECONDS = 30.0
_HTTP_READ_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True)
class MCPTool:
    """One MCP server tool a skill selected, ready for ``ctx.generate``.

    Returned by ``MCPClient.tools``; not meant to be constructed by
    application code. The description and schema come from the server:
    bounded but untrusted, so they reach a model only as a tool definition.

    Attributes:
        name: ``"<client>.<tool>"``, for example ``"files.read_file"``: the
            name execution policy tool checks see. The model sees it as the
            tool ``"files_read_file"``.
        tool: The tool's name on the server.
        description: The server's description of the tool, without control
            characters and at most 500 characters.
        input_schema: The JSON Schema of the tool's arguments, with its text
            bounded and its ``examples`` dropped. Treat it as read-only.
        requires_permissions: Permissions a caller must hold to use the tool,
            from the client's ``permissions=``; empty by default. A model is
            never offered a tool its caller lacks, and a call to one fails
            the run before anything is sent.
        client: The client the tool belongs to.
    """

    name: str
    tool: str
    description: str
    input_schema: dict[str, Any] = field(hash=False)
    requires_permissions: frozenset[str]
    client: "MCPClient" = field(repr=False, compare=False)


@dataclass
class _Connection:
    """One connection attempt, held open by ``task`` until ``stop`` is set.

    ``ready`` is set once ``client`` is open, or once the task has ended.
    """

    task: "asyncio.Task[None]" = field(init=False)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    client: "Client | None" = None


class MCPClient:
    """An application-configured MCP server whose tools skills may offer a model.

    Construction does no I/O. The connection opens on first use, or with
    ``connect()``, and is shared by every run. Register the client as an
    application-owned service and close it at shutdown::

        files = MCPClient(StdioServerParameters(command="files-server"), name="files")
        services.add(MCPClient, files)

    For several servers, register one subclass per server, for example
    ``class FilesMCP(MCPClient): ...``, and inject each by its class.

    No tool is offered to a model until a skill selects it with ``tools``.
    The client never offers servers sampling, elicitation or roots.

    Args:
        server: The server: ``StdioServerParameters`` to start the
            application's command as a subprocess speaking MCP on its
            standard input and output, or the URL of a streamable HTTP
            endpoint. URLs must use https, except to loopback hosts or hosts
            in ``allow_http_hosts``. An in-process SDK ``MCPServer`` is also
            accepted, for tests.
        name: Names this server's tools: the model sees ``<name>_<tool>``
            and policy checks see ``<name>.<tool>``. Letters, digits, ``_``
            and ``-`` only.
        credential: Supplies headers sent with every HTTP request, for
            example ``APIKeyCredential``. Only for a URL.
        permissions: Tool names on the server mapped to permissions a caller
            must all hold to use the tool, for example
            ``{"write_file": ["files.write"]}``. Tools not listed need none.
        connect_timeout_seconds: How long starting or connecting to the
            server may take. Defaults to 30.
        call_timeout_seconds: How long one tool call, or listing the tools,
            may take. Defaults to 60.
        max_result_bytes: The largest tool result a model is given, in
            UTF-8 bytes. A larger result is never cut short: the model gets a
            tool error instead. Defaults to 1 MiB.
        allow_http_hosts: Non-loopback hosts that may be reached over plain
            http, for development only.

    Raises:
        DefinitionError: The ``mcp`` extra is not installed, or an argument
            is invalid: a URL that is not plain http(s) or uses http to a
            host not allowed, a ``credential`` for a stdio server, an unsafe
            ``name``, a permission list given as one string, or a bound that
            is not greater than 0.
    """

    def __init__(
        self,
        server: "str | StdioServerParameters | MCPServer",
        *,
        name: str,
        credential: OutboundCredential | None = None,
        permissions: Mapping[str, Collection[str]] | None = None,
        connect_timeout_seconds: float = 30.0,
        call_timeout_seconds: float = 60.0,
        max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
        allow_http_hosts: Collection[str] = (),
    ) -> None:
        _require_sdk()
        self.name = _checked_name(name)
        self._server = _checked_server(server, allow_http_hosts)
        if credential is not None and not isinstance(server, str):
            raise DefinitionError(
                f"MCPClient '{name}' credential is only for a server URL."
            )
        self._credential = credential
        self._permissions = _checked_permissions(name, permissions)
        self._connect_timeout = _positive(
            name, "connect_timeout_seconds", connect_timeout_seconds
        )
        self._call_timeout = _positive(
            name, "call_timeout_seconds", call_timeout_seconds
        )
        self._max_result_bytes = int(
            _positive(name, "max_result_bytes", max_result_bytes, whole=True)
        )
        self._connection: _Connection | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def connect(self) -> None:
        """Open the connection now, for example at startup, to fail early.

        Raises:
            MCPServerError: The server did not start or connect within
                ``connect_timeout_seconds``.
        """
        await self._connected()

    async def aclose(self) -> None:
        """Close the connection, stopping a stdio server's process.

        Safe to call more than once; a later use connects again.
        """
        connection = self._connection
        if connection is not None:
            await self._stop(connection)

    async def tools(self, *names: str) -> tuple[MCPTool, ...]:
        """Select server tools by name, to offer a model with ``ctx.generate``.

        The server's tool list is read on each call (the SDK may serve it
        from its response cache).

        Args:
            *names: The tools' names on the server, at least one.

        Returns:
            One ``MCPTool`` per name, in order.

        Raises:
            DefinitionError: No name is given, a name is given twice or is
                not one of the server's tools, a ``permissions=`` key is not
                one of the server's tools, a tool's model name would be
                longer than 64 characters, or a tool's argument schema is
                larger than 8 KiB.
            MCPServerError: The server could not be reached or did not list
                its tools within ``call_timeout_seconds``.
        """
        _check_names(self.name, names)
        listed = await self._list_tools()
        unknown = [n for n in [*names, *self._permissions] if n not in listed]
        if unknown:
            raise DefinitionError(
                f"MCP server '{self.name}' has no tools named "
                f"{', '.join(repr(n) for n in dict.fromkeys(unknown))}."
            )
        return tuple(self._entry(listed[n]) for n in names)

    def _entry(self, tool: "Tool") -> MCPTool:
        """Return the bounded entry for one listed tool."""
        name = f"{self.name}.{tool.name}"
        if len(safe_tool_name(name)) > _MAX_TOOL_NAME:
            raise DefinitionError(
                f"MCP tool '{name}' cannot be a model tool: its name is longer "
                f"than {_MAX_TOOL_NAME} characters."
            )
        schema = bounded_schema(tool.input_schema)
        if schema_size(schema) > MAX_SCHEMA_BYTES:
            raise DefinitionError(
                f"MCP tool '{name}' has an argument schema larger than "
                f"{MAX_SCHEMA_BYTES} bytes."
            )
        return MCPTool(
            name=name,
            tool=tool.name,
            description=bounded_text(tool.description or tool.title or tool.name),
            input_schema=schema,
            requires_permissions=self._permissions.get(tool.name, frozenset()),
            client=self,
        )

    async def _list_tools(self) -> "dict[str, Tool]":
        """Return the server's tools by name, reading at most ten pages."""
        from mcp import MCPError

        connection, client = await self._connected()
        listed: dict[str, Tool] = {}
        cursor: str | None = None
        try:
            async with asyncio.timeout(self._call_timeout):
                for _ in range(MAX_TOOL_PAGES):
                    page = await client.list_tools(cursor=cursor)
                    listed.update((tool.name, tool) for tool in page.tools)
                    cursor = page.next_cursor
                    if cursor is None:
                        return listed
        except (TimeoutError, MCPError) as error:
            if _connection_closed(error):
                await self._broken(connection, error)
            raise MCPServerError(
                f"MCP server '{self.name}' did not list its tools: "
                f"{type(error).__name__}"
            ) from error
        except Exception as error:
            await self._broken(connection, error)
        raise MCPServerError(
            f"MCP server '{self.name}' lists more than {MAX_TOOL_PAGES} pages of tools."
        )

    async def _call(self, tool: str, arguments: dict[str, Any], run_id: str) -> str:
        """Call ``tool`` and return its result as text for the model.

        Only for a run's tool bindings (``agentframe.mcp.tools``), which check
        permissions and the execution policy before calling it.

        Args:
            tool: The tool's name on the server.
            arguments: The model's arguments, a JSON object.
            run_id: The calling run's invocation ID, for the logs.

        Returns:
            The tool's structured result as JSON, or its text content. A
            tool error, an MCP protocol error, a timeout or a result over
            ``max_result_bytes`` is returned as a JSON ``{"error": ...}``
            object for the model, and logged. Either is untrusted content.

        Raises:
            MCPServerError: The server could not be reached, or the
                connection closed (a stdio server exited, for example),
                broke or returned something invalid. The next use connects
                again.
        """
        from mcp import MCPError

        connection, client = await self._connected()
        try:
            async with asyncio.timeout(self._call_timeout):
                result = await client.call_tool(tool, arguments)
        except (TimeoutError, MCPError) as error:
            if _connection_closed(error):
                await self._broken(connection, error)
            self._log_failed_call(tool, run_id, type(error).__name__)
            return error_result(f"The tool call failed: {type(error).__name__}")
        except Exception as error:
            await self._broken(connection, error)
        text = _result_text(result)
        if len(text.encode()) > self._max_result_bytes:
            self._log_failed_call(tool, run_id, "result too large")
            return error_result(
                f"The tool result is larger than {self._max_result_bytes} bytes."
            )
        if result.is_error:
            self._log_failed_call(tool, run_id, "the tool reported an error")
        return text

    def _log_failed_call(self, tool: str, run_id: str, reason: str) -> None:
        logger.warning(
            "MCP tool call '%s.%s' failed: %s (invocation_id=%s)",
            self.name,
            tool,
            reason,
            run_id,
        )

    async def _broken(self, connection: _Connection, error: Exception) -> NoReturn:
        """Drop ``connection`` after an unexpected failure, then fail the run.

        Only the connection the failing call used is stopped, never a newer
        one another run has opened since.
        """
        await self._stop(connection)
        raise MCPServerError(
            f"MCP server '{self.name}' connection failed: {type(error).__name__}"
        ) from error

    async def _connected(self) -> "tuple[_Connection, Client]":
        """Return the open connection and its SDK client, connecting if needed."""
        connection = self._connection
        if connection is None or not _usable(connection):
            # No await between the check and the assignment, so concurrent
            # runs on one event loop share a single connection attempt.
            connection = self._connection = self._start()
        try:
            async with asyncio.timeout(self._connect_timeout):
                await connection.ready.wait()
        except TimeoutError:
            await self._stop(connection)
            raise MCPServerError(
                f"MCP server '{self.name}' did not connect within "
                f"{self._connect_timeout:g} seconds."
            ) from None
        if connection.client is None:
            raise MCPServerError(f"MCP server '{self.name}' could not be connected.")
        return connection, connection.client

    def _start(self) -> _Connection:
        """Start a connection attempt in its own task."""
        connection = _Connection()
        connection.task = asyncio.create_task(
            self._hold(connection), name=f"mcp-{self.name}"
        )
        connection.task.add_done_callback(lambda _: connection.ready.set())
        return connection

    async def _hold(self, connection: _Connection) -> None:
        """Open the connection, publish it, and keep it open until stopped.

        Runs as the connection's own task, so the SDK's transport is entered
        and left by the same task.
        """
        try:
            async with self._open() as client:
                connection.client = client
                connection.ready.set()
                await connection.stop.wait()
        except Exception as error:
            # A run waiting to connect, or the next call, reports the failure.
            logger.warning(
                "MCP server '%s' connection ended: %s", self.name, type(error).__name__
            )
        finally:
            connection.client = None
            if self._connection is connection:
                self._connection = None

    @asynccontextmanager
    async def _open(self) -> "AsyncIterator[Client]":
        """Connect to the server with the SDK, without its optional callbacks."""
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        if not isinstance(self._server, str):
            async with Client(
                self._server, read_timeout_seconds=self._call_timeout
            ) as client:
                yield client
            return
        credential = self._credential

        async def add_credential(request: httpx2.Request) -> None:
            if credential is not None:
                request.headers.update(await credential.headers())

        timeout = httpx2.Timeout(_HTTP_TIMEOUT_SECONDS, read=_HTTP_READ_TIMEOUT_SECONDS)
        async with (
            httpx2.AsyncClient(
                timeout=timeout, event_hooks={"request": [add_credential]}
            ) as http,
            Client(
                streamable_http_client(self._server, http_client=http),
                read_timeout_seconds=self._call_timeout,
            ) as client,
        ):
            yield client

    async def _stop(self, connection: _Connection) -> None:
        """Close ``connection`` and wait for its task to end."""
        if self._connection is connection:
            self._connection = None
        if connection.task.get_loop() is not asyncio.get_running_loop():
            return  # Its event loop has ended, and the connection with it.
        if connection.client is not None:
            connection.stop.set()
        else:
            connection.task.cancel()
        # wait() never raises the task's own error or cancellation.
        await asyncio.wait([connection.task])


def _connection_closed(error: Exception) -> bool:
    """Return whether ``error`` is the SDK's report that the connection ended.

    A stdio server that exited, for example, fails every later request this
    way, so the connection must be replaced rather than reported per call.
    """
    from mcp import MCPError
    from mcp.types import CONNECTION_CLOSED

    return isinstance(error, MCPError) and error.code == CONNECTION_CLOSED


def _usable(connection: _Connection) -> bool:
    """Return whether ``connection`` is open or opening on this event loop."""
    return (
        not connection.task.done()
        and connection.task.get_loop() is asyncio.get_running_loop()
    )


def _result_text(result: "CallToolResult") -> str:
    """Return a tool result as the model sees it.

    A tool error becomes ``{"error": <its text>}``. Otherwise structured
    content is returned as JSON, or else the text content, one block per
    line, with other content named but left out.
    """
    if result.is_error:
        return error_result(_content_text(result.content) or "The tool failed.")
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return _content_text(result.content)


def _content_text(content: "Sequence[ContentBlock]") -> str:
    from mcp.types import TextContent

    return "\n".join(
        block.text
        if isinstance(block, TextContent)
        else f"[{block.type} content omitted]"
        for block in content
    )


def _require_sdk() -> None:
    try:
        import mcp  # noqa: F401
    except ImportError as error:
        raise DefinitionError(
            "MCPClient needs the MCP SDK: install agentframe-py[mcp]."
        ) from error


def _checked_name(name: str) -> str:
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise DefinitionError(
            f"MCPClient name {name!r} must be letters, digits, '_' or '-'."
        )
    return name


def _checked_server(
    server: "str | StdioServerParameters | MCPServer", allow_http_hosts: Collection[str]
) -> "str | StdioServerParameters | MCPServer":
    from mcp import StdioServerParameters
    from mcp.server.mcpserver import MCPServer

    if isinstance(server, StdioServerParameters | MCPServer):
        return server
    if not isinstance(server, str) or not is_plain_http_url(server):
        raise DefinitionError(
            "MCPClient server must be StdioServerParameters or an http(s) URL "
            "without credentials, query or fragment."
        )
    if isinstance(allow_http_hosts, str):
        raise DefinitionError(
            "MCPClient allow_http_hosts must be a list of host names, "
            "not a single string."
        )
    parts = urlsplit(server)
    host = parts.hostname or ""
    allowed = {h.lower() for h in allow_http_hosts}
    if parts.scheme == "http" and not (is_loopback_host(host) or host in allowed):
        raise DefinitionError(
            f"MCPClient server URL must use https: '{host}' is not a loopback "
            "host or in allow_http_hosts."
        )
    return server


def _checked_permissions(
    name: str, permissions: Mapping[str, Collection[str]] | None
) -> dict[str, frozenset[str]]:
    checked: dict[str, frozenset[str]] = {}
    for tool, required in (permissions or {}).items():
        # A bare string would otherwise be read as one permission per character.
        if isinstance(required, str):
            raise DefinitionError(
                f"MCPClient '{name}' permissions for '{tool}' must be a list of "
                "permissions, not a single string."
            )
        checked[tool] = frozenset(required)
    return checked


def _positive(
    name: str, field_name: str, value: float, *, whole: bool = False
) -> float:
    valid_type = int if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, valid_type) or not value > 0:
        kind = "a whole number" if whole else "a number"
        raise DefinitionError(
            f"MCPClient '{name}' {field_name} must be {kind} greater than 0."
        )
    if not math.isfinite(value):
        raise DefinitionError(f"MCPClient '{name}' {field_name} must be finite.")
    return value


def _check_names(client: str, names: Sequence[str]) -> None:
    if not names:
        raise DefinitionError(f"MCPClient '{client}' tools() needs at least one name.")
    repeated = [n for n in dict.fromkeys(names) if names.count(n) > 1]
    if repeated:
        raise DefinitionError(
            f"MCP tool '{client}.{repeated[0]}' is selected more than once."
        )
