# MCP client: server tools for `ctx.generate`

An agent's model may call the tools of an [MCP](https://modelcontextprotocol.io)
server the application configured ([#253](https://github.com/malcmiller/agentframe/issues/253)).
The application creates one [`MCPClient`](#mcpclient) per server and registers
it as a service; a skill selects tools by name with
[`MCPClient.tools`](#mcpclienttools) and passes the returned
[`MCPTool`](#mcptool) entries to
[`ctx.generate(tools=[...])`](agents.md#runcontextgenerate), beside `@tool`
functions and [remote skills](orchestration.md#remote-skills-as-model-tools).
Nothing is offered to a model until a skill selects it.

Install the optional extra, which uses the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.x:

```bash
pip install "agentframe-py[mcp]"
```

Publishing an agent's own skills as an MCP server ([`MCPConfig`](hosting.md#mcpconfig),
#254) is the other direction, documented with hosting; it uses the same extra. The rationale is in the
[MCP design](../design/mcp-skills.md#mcp-client-server-tools-for-ctxgenerate).

## `MCPClient`

**Purpose / signature:** one application-configured MCP server, reached over
stdio (a subprocess the client starts) or streamable HTTP, whose tools skills
may offer a model. Implemented (#253).

`MCPClient(server, *, name, credential=None, permissions=None, connect_timeout_seconds=30.0, call_timeout_seconds=60.0, max_result_bytes=1_048_576, allow_http_hosts=())`

| Parameter | Type | Required/default | Meaning and constraints |
| --- | --- | --- | --- |
| `server` | `StdioServerParameters \| str \| MCPServer` | Required | SDK `StdioServerParameters` for the application's command, started as a subprocess speaking MCP on standard input and output; or a streamable HTTP endpoint URL: https, or http only to a loopback host or one in `allow_http_hosts`, without user info, query or fragment. An in-process SDK `MCPServer` is accepted for tests. |
| `name` | `str` | Required | Names the server's tools: the model sees `<name>_<tool>`, execution policy tool checks see `<name>.<tool>`. Letters, digits, `_` and `-` only. |
| `credential` | [`OutboundCredential`](discovery.md#outboundcredential) `\| None` | `None` | Headers sent with every HTTP request, for example `APIKeyCredential(key)`. Only for a URL. |
| `permissions` | `Mapping[str, Collection[str]] \| None` | `None` | Tool names on the server mapped to permissions a caller must all hold to use the tool. Unlisted tools need none. Every key must be one of the server's tools, checked by `tools()`. A bare string is refused. |
| `connect_timeout_seconds` | `float` | `30.0` | How long starting or connecting to the server may take. |
| `call_timeout_seconds` | `float` | `60.0` | How long one tool call, or listing the tools, may take. |
| `max_result_bytes` | `int` | `1_048_576` (1 MiB) | The largest tool result, in UTF-8 bytes, a model is given. A larger result is never cut short: the model gets a tool error. |
| `allow_http_hosts` | `Collection[str]` | `()` | Non-loopback hosts that may be reached over plain http, for development only. A bare string is refused. |

```python
from mcp import StdioServerParameters

from agentframe import MCPClient, Services

files = MCPClient(
    StdioServerParameters(command="python", args=["-m", "files_server"]),
    name="files",
    permissions={"write_file": ["files.write"]},
)
services = Services()
services.add(MCPClient, files)
```

**Behavior / failures:** construction does no I/O and raises
`DefinitionError` when the `mcp` extra is missing or an argument is invalid
(as listed above; bounds must be finite and greater than 0, and
`max_result_bytes` a whole number). The connection opens on first use and is
shared by every run; it lives in a background task the client owns, because
the SDK's transports must be entered and left by one task. A connection that
fails raises [`MCPServerError`](#mcpservererror); the next use connects again.
The client offers servers no sampling, elicitation or roots.

The application owns the client: call [`aclose`](#mcpclientaclose) at
shutdown (or use `async with`). For several servers, register one subclass
per server, for example `class FilesMCP(MCPClient): ...`, and inject each by
its class.

**Related:** [`Inject[T]` / `Services`](agents.md#injectt),
[MCP design](../design/mcp-skills.md#mcp-client-server-tools-for-ctxgenerate).

## `MCPClient.tools`

**Purpose / signature:** `async tools(*names: str) -> tuple[MCPTool, ...]`.
Selects server tools by name, one [`MCPTool`](#mcptool) per name, in order.
Implemented (#253).

```python
from agentframe import Agent, Inject, MCPClient, RunContext, agent, skill


@agent(name="notes", description="Tidies notes.", version="0.1.0")
class Notes(Agent):
    @skill(description="Tidy the notes.", tags=["notes"])
    async def tidy(self, text: str, files: Inject[MCPClient], ctx: RunContext) -> str:
        tools = await files.tools("read_file", "write_file")
        return await ctx.generate(text, tools=[*tools])
```

**Behavior / failures:** the server's tool list is read on every call, at
most ten pages, within `call_timeout_seconds` (the SDK may serve it from its
response cache). Raises `DefinitionError` when no name is given, a name is
repeated or is not one of the server's tools, a `permissions=` key is not
one of the server's tools, a tool's model name would be longer than 64
characters or its argument schema larger than 8 KiB. Raises
[`MCPServerError`](#mcpservererror) when the server cannot be reached or does
not list its tools in time.

## `MCPClient.connect`

**Purpose / signature:** `async connect() -> None`. Opens the connection now,
for example at application startup, so a misconfigured server fails early.
Implemented (#253). Raises [`MCPServerError`](#mcpservererror) when the server
does not start or connect within `connect_timeout_seconds`.

## `MCPClient.aclose`

**Purpose / signature:** `async aclose() -> None`. Closes the connection and
waits for it to end, stopping a stdio server's process. Safe to call more than
once; a later use connects again. `async with MCPClient(...) as client:`
connects on entry and closes on exit. Implemented (#253).

## `MCPTool`

**Purpose / signature:** one server tool a skill selected, ready for
`ctx.generate(tools=[...])`. A frozen dataclass returned by
[`MCPClient.tools`](#mcpclienttools); not constructed by application code.
Implemented (#253).

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | `str` | `"<client>.<tool>"`, for example `"files.read_file"`: the name execution policy tool checks see. The model sees the tool `"files_read_file"`. |
| `tool` | `str` | The tool's name on the server. |
| `description` | `str` | The server's description (or title, or name), without control characters and at most 500 characters. |
| `input_schema` | `dict[str, Any]` | The tool's argument JSON Schema, text bounded and `examples` dropped, at most 8 KiB. Treat it as read-only. |
| `requires_permissions` | `frozenset[str]` | From the client's `permissions=`; empty by default. |
| `client` | `MCPClient` | The client the tool belongs to. |

Descriptions and schemas are the server's content: bounded but untrusted, so
they reach a model only as a tool definition, never as instructions.

### Calls from `ctx.generate`

| Rule | Behavior |
| --- | --- |
| Offering | Entries whose `requires_permissions` the caller lacks are not offered. An entry selected twice, or whose model name matches another selected tool or remote entry, raises `DefinitionError` before anything is sent. |
| Before a call | The permissions are checked again (a call to an unoffered entry raises `PermissionDeniedError`); arguments that are not a JSON object send nothing and the model is told; then every [execution policy tool check](authorization.md#executionpolicy) gets the qualified name and the arguments. Denial raises `PermissionDeniedError`, a failing check `PolicyUnavailableError`; nothing is sent and the model is never told. `ToolAuthorizationPolicy` covers `@tool` functions only, as for remote skills. |
| Limits | Each call counts toward `RunLimits.max_tool_calls`, runs one at a time within `call_timeout_seconds` and the run's deadline, and is never retried. |
| Result | Structured content as JSON, otherwise the text content blocks one per line (other content is named, not included). Untrusted tool output. |
| Tool errors | A server-reported error (`isError`) goes back as `{"error": <its text>}`; another MCP protocol error, a call timeout or a result over `max_result_bytes` goes back as a tool error naming only the failure. Each is logged with the run's invocation ID. |
| Broken connection | A closed connection (the server process exited) or any other failure (the transport broke, an invalid result) stops the connection and raises [`MCPServerError`](#mcpservererror), failing the run. |

## `MCPServerError`

**Purpose / signature:** an MCP server could not be reached, did not connect
or list its tools in time, or its connection broke during a call. A direct
`AgentFrameError` subclass: `MCPServerError(message: str, *, invocation_id:
str | None = None)`. Implemented (#253).

```python
from agentframe import MCPServerError


def failed_server(error: MCPServerError) -> str:
    return error.message
```

**Behavior / failures:** the message names the client and the failure's class,
never the server's output. A hosted run that raises it fails with a safe
message. A tool error the server reports is not an `MCPServerError`: it goes
back to the model.

## Limitations

The SDK reads a whole stdio line or HTTP response before AgentFrame sees it,
so `max_result_bytes` bounds what reaches the model, not what the SDK reads.
Connect only to servers the application trusts. Tool list change
notifications, resources, prompts, sampling, elicitation and OAuth flows are
not supported.
