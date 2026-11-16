"""``AgentApp(mcp=MCPConfig(...))``: allowlisted skills served as MCP tools.

Each test serves an app over real localhost HTTP and talks to ``/mcp`` with
the official MCP SDK client, as an LLM app would.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    DefinitionError,
    HostConfig,
    Inject,
    MCPConfig,
    RateLimit,
    RunContext,
    Services,
    TracingConfig,
    agent,
    skill,
)
from local_server import serve_locally

READER_KEY = "reader-key"
ADMIN_KEY = "admin-key"


class Clock:
    """An injected service; it must never appear in a tool's schema."""

    def now(self) -> str:
        return "12:00"


class Forecast(BaseModel):
    city: str
    celsius: int


@agent(name="weather", description="Reports the weather.", version="1.0.0")
class Weather(Agent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @skill(description="Say hello.", tags=["greeting"])
    async def greet(self, name: str, clock: Inject[Clock], ctx: RunContext) -> str:
        return f"Hello, {name}! It is {clock.now()}."

    @skill(description="Forecast a city's weather.", tags=["weather"])
    async def forecast(self, city: str, ctx: RunContext) -> Forecast:
        return Forecast(city=city, celsius=21)

    @skill(
        description="Reset the station.",
        tags=["admin"],
        requires_permissions=["station:reset"],
    )
    async def reset(self, ctx: RunContext) -> str:
        return "reset"

    @skill(description="Not published over MCP.", tags=["internal"])
    async def secret(self, ctx: RunContext) -> str:
        return "secret"

    @skill(description="Always fails.", tags=["broken"])
    async def broken(self, ctx: RunContext) -> str:
        raise RuntimeError("password=hunter2")

    @skill(description="Waits until released.", tags=["slow"])
    async def wait(self, ctx: RunContext) -> str:
        self.started.set()
        await self.release.wait()
        return "released"


EXPOSED = ["greet", "forecast", "reset", "broken", "wait"]


def services() -> Services:
    registered = Services()
    registered.add(Clock, Clock())
    return registered


def make_app(
    base_url: str,
    *,
    max_concurrent_runs: int = 32,
    agent: Weather | None = None,
    rate_limit: RateLimit | None = None,
    tracing: TracingConfig | None = None,
) -> AgentApp:
    return AgentApp(
        agent=agent or Weather(),
        host=HostConfig(
            public_url=base_url,
            auth=APIKeyAuth(
                keys={
                    READER_KEY: APIKey("reader"),
                    ADMIN_KEY: APIKey("admin", roles=["Admin"]),
                }
            ),
            role_permissions={"Admin": ["station:reset"]},
            max_concurrent_runs=max_concurrent_runs,
            rate_limit=rate_limit,
        ),
        services=services(),
        mcp=MCPConfig(exposed_skills=EXPOSED),
        tracing=tracing,
    )


@asynccontextmanager
async def mcp_client(base_url: str, key: str = READER_KEY) -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(headers={"X-API-Key": key}) as http,
        Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client,
    ):
        yield client


async def test_lists_only_published_skills_with_business_argument_schemas() -> None:
    async with serve_locally(make_app) as base_url, mcp_client(base_url) as client:
        listed = await client.list_tools()

    tools = {tool.name: tool for tool in listed.tools}
    assert sorted(tools) == ["broken", "forecast", "greet", "wait"]
    greet = tools["greet"]
    assert greet.description == "Say hello."
    assert greet.input_schema["properties"].keys() == {"name"}
    assert greet.output_schema is None
    assert tools["forecast"].output_schema is not None


async def test_calls_a_text_skill_and_reports_the_invocation() -> None:
    async with serve_locally(make_app) as base_url, mcp_client(base_url) as client:
        result = await client.call_tool("greet", {"name": "Ada"})

    assert not result.is_error
    assert result.content[0].text == "Hello, Ada! It is 12:00."  # type: ignore[union-attr]
    assert result.structured_content is None
    assert result.meta is not None
    assert isinstance(result.meta["agentframe/invocation_id"], str)
    assert result.meta["agentframe/usage"]["model_calls"] == 0


