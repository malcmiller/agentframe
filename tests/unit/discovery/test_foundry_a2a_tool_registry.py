"""``FoundryA2AToolRegistry`` against a fake Foundry project connections API.

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
    FoundryA2AToolRegistry,
)

PROJECT = "https://contoso.services.ai.azure.com/api/projects/research"
SOURCE_TOKEN = "source-token"
WELL_KNOWN = "/.well-known/agent-card.json"
IN_PROJECT_TARGET = f"{PROJECT}/agents/writer/endpoint/protocols/a2a"


def connection(
    name: str,
    target: str | None,
    kind: str = "RemoteA2A",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    # The REST shape of a connection, as azure-ai-projects 2.11.0 models it.
    found: dict[str, Any] = {
        "name": name,
        "id": f"/connections/{name}",
        "type": kind,
        "isDefault": False,
        "credentials": {"type": "CustomKeys"},
        "metadata": metadata or {},
    }
    if target is not None:
        found["target"] = target
    return found


class FakeFoundry(AsyncHttpTransport):  # type: ignore[type-arg]
    """A fake Foundry project that serves its connections in pages."""

    def __init__(self, *pages: list[dict[str, Any]]) -> None:
        self.pages = list(pages)
        self.requests: list[HttpRequest] = []
        self.timeouts: list[dict[str, Any]] = []
        self.fail_page: int | None = None

    async def send(self, request: HttpRequest, **kwargs: Any) -> AsyncHttpResponseImpl:
        self.requests.append(request)
        self.timeouts.append(
            {key: value for key, value in kwargs.items() if key.endswith("timeout")}
        )
        path = urlsplit(request.url).path.removeprefix(urlsplit(PROJECT).path)
        if path == "/connections":
            return self.page(request)
        name = path.removeprefix("/connections/")
        for found in (item for page in self.pages for item in page):
            if found["name"] == name:
                return reply(request, 200, found)
        return reply(request, 404, {"error": {"code": "NotFound"}})

    def page(self, request: HttpRequest) -> AsyncHttpResponseImpl:
        index = int(parse_qs(urlsplit(request.url).query).get("page", ["0"])[0])
        if index == self.fail_page:
            return reply(request, 403, {"error": {"code": "Forbidden"}})
        body: dict[str, Any] = {"value": self.pages[index] if self.pages else []}
        if index + 1 < len(self.pages):
            body["nextLink"] = f"{PROJECT}/connections?api-version=v1&page={index + 1}"
        return reply(request, 200, body)

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
) -> FoundryA2AToolRegistry:
    return FoundryA2AToolRegistry(
        project_endpoint=f"{PROJECT}/",
        credential=credential or FakeCredential(),
        transport=foundry,
    )


def two_pages() -> FakeFoundry:
    return FakeFoundry(
        [
            connection("research", "https://research.example.com/a2a/"),
            connection("search-index", "https://search.example.com", "CognitiveSearch"),
        ],
        [
            connection("writer", IN_PROJECT_TARGET),
        ],
    )


# Listing


async def test_list_reads_every_page_and_maps_each_a2a_connection() -> None:
    foundry = two_pages()

    found = await registry(foundry).list(AgentQuery())

    assert found == [
        AgentReference("research", f"https://research.example.com/a2a{WELL_KNOWN}"),
        AgentReference("writer", f"{IN_PROJECT_TARGET}/agentCard/v1.0"),
    ]
    assert len(foundry.requests) == 2
    first = parse_qs(urlsplit(foundry.requests[0].url).query)
    assert first["connectionType"] == ["RemoteA2A"]
    assert all(
        r.headers["Authorization"] == f"Bearer {SOURCE_TOKEN}" for r in foundry.requests
    )


async def test_list_keeps_only_the_queried_names_from_any_page() -> None:
    found = await registry(two_pages()).list(
        AgentQuery(names=["writer", "search-index", "missing"])
    )

    assert found == [AgentReference("writer", f"{IN_PROJECT_TARGET}/agentCard/v1.0")]


async def test_an_empty_project_lists_no_agents() -> None:
    assert await registry(FakeFoundry()).list(AgentQuery()) == []


async def test_every_request_has_ten_second_connect_and_read_timeouts() -> None:
    foundry = two_pages()
    foundry_registry = registry(foundry)

    await foundry_registry.list(AgentQuery())
    await foundry_registry.get("writer")

    assert len(foundry.requests) == 3
    assert foundry.timeouts == [
        {"connection_timeout": 10.0, "read_timeout": 10.0}
    ] * len(foundry.requests)


async def test_connection_credentials_are_never_requested() -> None:
    foundry = two_pages()
    foundry_registry = registry(foundry)

    await foundry_registry.list(AgentQuery())
    await foundry_registry.get("research")

    assert all(r.method == "GET" for r in foundry.requests)
    assert not any("WithCredentials" in r.url for r in foundry.requests)


async def test_a_failed_page_raises_instead_of_returning_the_first_page() -> None:
    foundry = two_pages()
    foundry.fail_page = 1

    with pytest.raises(DefinitionError, match=r"cannot be read: HTTP 403\.$"):
        await registry(foundry).list(AgentQuery(names=["research"]))


async def test_a_credential_failure_raises_without_its_message() -> None:
    credential = FakeCredential()
    credential.failing = True

    with pytest.raises(DefinitionError, match="ClientAuthenticationError") as error:
        await registry(FakeFoundry(), credential).list(AgentQuery())

    assert "someone" not in str(error.value)


# Card URLs and malformed connections


@pytest.mark.parametrize(
    ("target", "card_url"),
    [
        ("https://a.example.com", f"https://a.example.com{WELL_KNOWN}"),
        ("https://a.example.com/", f"https://a.example.com{WELL_KNOWN}"),
        ("http://localhost:8000/a2a", f"http://localhost:8000/a2a{WELL_KNOWN}"),
        (f"{IN_PROJECT_TARGET}/", f"{IN_PROJECT_TARGET}/agentCard/v1.0"),
        (
            "https://other.services.ai.azure.com/api/projects/p/agents/a/endpoint"
            "/protocols/a2a",
            "https://other.services.ai.azure.com/api/projects/p/agents/a/endpoint"
            "/protocols/a2a/agentCard/v1.0",
        ),
    ],
)
async def test_the_card_url_is_built_from_the_target(
    target: str, card_url: str
) -> None:
    found = await registry(FakeFoundry([connection("peer", target)])).get("peer")

    assert found == AgentReference("peer", card_url)


@pytest.mark.parametrize(
    ("target", "card_path", "card_url"),
    [
        (
            "https://a.example.com/a2a",
            "/card.json",
            "https://a.example.com/a2a/card.json",
        ),
        (
            "https://a.example.com/a2a/",
            "card.json",
            "https://a.example.com/a2a/card.json",
        ),
        # Microsoft's sample connection to a Foundry agent sets this path.
        (IN_PROJECT_TARGET, "/agentCard/v0.3", f"{IN_PROJECT_TARGET}/agentCard/v0.3"),
        ("https://a.example.com", "/", f"https://a.example.com{WELL_KNOWN}"),
    ],
)
async def test_the_metadata_card_path_is_joined_to_the_target(
    target: str, card_path: str, card_url: str
) -> None:
    peer = connection("peer", target, metadata={"AgentCardPath": card_path})

    found = await registry(FakeFoundry([peer])).get("peer")

    assert found == AgentReference("peer", card_url)


@pytest.mark.parametrize("card_path", ["/card?code=secret", "/card#secret"])
async def test_a_card_path_cannot_add_a_query_or_fragment(card_path: str) -> None:
    peer = connection(
        "peer", "https://a.example.com", metadata={"AgentCardPath": card_path}
    )

    with pytest.raises(DefinitionError, match="query or fragment") as error:
        await registry(FakeFoundry([peer])).get("peer")

    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "research.example.com",
        "ftp://research.example.com",
        "https://user:secret@research.example.com",
        "https://research.example.com/a2a?code=secret",
        "https://research.example.com/a2a#fragment",
    ],
)
async def test_a_malformed_a2a_connection_fails_without_echoing_it(
    target: str | None,
) -> None:
    foundry = FakeFoundry([connection("broken", target)])

    with pytest.raises(DefinitionError, match="'broken' must have a target") as error:
        await registry(foundry).list(AgentQuery())

    assert "secret" not in str(error.value)


async def test_a_malformed_connection_outside_the_query_is_not_checked() -> None:
    foundry = FakeFoundry(
        [connection("broken", None), connection("research", "https://r.example.com")]
    )

    found = await registry(foundry).list(AgentQuery(names=["research"]))

    assert [reference.name for reference in found] == ["research"]


# Getting one connection


async def test_get_returns_an_a2a_connection_with_one_call() -> None:
    foundry = two_pages()

    found = await registry(foundry).get("writer")

    assert found == AgentReference("writer", f"{IN_PROJECT_TARGET}/agentCard/v1.0")
    assert [urlsplit(r.url).path for r in foundry.requests] == [
        "/api/projects/research/connections/writer"
    ]


async def test_get_refuses_a_connection_of_another_type() -> None:
    with pytest.raises(DefinitionError, match="'search-index' is not an A2A"):
        await registry(two_pages()).get("search-index")


async def test_get_of_an_unknown_connection_names_it() -> None:
    with pytest.raises(
        DefinitionError, match="does not have a connection named 'missing'"
    ):
        await registry(two_pages()).get("missing")


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
    with pytest.raises(
        DefinitionError, match=r"FoundryA2AToolRegistry.*absolute https"
    ):
        FoundryA2AToolRegistry(project_endpoint=endpoint, credential=FakeCredential())


def test_construction_does_no_io() -> None:
    foundry = FakeFoundry()
    credential = FakeCredential()

    registry(foundry, credential)

    assert foundry.requests == []
    assert credential.scopes == []


# Card headers


async def test_card_headers_send_the_token_only_inside_the_project() -> None:
    credential = FakeCredential()
    foundry_registry = registry(FakeFoundry(), credential)
    inside = AgentReference("writer", f"{IN_PROJECT_TARGET}/agentCard/v1.0")
    elsewhere = [
        AgentReference("peer", f"https://research.example.com{WELL_KNOWN}"),
        # Another project on the same account is still another project.
        AgentReference(
            "other",
            "https://contoso.services.ai.azure.com/api/projects/research-2/agents/a"
            "/endpoint/protocols/a2a/agentCard/v1.0",
        ),
    ]

    assert await foundry_registry.card_headers(inside) == {
        "Authorization": f"Bearer {SOURCE_TOKEN}"
    }
    for reference in elsewhere:
        assert await foundry_registry.card_headers(reference) == {}
    assert credential.scopes == [("https://ai.azure.com/.default",)]


def a2a_card(card_url: str) -> dict[str, Any]:
    interface = card_url.removesuffix(WELL_KNOWN).removesuffix("/agentCard/v1.0")
    return {
        "name": "Connected agent",
        "description": "An agent a Foundry project connects to.",
        "version": "1.0",
        "supportedInterfaces": [
            {"url": interface, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [],
    }


async def test_directory_fetches_cards_with_the_token_only_inside_the_project() -> None:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=a2a_card(str(request.url)))

    agents = AgentDirectory(
        sources=[registry(two_pages())],
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )

    research = await agents.get("research")
    writer = await agents.get("writer")

    assert research.interface_url == "https://research.example.com/a2a"
    assert writer.interface_url == IN_PROJECT_TARGET
    research_request, writer_request = requests
    assert "Authorization" not in research_request.headers
    assert writer_request.headers["Authorization"] == f"Bearer {SOURCE_TOKEN}"
