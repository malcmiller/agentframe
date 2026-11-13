"""``FoundryAgentRegistry`` against a fake Foundry project agents API.

The fake is an ``azure-core`` transport, so the official ``azure-ai-projects``
client sends real requests and parses real responses; only the network is fake.
"""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.core.pipeline.transport import AsyncHttpTransport
from azure.core.rest import HttpRequest

# azure-core has no public in-memory response; this is the class its own
# transports build, so the client parses it exactly like a real one.
from azure.core.rest._http_response_impl_async import AsyncHttpResponseImpl

from agentframe import (
    AgentDirectory,
    AgentQuery,
    AgentReference,
    DefinitionError,
    FileAgentRegistry,
    FoundryAgentRegistry,
    RemoteAgentError,
)

PROJECT = "https://contoso.services.ai.azure.com/api/projects/research"
SOURCE_TOKEN = "source-token"
A2A_ENABLED: dict[str, Any] = {"protocol_configuration": {"responses": {}, "a2a": {}}}
RESPONSES_ONLY: dict[str, Any] = {"protocol_configuration": {"responses": {}}}


def card_url(name: str) -> str:
    return f"{PROJECT}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0"


def foundry_agent(name: str, endpoint: dict[str, Any] | None) -> dict[str, Any]:
    agent: dict[str, Any] = {
        "object": "agent",
        "id": f"{name}-id",
        "name": name,
        "state": "enabled",
        "configuration_state": "enabled",
        "versions": {},
    }
    if endpoint is not None:
        agent["agent_endpoint"] = endpoint
    return agent


class FakeFoundry(AsyncHttpTransport):  # type: ignore[type-arg]
    """A fake Foundry project that serves its agents in pages."""

    def __init__(self, *pages: list[dict[str, Any]]) -> None:
        self.pages = list(pages)
        self.requests: list[HttpRequest] = []
        self.fail_page: int | None = None

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponseImpl:
        self.requests.append(request)
        path = urlsplit(request.url).path.removeprefix(urlsplit(PROJECT).path)
        if path == "/agents":
            return self.page(request)
        name = path.removeprefix("/agents/")
        for agent in (agent for page in self.pages for agent in page):
            if agent["name"] == name:
                return reply(request, 200, agent)
        return reply(request, 404, {"error": {"code": "not_found"}})

    def page(self, request: HttpRequest) -> AsyncHttpResponseImpl:
        # Foundry pages by cursor: each page's last agent ID is the next "after".
        after = parse_qs(urlsplit(request.url).query).get("after", [None])[0]
        ids = [page[-1]["id"] for page in self.pages]
        index = 0 if after is None else ids.index(after) + 1
        if index == self.fail_page:
            return reply(request, 403, {"error": {"code": "forbidden"}})
        data = self.pages[index] if index < len(self.pages) else []
        last_id = data[-1]["id"] if data else None
        return reply(request, 200, {"data": data, "last_id": last_id})

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeFoundry":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass


def reply(request: HttpRequest, status: int, body: object) -> AsyncHttpResponseImpl:
    response = AsyncHttpResponseImpl(
        request=request,
        internal_response=None,
        status_code=status,
        reason="",
        content_type="application/json",
        headers={"Content-Type": "application/json"},
        stream_download_generator=None,
    )
    response._content = json.dumps(body).encode()
    response._is_closed = True
    response._is_stream_consumed = True
    return response


class FakeCredential:
    """An Entra credential that hands out one fixed token, or fails."""

    def __init__(self) -> None:
        self.scopes: list[tuple[str, ...]] = []
        self.failing = False

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        if self.failing:
            raise ClientAuthenticationError("az login has expired for someone@x")
        return AccessToken(SOURCE_TOKEN, 2**40)

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> "FakeCredential":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass


def registry(
    foundry: FakeFoundry, credential: FakeCredential | None = None
) -> FoundryAgentRegistry:
    return FoundryAgentRegistry(
        project_endpoint=f"{PROJECT}/",
        credential=credential or FakeCredential(),
        transport=foundry,
    )


TWO_PAGES = FakeFoundry(
    [
        foundry_agent("research-agent", A2A_ENABLED),
        foundry_agent("chat-only", RESPONSES_ONLY),
    ],
    [
        foundry_agent("no-endpoint", None),
        foundry_agent("review-agent", A2A_ENABLED),
    ],
)


# Listing


async def test_list_reads_every_page_and_keeps_only_a2a_enabled_agents() -> None:
    foundry = FakeFoundry(*TWO_PAGES.pages)

    found = await registry(foundry).list(AgentQuery())

    assert found == [
        AgentReference("research-agent", card_url("research-agent")),
        AgentReference("review-agent", card_url("review-agent")),
    ]
    assert len(foundry.requests) == 3  # two pages, then an empty one
    assert all(
        r.headers["Authorization"] == "Bearer source-token" for r in foundry.requests
    )


async def test_list_keeps_only_the_queried_names_from_any_page() -> None:
    foundry = FakeFoundry(*TWO_PAGES.pages)

    found = await registry(foundry).list(
        AgentQuery(names=["review-agent", "chat-only", "missing"])
    )

    assert found == [AgentReference("review-agent", card_url("review-agent"))]


async def test_an_empty_project_lists_no_agents() -> None:
    assert await registry(FakeFoundry()).list(AgentQuery()) == []