async def test_structured_results_match_the_output_schema() -> None:
    async with serve_locally(make_app) as base_url, mcp_client(base_url) as client:
        result = await client.call_tool("forecast", {"city": "Oslo"})

    assert result.structured_content == {"value": {"city": "Oslo", "celsius": 21}}
    assert '"celsius":21' in result.content[0].text  # type: ignore[union-attr]


async def test_requests_without_valid_credentials_are_refused() -> None:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
    }
    async with serve_locally(make_app) as base_url, httpx2.AsyncClient() as http:
        missing = await http.post(f"{base_url}/mcp", json=initialize)
        wrong = await http.post(
            f"{base_url}/mcp", json=initialize, headers={"X-API-Key": "wrong"}
        )

    for response in (missing, wrong):
        assert response.status_code == 401
        assert response.json() == {"error": "Authentication required."}
        assert "WWW-Authenticate" in response.headers


async def test_an_unpublished_skill_is_not_callable() -> None:
    async with serve_locally(make_app) as base_url, mcp_client(base_url) as client:
        with pytest.raises(MCPError, match="Unknown tool"):
            await client.call_tool("secret", {})


async def test_permissions_hide_and_deny_a_tool() -> None:
    async with serve_locally(make_app) as base_url:
        async with mcp_client(base_url) as reader:
            listed = await reader.list_tools()
            denied = await reader.call_tool("reset", {})
        async with mcp_client(base_url, ADMIN_KEY) as admin:
            admin_tools = {tool.name for tool in (await admin.list_tools()).tools}
            allowed = await admin.call_tool("reset", {})

    assert "reset" not in {tool.name for tool in listed.tools}
    assert denied.is_error
    assert "reset" in admin_tools
    assert not allowed.is_error


async def test_failures_are_tool_errors_with_safe_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with serve_locally(make_app) as base_url, mcp_client(base_url) as client:
        invalid = await client.call_tool("greet", {"name": 3, "extra": 1})
        broken = await client.call_tool("broken", {})

    assert invalid.is_error
    assert broken.is_error
    assert broken.content[0].text == "The skill failed."  # type: ignore[union-attr]
    assert "hunter2" in caplog.text  # Only the operator log has the details.
    assert broken.meta is not None
    assert broken.meta["agentframe/invocation_id"] in caplog.text


async def test_a_busy_host_refuses_tool_calls_with_503() -> None:
    weather = Weather()

    def app(base_url: str) -> AgentApp:
        return make_app(base_url, max_concurrent_runs=1, agent=weather)

    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "greet", "arguments": {"name": "Ada"}},
    }
    async with serve_locally(app) as base_url, mcp_client(base_url) as client:
        waiting = asyncio.create_task(client.call_tool("wait", {}))
        async with asyncio.timeout(5):
            await weather.started.wait()
        async with httpx2.AsyncClient(headers={"X-API-Key": READER_KEY}) as http:
            busy = await http.post(
                f"{base_url}/mcp",
                json=call,
                headers={"Accept": "application/json, text/event-stream"},
            )
        weather.release.set()
        released = await waiting

    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "1"
    assert not released.is_error


async def test_tool_calls_count_against_the_callers_rate_limit() -> None:
    def app(base_url: str) -> AgentApp:
        return make_app(base_url, rate_limit=RateLimit(requests=1, per_seconds=60))

    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "greet", "arguments": {"name": "Ada"}},
    }
    async with serve_locally(app) as base_url, mcp_client(base_url) as client:
        listed = await client.list_tools()  # Takes no allowance.
        first = await client.call_tool("greet", {"name": "Ada"})
        async with httpx2.AsyncClient(headers={"X-API-Key": READER_KEY}) as http:
            limited = await http.post(
                f"{base_url}/mcp",
                json=call,
                headers={"Accept": "application/json, text/event-stream"},
            )

    assert listed.tools
    assert not first.is_error
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers


