import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    DefinitionError,
    EnvironmentAgentRegistry,
    HostConfig,
    Inject,
    Principal,
    RemoteAgentError,
    RunContext,
    Runtime,
    Services,
    agent,
    skill,
)
from agentframe.discovery import cards, directory

ORIGIN = "https://research.example.com"
CARD_URL = f"{ORIGIN}/.well-known/agent-card.json"
RESEARCH = AgentReference("research-agent", CARD_URL)

type Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def card_json(
    interface_url: str = f"{ORIGIN}/a2a",
    *,
    binding: str = "JSONRPC",
    version: str = "1.0",
    name: str = "Research",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": "Finds sources.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": interface_url,
                "protocolBinding": binding,
                "protocolVersion": version,
            }
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


class StaticRegistry(AgentRegistry):
    """An in-memory source that can be switched to failing."""

    def __init__(self, *references: AgentReference) -> None:
        self.references = list(references)
        self.failing = False
        self.reads = 0

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        self.reads += 1
        if self.failing:
            raise DefinitionError("Static registry is unavailable.")
        names = query.names or set()
        return [ref for ref in self.references if ref.name in names]

    def __repr__(self) -> str:
        return "StaticRegistry()"


class CardServer:
    """A fake remote agent that records every request it answers."""

    def __init__(self, body: object = None) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Handler = self.json_reply(card_json() if body is None else body)

    @staticmethod
    def json_reply(body: object) -> Handler:
        async def reply(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        return reply

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.respond(request)

    def client(self, **options: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **options)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(directory, "monotonic", fake)
    return fake


def make_directory(
    server: CardServer, *references: AgentReference, **options: Any
) -> AgentDirectory:
    return AgentDirectory(
        sources=[StaticRegistry(*(references or (RESEARCH,)))],
        http_client=server.client(),
        **options,
    )


# Fetching and caching


async def test_get_returns_the_validated_card_and_interface_url() -> None:
    server = CardServer()

    found = await make_directory(server).get("research-agent")

    assert found.reference == RESEARCH
    assert found.card.name == "Research"
    assert found.interface_url == f"{ORIGIN}/a2a"
    assert found.stale is False
    assert [str(request.url) for request in server.requests] == [CARD_URL]


async def test_fresh_cards_come_from_the_cache_until_the_ttl_passes(
    clock: FakeClock,
) -> None:
    server = CardServer()
    agents = make_directory(server)

    await agents.get("research-agent")
    clock.now += 299
    await agents.get("research-agent")
    assert len(server.requests) == 1

    clock.now += 1
    await agents.get("research-agent")
    assert len(server.requests) == 2


async def test_refresh_refetches_a_fresh_card(clock: FakeClock) -> None:
    server = CardServer()
    agents = make_directory(server)
    await agents.get("research-agent")

    server.respond = CardServer.json_reply(card_json(name="Research v2"))
    refreshed = await agents.refresh("research-agent")

    assert refreshed.card.name == "Research v2"
    assert (await agents.get("research-agent")).card.name == "Research v2"
    assert len(server.requests) == 2


async def test_a_failed_refresh_raises_and_keeps_the_cached_card(
    clock: FakeClock,
) -> None:
    server = CardServer()
    agents = make_directory(server, allow_stale=True)
    await agents.get("research-agent")

    server.respond = CardServer.json_reply([])
    with pytest.raises(RemoteAgentError):
        await agents.refresh("research-agent")

    assert (await agents.get("research-agent")).stale is False


async def test_a_failed_stale_refresh_raises_by_default(clock: FakeClock) -> None:
    server = CardServer()
    agents = make_directory(server)
    await agents.get("research-agent")

    clock.now += 300
    server.respond = CardServer.json_reply([])
    with pytest.raises(RemoteAgentError, match="not a valid Agent Card"):
        await agents.get("research-agent")


async def test_allow_stale_serves_the_old_card_marked_stale_and_warns(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    server = CardServer()
    agents = make_directory(server, allow_stale=True)
    first = await agents.get("research-agent")

    clock.now += 300
    server.respond = CardServer.json_reply([])
    with caplog.at_level(logging.WARNING, logger="agentframe.discovery.directory"):
        stale = await agents.get("research-agent")

    assert stale.stale is True
    assert stale.card == first.card
    assert "stale card for agent 'research-agent'" in caplog.text
    # The failed refresh did not reset freshness, so the next get tries again.
    await agents.get("research-agent")
    assert len(server.requests) == 3


async def test_concurrent_gets_of_a_stale_card_make_one_request(
    clock: FakeClock,
) -> None:
    server = CardServer()
    agents = make_directory(server)
    await agents.get("research-agent")
    clock.now += 300

    async def slow_reply(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=card_json())

    server.respond = slow_reply
    found = await asyncio.gather(*(agents.get("research-agent") for _ in range(5)))

    assert len(server.requests) == 2
    assert all(item.card == found[0].card for item in found)


# Failures caused by the remote agent


def reply_with(response: httpx.Response) -> Handler:
    async def reply(request: httpx.Request) -> httpx.Response:
        return response

    return reply


async def slow_reply(request: httpx.Request) -> httpx.Response:
    await asyncio.sleep(1)
    return httpx.Response(200, json=card_json())


async def unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused to secret-host")


oversized = {**card_json(), "description": "x" * cards.MAX_CARD_BYTES}


@pytest.mark.parametrize(
    ("respond", "message"),
    [
        (reply_with(httpx.Response(200, text="{secret")), "not valid JSON"),
        (CardServer.json_reply(["secret"]), "not a valid Agent Card"),
        (CardServer.json_reply({"secret": "value"}), "not a valid Agent Card"),
        (CardServer.json_reply({"name": 5}), "not a valid Agent Card"),
        (CardServer.json_reply(oversized), "larger than 1048576 bytes"),
        (
            reply_with(httpx.Response(302, headers={"Location": "https://x.test/"})),
            "redirects are not followed",
        ),
        (reply_with(httpx.Response(404, text="secret")), "HTTP 404"),
        (unreachable, "could not be fetched: ConnectError"),
        (slow_reply, "not received within"),
        (
            CardServer.json_reply(card_json(binding="GRPC")),
            "no JSON-RPC A2A 1.0 interface",
        ),
        (
            CardServer.json_reply(card_json(version="0.3")),
            "no JSON-RPC A2A 1.0 interface",
        ),
    ],
)
async def test_invalid_or_unreachable_cards_fail_with_a_safe_message(
    monkeypatch: pytest.MonkeyPatch, respond: Handler, message: str
) -> None:
    monkeypatch.setattr(cards, "FETCH_TIMEOUT_SECONDS", 0.05)
    server = CardServer()
    server.respond = respond

    with pytest.raises(RemoteAgentError, match=message) as raised:
        await make_directory(server).get("research-agent")

    assert "research-agent" in raised.value.message
    assert "secret" not in raised.value.message
    assert raised.value.__cause__ is None


async def test_an_oversized_streamed_card_is_refused_without_a_length_header() -> None:
    async def streamed(request: httpx.Request) -> httpx.Response:
        chunks = [b" " * 65536] * 17
        return httpx.Response(200, content=iter_bytes(chunks))

    server = CardServer()
    server.respond = streamed

    with pytest.raises(RemoteAgentError, match="larger than"):
        await make_directory(server).get("research-agent")


async def iter_bytes(chunks: list[bytes]) -> Any:
    for chunk in chunks:
        yield chunk


# Destination safety


@pytest.mark.parametrize(
    "interface_url",
    [
        "https://elsewhere.example.com/a2a",
        "https://research.example.com:8443/a2a",
        "http://research.example.com/a2a",
        "https://user:secret@research.example.com/a2a",
    ],
)
async def test_a_card_whose_interface_is_on_another_origin_is_refused(
    interface_url: str,
) -> None:
    server = CardServer(card_json(interface_url))

    with pytest.raises(RemoteAgentError, match="not on the card URL's origin"):
        await make_directory(server).get("research-agent")


async def test_the_default_https_port_counts_as_the_same_origin() -> None:
    server = CardServer(card_json("https://RESEARCH.example.com:443/a2a"))

    found = await make_directory(server).get("research-agent")

    assert found.interface_url == "https://RESEARCH.example.com:443/a2a"


async def test_plain_http_to_a_non_loopback_host_is_refused_before_sending() -> None:
    reference = AgentReference("dev-agent", "http://dev.internal:8000/card.json")
    server = CardServer(card_json("http://dev.internal:8000/a2a"))

    with pytest.raises(RemoteAgentError, match="allow_http_hosts"):
        await make_directory(server, reference).get("dev-agent")
    assert server.requests == []

    allowed = make_directory(server, reference, allow_http_hosts=["DEV.internal"])
    assert (await allowed.get("dev-agent")).card.name == "Research"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
async def test_plain_http_to_loopback_is_allowed(host: str) -> None:
    reference = AgentReference("local-agent", f"http://{host}:8001/card.json")
    server = CardServer(card_json(f"http://{host}:8001/a2a"))

    found = await make_directory(server, reference).get("local-agent")

    assert found.interface_url == f"http://{host}:8001/a2a"


# Credentials and callers


@agent(name="Planner", description="Looks up a research agent.", version="0.1.0")
class Planner(Agent):
    @skill(
        id="research_card",
        name="Research card",
        description="Card name.",
        tags=["lookup"],
    )
    async def research_card(
        self, agents: Inject[AgentDirectory], ctx: RunContext
    ) -> str:
        return (await agents.get("research-agent")).card.name


async def test_two_callers_share_one_card_fetched_without_credentials() -> None:
    server = CardServer()
    agents = AgentDirectory(
        sources=[StaticRegistry(RESEARCH)],
        http_client=server.client(auth=httpx.BasicAuth("app", "secret")),
    )
    services = Services()
    services.add(AgentDirectory, agents)
    runtime = Runtime(services=services)

    for caller in ("alice", "bob"):
        name = await runtime.invoke(
            Planner(), skill="research_card", principal=Principal(id=caller)
        )
        assert name == "Research"

    assert len(server.requests) == 1
    headers = server.requests[0].headers
    assert "Authorization" not in headers
    assert "Cookie" not in headers


# Sources


async def test_an_unknown_name_is_a_definition_error() -> None:
    with pytest.raises(DefinitionError, match=r"No agent source lists .*'writer'"):
        await make_directory(CardServer()).get("writer")


async def test_a_name_in_two_sources_is_a_definition_error_naming_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTS", f"research-agent={CARD_URL}")
    agents = AgentDirectory(
        sources=[StaticRegistry(RESEARCH), EnvironmentAgentRegistry("AGENTS")],
        http_client=CardServer().client(),
    )

    with pytest.raises(
        DefinitionError,
        match=r"StaticRegistry\(\) and EnvironmentAgentRegistry\('AGENTS'\)",
    ):
        await agents.get("research-agent")


async def test_a_source_failure_raises_and_keeps_the_cached_card() -> None:
    server = CardServer()
    source = StaticRegistry(RESEARCH)
    agents = AgentDirectory(sources=[source], http_client=server.client())
    await agents.get("research-agent")

    source.failing = True
    with pytest.raises(DefinitionError, match="unavailable"):
        await agents.get("research-agent")

    source.failing = False
    await agents.get("research-agent")
    assert len(server.requests) == 1


# Lifecycle


async def test_construction_does_no_io_and_starts_no_tasks() -> None:
    server = CardServer()
    source = StaticRegistry(RESEARCH)
    tasks_before = asyncio.all_tasks()

    agents = AgentDirectory(sources=[source], http_client=server.client())
    async with agents:
        assert asyncio.all_tasks() == tasks_before

    assert source.reads == 0
    assert server.requests == []


async def test_a_caller_provided_client_is_not_closed() -> None:
    server = CardServer()
    client = server.client()

    async with AgentDirectory(sources=[StaticRegistry(RESEARCH)], http_client=client):
        pass

    assert not client.is_closed
    await client.aclose()


async def test_an_owned_client_is_created_on_enter_and_closed_on_exit() -> None:
    agents = AgentDirectory(sources=[StaticRegistry(RESEARCH)])

    async with agents as entered:
        owned = entered._client
        assert owned is not None
        assert not owned.is_closed

    assert owned.is_closed


async def test_get_without_a_client_asks_for_async_with() -> None:
    agents = AgentDirectory(sources=[StaticRegistry(RESEARCH)])

    with pytest.raises(DefinitionError, match="async with"):
        await agents.get("research-agent")


async def test_an_open_directory_cannot_be_entered_again() -> None:
    agents = AgentDirectory(sources=[StaticRegistry(RESEARCH)])

    async with agents:
        with pytest.raises(DefinitionError, match="already open"):
            await agents.__aenter__()


@pytest.mark.parametrize(
    "options",
    [
        {"sources": []},
        {"sources": [object()]},
        {"sources": [StaticRegistry()], "card_ttl_seconds": -1},
        {"sources": [StaticRegistry()], "card_ttl_seconds": float("inf")},
        {"sources": [StaticRegistry()], "card_ttl_seconds": True},
        {"sources": [StaticRegistry()], "allow_http_hosts": "dev.internal"},
    ],
)
def test_invalid_settings_are_definition_errors(options: dict[str, Any]) -> None:
    with pytest.raises(DefinitionError):
        AgentDirectory(**options)


# An in-process AgentFrame agent


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Research(Agent):
    @skill(
        id="find_sources",
        name="Find sources",
        description="Find sources.",
        tags=["research"],
    )
    async def find_sources(self, topic: str, ctx: RunContext) -> str:
        return topic


async def test_an_agentframe_apps_card_validates() -> None:
    app = AgentApp(
        agent=Research(), host=HostConfig(public_url=ORIGIN, auth=AllowAnonymous())
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    async with (
        client,
        AgentDirectory(
            sources=[StaticRegistry(RESEARCH)], http_client=client
        ) as agents,
    ):
        found = await agents.get("research-agent")

    assert found.card.skills[0].id == "find_sources"
    assert found.interface_url == f"{ORIGIN}/a2a"
