import asyncio
from typing import Any

import httpx
import pytest

from agentframe import (
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    DefinitionError,
    RemoteAgentError,
)


def card_url(name: str) -> str:
    return f"https://{name}.example.com/.well-known/agent-card.json"


def reference(name: str) -> AgentReference:
    return AgentReference(name, card_url(name))


def card_json(name: str, skills: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"The {name} agent.",
        "version": "0.1.0",
        "supportedInterfaces": [
            {
                "url": f"https://{name}.example.com/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def advertised(skill_id: str, *tags: str) -> dict[str, Any]:
    return {"id": skill_id, "name": skill_id, "description": "", "tags": list(tags)}


class ListRegistry(AgentRegistry):
    """An in-memory source that lists its references, or fails."""

    def __init__(self, *references: AgentReference, failing: bool = False) -> None:
        self.references = list(references)
        self.failing = failing

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        if self.failing:
            raise DefinitionError("List registry is unavailable.")
        if query.names is None:
            return list(self.references)
        return [ref for ref in self.references if ref.name in query.names]

    def __repr__(self) -> str:
        return "ListRegistry()"


class CardServer:
    """Fake remote agents that serve cards by host and count fetches."""

    def __init__(self, cards: dict[str, dict[str, Any]]) -> None:
        self.cards = cards
        self.unreachable: set[str] = set()
        self.fetches = 0
        self.in_flight = 0
        self.most_in_flight = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            name = request.url.host.split(".")[0]
            if name in self.unreachable:
                raise httpx.ConnectError("unreachable", request=request)
            return httpx.Response(200, json=self.cards[name])
        finally:
            self.in_flight -= 1

    def directory(self, *sources: AgentRegistry, **options: Any) -> AgentDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return AgentDirectory(sources=sources, http_client=client, **options)


@pytest.fixture
def server() -> CardServer:
    return CardServer(
        {
            "research": card_json(
                "research",
                [advertised("find_sources", "research", "web")],
            ),
            "review": card_json(
                "review",
                [advertised("review", "research"), advertised("find_sources")],
            ),
            "billing": card_json("billing", [advertised("invoice", "finance")]),
        }
    )


@pytest.fixture
def directory(server: CardServer) -> AgentDirectory:
    source = ListRegistry(reference("research"), reference("review"))
    return server.directory(source, ListRegistry(reference("billing")))


def names(cards: list[Any]) -> list[str]:
    return [card.name for card in cards]


async def test_find_by_skill_returns_matching_cards_in_source_order(
    directory: AgentDirectory,
) -> None:
    cards = await directory.find(skill="find_sources")

    assert names(cards) == ["research", "review"]


async def test_find_by_tags_needs_every_tag(directory: AgentDirectory) -> None:
    assert names(await directory.find(tags=["research"])) == ["research", "review"]
    assert names(await directory.find(tags=["research", "web"])) == ["research"]
    assert await directory.find(tags=["research", "finance"]) == []


async def test_skill_and_tags_must_match_on_the_same_skill(
    directory: AgentDirectory,
) -> None:
    # The review agent has find_sources and a "research" tag, but on
    # different skills, so it does not match.
    cards = await directory.find(skill="find_sources", tags=["research"])

    assert names(cards) == ["research"]


async def test_find_uses_the_card_cache(
    server: CardServer, directory: AgentDirectory
) -> None:
    await directory.get("research")
    await directory.find(skill="invoice")
    await directory.find(skill="review")

    assert server.fetches == 3


async def test_an_unreachable_agent_makes_find_raise(
    server: CardServer, directory: AgentDirectory
) -> None:
    server.unreachable.add("billing")

    with pytest.raises(RemoteAgentError, match="'billing'"):
        await directory.find(skill="find_sources")


async def test_a_stale_card_is_used_when_allowed(server: CardServer) -> None:
    directory = server.directory(
        ListRegistry(reference("research")), card_ttl_seconds=0, allow_stale=True
    )
    await directory.get("research")
    server.unreachable.add("research")

    assert names(await directory.find(skill="find_sources")) == ["research"]


async def test_a_failing_source_makes_find_raise(server: CardServer) -> None:
    directory = server.directory(
        ListRegistry(reference("research")), ListRegistry(failing=True)
    )

    with pytest.raises(DefinitionError, match="unavailable"):
        await directory.find(skill="find_sources")


async def test_a_name_in_two_sources_makes_find_raise(server: CardServer) -> None:
    directory = server.directory(
        ListRegistry(reference("research")), ListRegistry(reference("research"))
    )

    with pytest.raises(DefinitionError, match="more than one source"):
        await directory.find(skill="find_sources")


async def test_find_fetches_at_most_eight_cards_at_a_time() -> None:
    many = [f"agent{number}" for number in range(20)]
    server = CardServer(
        {name: card_json(name, [advertised("find_sources")]) for name in many}
    )
    directory = server.directory(ListRegistry(*(reference(name) for name in many)))

    cards = await directory.find(skill="find_sources")

    assert names(cards) == many
    assert server.most_in_flight == 8


@pytest.mark.parametrize(
    ("filters", "message"),
    [
        ({}, "needs a skill, tags, or both"),
        ({"tags": []}, "needs a skill, tags, or both"),
        ({"tags": "research"}, "not a single string"),
    ],
)
async def test_find_refuses_missing_or_malformed_filters(
    directory: AgentDirectory, filters: dict[str, Any], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        await directory.find(**filters)