async def test_tool_calls_are_traced_like_a2a_runs() -> None:
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))

    def app(base_url: str) -> AgentApp:
        return make_app(base_url, tracing=TracingConfig(tracer_provider=provider))

    trace_id = "0af7651916cd43dd8448eb211c80319c"
    async with (
        serve_locally(app) as base_url,
        httpx2.AsyncClient(
            headers={
                "X-API-Key": READER_KEY,
                "traceparent": f"00-{trace_id}-b7ad6b7169203331-01",
            }
        ) as http,
        Client(streamable_http_client(f"{base_url}/mcp", http_client=http)) as client,
    ):
        await client.call_tool("greet", {"name": "Ada"})

    [run] = [s for s in spans.get_finished_spans() if s.name == "invoke_agent weather"]
    assert run.kind == SpanKind.SERVER
    assert run.context is not None
    assert f"{run.context.trace_id:032x}" == trace_id


async def test_batches_are_refused() -> None:
    batch: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    ]
    async with (
        serve_locally(make_app) as base_url,
        httpx2.AsyncClient(headers={"X-API-Key": READER_KEY}) as http,
    ):
        response = await http.post(f"{base_url}/mcp", json=batch)

    assert response.status_code == 400


async def test_deeply_nested_bodies_are_left_for_the_sdk_to_refuse() -> None:
    body = '{"jsonrpc": "2.0", "id": 1, "method": "x", "params": ' + "[" * 5000
    body += "]" * 5000 + "}"
    async with (
        serve_locally(make_app) as base_url,
        httpx2.AsyncClient(headers={"X-API-Key": READER_KEY}) as http,
    ):
        response = await http.post(
            f"{base_url}/mcp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code < 500


async def test_without_mcp_config_there_is_no_mcp_endpoint() -> None:
    def app(base_url: str) -> AgentApp:
        return AgentApp(
            agent=Weather(),
            host=HostConfig(public_url=base_url, auth=AllowAnonymous()),
            services=services(),
        )

    async with serve_locally(app) as base_url, httpx2.AsyncClient() as http:
        response = await http.post(f"{base_url}/mcp", json={})

    assert response.status_code == 404


def test_an_empty_allowlist_publishes_nothing_by_default() -> None:
    assert MCPConfig().exposed_skills == ()


@pytest.mark.parametrize(
    ("exposed", "error"),
    [
        ("greet", "not a single string"),
        (["greet", "greet"], "twice"),
        ([3], "skill ID strings"),
    ],
)
def test_invalid_allowlists_are_refused(exposed: Any, error: str) -> None:
    with pytest.raises(DefinitionError, match=error):
        MCPConfig(exposed_skills=exposed)


def test_unknown_skills_fail_app_construction() -> None:
    with pytest.raises(DefinitionError, match="'missing', which is not a skill"):
        AgentApp(
            agent=Weather(),
            host=HostConfig(public_url="http://127.0.0.1", auth=AllowAnonymous()),
            services=services(),
            mcp=MCPConfig(exposed_skills=["missing"]),
        )


def test_skill_ids_that_are_not_tool_names_fail_app_construction() -> None:
    @agent(name="spaced", description="Has a spaced skill ID.", version="1.0.0")
    class Spaced(Agent):
        @skill(id="say hi", description="Say hi.", tags=["greeting"])
        async def say_hi(self, ctx: RunContext) -> str:
            return "hi"

    with pytest.raises(DefinitionError, match="cannot be an MCP tool"):
        AgentApp(
            agent=Spaced(),
            host=HostConfig(public_url="http://127.0.0.1", auth=AllowAnonymous()),
            mcp=MCPConfig(exposed_skills=["say hi"]),
        )