async def test_a_failed_page_raises_instead_of_returning_the_first_page() -> None:
    foundry = FakeFoundry(*TWO_PAGES.pages)
    foundry.fail_page = 1

    with pytest.raises(DefinitionError, match=r"cannot be read: HTTP 403\.$"):
        await registry(foundry).list(AgentQuery(names=["research-agent"]))


async def test_a_credential_failure_raises_without_its_message() -> None:
    credential = FakeCredential()
    credential.failing = True

    with pytest.raises(DefinitionError, match="ClientAuthenticationError") as error:
        await registry(FakeFoundry(), credential).list(AgentQuery())

    assert "someone" not in str(error.value)


# Getting one agent


async def test_get_returns_an_a2a_enabled_agent_with_one_call() -> None:
    foundry = FakeFoundry(*TWO_PAGES.pages)

    found = await registry(foundry).get("review-agent")

    assert found == AgentReference("review-agent", card_url("review-agent"))
    assert [urlsplit(r.url).path for r in foundry.requests] == [
        "/api/projects/research/agents/review-agent"
    ]


async def test_get_refuses_an_agent_without_incoming_a2a() -> None:
    with pytest.raises(DefinitionError, match="'chat-only' does not have incoming A2A"):
        await registry(FakeFoundry(*TWO_PAGES.pages)).get("chat-only")


async def test_get_of_an_unknown_agent_names_it() -> None:
    with pytest.raises(DefinitionError, match="does not list an agent named 'writer'"):
        await registry(FakeFoundry(*TWO_PAGES.pages)).get("writer")


# Construction


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://contoso.services.ai.azure.com/api/projects/research",
        "https://contoso.services.ai.azure.com/api/projects/research?key=secret",
        "contoso.services.ai.azure.com/api/projects/research",
    ],
)
def test_the_project_endpoint_must_be_a_plain_https_url(endpoint: str) -> None:
    with pytest.raises(DefinitionError, match="absolute https URL"):
        FoundryAgentRegistry(project_endpoint=endpoint, credential=FakeCredential())


def test_construction_does_no_io() -> None:
    foundry = FakeFoundry()
    credential = FakeCredential()

    registry(foundry, credential)

    assert foundry.requests == []
    assert credential.scopes == []


# Card fetches through AgentDirectory


def foundry_card(card_url: str) -> dict[str, Any]:
    # Foundry's card path differs from its interface path, on the same origin.
    return {
        "name": "Foundry agent",
        "description": "A Foundry agent.",
        "version": "1.0",
        "supportedInterfaces": [
            {
                "url": card_url.removesuffix("/agentCard/v1.0"),
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


class CardHost:
    """Serves Agent Cards over a mock httpx transport and records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.redirect_to: str | None = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.redirect_to is not None:
            return httpx.Response(302, headers={"Location": self.redirect_to})
        return httpx.Response(200, json=foundry_card(str(request.url)))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


async def test_card_fetches_send_only_the_source_token_to_the_card_url(
    tmp_path: Any,
) -> None:
    other_card = "https://other.example.com/.well-known/agent-card.json"
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(f"- name: other-agent\n  url: {other_card}\n")
    credential = FakeCredential()
    host = CardHost()
    agents = AgentDirectory(
        sources=[
            registry(FakeFoundry(*TWO_PAGES.pages), credential),
            FileAgentRegistry(agents_file),
        ],
        http_client=host.client(),
    )

    found = await agents.get("review-agent")
    await agents.get("other-agent")

    assert (
        found.interface_url == f"{PROJECT}/agents/review-agent/endpoint/protocols/a2a"
    )
    foundry_request, other_request = host.requests
    assert str(foundry_request.url) == card_url("review-agent")
    assert foundry_request.headers["Authorization"] == f"Bearer {SOURCE_TOKEN}"
    assert str(other_request.url) == other_card
    assert "Authorization" not in other_request.headers
    assert ("https://ai.azure.com/.default",) in credential.scopes


async def test_find_fetches_foundry_cards_with_the_source_token() -> None:
    host = CardHost()
    agents = AgentDirectory(
        sources=[registry(FakeFoundry(*TWO_PAGES.pages))], http_client=host.client()
    )

    assert await agents.find(skill="general-qa") == []

    assert sorted(str(r.url) for r in host.requests) == [
        card_url("research-agent"),
        card_url("review-agent"),
    ]
    assert all(
        r.headers["Authorization"] == f"Bearer {SOURCE_TOKEN}" for r in host.requests
    )


async def test_a_redirected_card_request_never_carries_the_token_onward() -> None:
    host = CardHost()
    host.redirect_to = "https://attacker.example.com/card.json"
    agents = AgentDirectory(
        sources=[registry(FakeFoundry(*TWO_PAGES.pages))], http_client=host.client()
    )

    with pytest.raises(RemoteAgentError, match="redirected"):
        await agents.get("research-agent")

    assert [str(r.url) for r in host.requests] == [card_url("research-agent")]


async def test_card_headers_refuse_a_card_outside_the_project() -> None:
    elsewhere = AgentReference("x", "https://attacker.example.com/agents/x/card")

    with pytest.raises(DefinitionError, match="not a card in Foundry project"):
        await registry(FakeFoundry()).card_headers(elsewhere)
