"""``ctx.generate(tools=[*mcp_tools])`` offers MCP server tools to the model.

The servers here are in-process SDK ``MCPServer`` objects, so no process is
started; ``test_mcp_client.py`` covers a real stdio subprocess.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agentframe import (
    Agent,
    DefinitionError,
    ExecutionPolicy,
    Inject,
    LimitExceededError,
    MCPClient,
    ModelCapabilities,
    PermissionDeniedError,
    Principal,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    Services,
    ToolCall,
    agent,
    skill,
    tool,
)
from agentframe.testing import ScriptedModel

TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
WRITER = Principal(id="writer", permissions=frozenset({"files.write"}))
READER = Principal(id="reader", permissions=frozenset())


class Files:
    """An in-process MCP server with a few file tools; records what it wrote."""

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []
        self.server = MCPServer("files")
        self.server.tool(description="Read a file.", structured_output=False)(
            self.read_file
        )
        self.server.tool(description="Write a file.", structured_output=False)(
            self.write_file
        )
        self.server.tool(description="Count the files.")(self.stats)
        self.server.tool(description="Always fails.", structured_output=False)(
            self.broken
        )
        self.server.tool(description="Never answers.", structured_output=False)(
            self.slow
        )

    def read_file(self, path: str) -> str:
        return f"contents of {path}"

    def write_file(self, path: str, text: str) -> str:
        self.written.append((path, text))
        return "written"

    def stats(self) -> dict[str, int]:
        return {"files": 3}

    def broken(self) -> str:
        raise ToolError("disk on fire")

    async def slow(self) -> str:
        await asyncio.sleep(30)
        return "late"


@pytest.fixture
def files() -> Files:
    return Files()


@pytest.fixture
async def client(files: Files) -> AsyncIterator[MCPClient]:
    client = MCPClient(
        files.server,
        name="files",
        permissions={"write_file": ["files.write"]},
        call_timeout_seconds=1,
    )
    yield client
    await client.aclose()


@tool(description="Look something up.")
def lookup(text: str) -> str:
    return text


@tool(description="A local tool with an MCP tool's model name.")
def files_read_file(path: str) -> str:
    return path


@agent(name="Editor", description="Edits files.", version="0.1.0")
class Editor(Agent):
    """Offers the named MCP tools (plus ``extra``) to its model."""

    def __init__(self, model: ScriptedModel, *names: str, extra: Any = ()) -> None:
        super().__init__(model=model, tools=[lookup, files_read_file])
        self.names = names
        self.extra = extra

    @skill(id="edit", description="Edit files for a goal.", tags=["files"])
    async def edit(self, goal: str, files: Inject[MCPClient], ctx: RunContext) -> str:
        tools = await files.tools(*self.names)
        return await ctx.generate(goal, tools=[*self.extra, *tools])


def scripted(*replies: Any) -> ScriptedModel:
    return ScriptedModel(replies, capabilities=TOOL_MODEL)


async def edit(
    editor: Editor,
    client: MCPClient,
    *,
    principal: Principal | None = WRITER,
    policy: ExecutionPolicy | None = None,
    limits: RunLimits | None = None,
) -> str:
    services = Services()
    services.add(MCPClient, client)
    result: str = await Runtime(services=services, policy=policy).invoke(
        editor,
        skill="edit",
        arguments={"goal": "Tidy the notes"},
        principal=principal,
        options=RunOptions(limits=limits),
    )
    return result


def tool_results(model: ScriptedModel) -> list[str]:
    return [m.content for m in model.requests[-1].messages if m.role == "tool"]


async def test_selected_tools_are_offered_with_safe_names_and_server_schemas(
    client: MCPClient,
) -> None:
    model = scripted("done")

    await edit(Editor(model, "read_file", "write_file"), client)

    specs = {spec.name: spec for spec in model.requests[0].tools}
    assert list(specs) == ["files_read_file", "files_write_file"]
    assert specs["files_read_file"].description == "Read a file."
    assert specs["files_write_file"].parameters["required"] == ["path", "text"]


async def test_unselected_server_tools_are_never_offered(client: MCPClient) -> None:
    model = scripted("done")

    await edit(Editor(model, "read_file"), client)

    assert [spec.name for spec in model.requests[0].tools] == ["files_read_file"]


async def test_a_call_runs_on_the_server_and_its_text_goes_back(
    client: MCPClient, files: Files
) -> None:
    model = scripted(
        ToolCall("1", "files_write_file", '{"path": "a.md", "text": "hi"}'),
        "saved",
    )

    assert await edit(Editor(model, "write_file"), client) == "saved"

    assert files.written == [("a.md", "hi")]
    assert tool_results(model) == ["written"]


async def test_structured_content_goes_back_as_json(client: MCPClient) -> None:
    model = scripted(ToolCall("1", "files_stats", "{}"), "three")

    await edit(Editor(model, "stats"), client)

    assert tool_results(model) == ['{"files": 3}']


async def test_a_server_tool_error_goes_back_to_the_model(client: MCPClient) -> None:
    model = scripted(ToolCall("1", "files_broken", "{}"), "sorry")

    assert await edit(Editor(model, "broken"), client) == "sorry"

    [result] = tool_results(model)
    assert result.startswith('{"error": ') and "disk on fire" in result


async def test_arguments_that_are_not_an_object_send_nothing(
    client: MCPClient, files: Files
) -> None:
    model = scripted(ToolCall("1", "files_write_file", "[1]"), "ok")

    await edit(Editor(model, "write_file"), client)

    assert files.written == []
    assert tool_results(model) == [
        '{"error": "Invalid arguments: expected a JSON object."}'
    ]


async def test_a_call_over_the_call_timeout_becomes_a_tool_error(
    client: MCPClient,
) -> None:
    model = scripted(ToolCall("1", "files_slow", "{}"), "gave up")

    assert await edit(Editor(model, "slow"), client) == "gave up"

    [result] = tool_results(model)
    assert result.startswith('{"error": "The tool call failed')


async def test_a_result_over_max_result_bytes_is_refused_not_cut_short(
    files: Files,
) -> None:
    model = scripted(ToolCall("1", "files_read_file", '{"path": "big"}'), "ok")
    small = MCPClient(files.server, name="files", max_result_bytes=10)

    try:
        await edit(Editor(model, "read_file"), small)
    finally:
        await small.aclose()

    assert tool_results(model) == [
        '{"error": "The tool result is larger than 10 bytes."}'
    ]


async def test_a_tool_the_caller_lacks_permissions_for_is_not_offered(
    client: MCPClient,
) -> None:
    model = scripted("done")

    await edit(Editor(model, "read_file", "write_file"), client, principal=READER)

    assert [spec.name for spec in model.requests[0].tools] == ["files_read_file"]


async def test_a_call_to_a_tool_the_caller_lacks_permissions_for_fails_the_run(
    client: MCPClient, files: Files
) -> None:
    model = scripted(
        ToolCall("1", "files_write_file", '{"path": "a.md", "text": "hi"}'),
        "unsent",
    )

    with pytest.raises(PermissionDeniedError):
        await edit(Editor(model, "write_file"), client, principal=READER)

    assert files.written == []
    assert len(model.requests) == 1


async def test_policy_tool_checks_see_the_qualified_name_and_can_deny(
    client: MCPClient, files: Files
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def no_writes(
        skill: Any, principal: Any, name: str, arguments: Mapping[str, Any]
    ) -> bool:
        seen.append((name, dict(arguments)))
        return name != "files.write_file"

    model = scripted(
        ToolCall("1", "files_read_file", '{"path": "a.md"}'),
        ToolCall("2", "files_write_file", '{"path": "a.md", "text": "x"}'),
        "unsent",
    )

    with pytest.raises(PermissionDeniedError):
        await edit(
            Editor(model, "read_file", "write_file"),
            client,
            policy=ExecutionPolicy(tool_checks=[no_writes]),
        )

    assert seen == [
        ("files.read_file", {"path": "a.md"}),
        ("files.write_file", {"path": "a.md", "text": "x"}),
    ]
    assert files.written == []


async def test_calls_count_toward_max_tool_calls(client: MCPClient) -> None:
    model = scripted(
        ToolCall("1", "files_read_file", '{"path": "a"}'),
        ToolCall("2", "files_read_file", '{"path": "b"}'),
        "unsent",
    )

    with pytest.raises(LimitExceededError):
        await edit(
            Editor(model, "read_file"), client, limits=RunLimits(max_tool_calls=1)
        )


async def test_an_mcp_tool_named_like_a_tool_is_refused_before_sending(
    client: MCPClient,
) -> None:
    model = scripted("unsent")
    editor = Editor(model, "read_file", extra=[files_read_file])

    with pytest.raises(DefinitionError, match="both become the model tool"):
        await edit(editor, client)

    assert model.requests == []


async def test_a_tool_selected_twice_is_refused_before_sending(
    client: MCPClient,
) -> None:
    model = scripted("unsent")
    twice = await client.tools("read_file")

    with pytest.raises(DefinitionError, match="more than once"):
        await edit(Editor(model, "read_file", extra=twice), client)

    assert model.requests == []
