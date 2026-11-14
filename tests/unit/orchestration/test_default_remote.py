"""``default_remote=`` authenticates skill-catalog agents without a
``remotes=`` entry.

The secured specialist is a real ``AgentApp`` on localhost behind
``APIKeyAuth``, so each test sees which key actually reached it. Cards that
are only looked at, never called, come from an ``httpx.MockTransport``.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    APIKey,
    APIKeyAuth,
    APIKeyCredential,
    DefinitionError,
    EntraAppCredential,
    HostConfig,
    ModelCapabilities,
    OnBehalfOf,
    PermissionDeniedError,
    Remote,
    RunContext,
    Runtime,
    Service,
    SkillCatalog,
    ToolCall,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel, ScriptedReply
from local_server import ServeLocally

CATALOG_LOGGER = "agentframe.orchestration.catalog"
TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
CALLERS = {
    "default-key": APIKey(caller_id="via-default"),
    "own-key": APIKey(caller_id="via-own-entry"),
}
API_KEY_SCHEMES = {
    "apiKey": {"apiKeySecurityScheme": {"location": "header", "name": "X-API-Key"}}
}
BEARER_SCHEMES = {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}}


@agent(name="Secured", description="Says who called it.", version="0.1.0")
class Secured(Agent):
    @skill(id="whoami", description="Return the caller's ID.", tags=["identity"])
    async def whoami(self, ctx: RunContext) -> str:
        assert ctx.principal is not None
        return ctx.principal.id


@agent(name="Orchestrator", description="Delegates.", version="0.1.0")
class Orchestrator(Agent):
    """Lists its catalog, and calls ``secured.whoami`` when told to."""

    def __init__(self, *, call: bool = False) -> None:
        replies: list[ScriptedReply] = []
        if call:
            replies = [ToolCall("1", "secured_whoami", "{}"), "done"]
        self.scripted = ScriptedModel(replies, capabilities=TOOL_MODEL)
        super().__init__(model=self.scripted)
        self.call = call
        self.catalog: SkillCatalog | None = None

    @skill(id="answer", description="Answer.", tags=["orchestration"])
    async def answer(self, ctx: RunContext) -> list[str]:
        catalog = await ctx.remote_skills()
        self.catalog = catalog
        if self.call:
            await ctx.generate("Who am I?", tools=[*catalog])
        return [entry.name for entry in catalog]

    def tool_results(self) -> list[str]:
        last = self.scripted.requests[-1].messages
        return [json.loads(m.content) for m in last if m.role == "tool"]


class Registered(AgentRegistry):
    """Lists agents by name and base URL."""

    def __init__(self, **urls: str) -> None:
        self.urls = urls

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [
            AgentReference(name, f"{url}/.well-known/agent-card.json")
            for name, url in self.urls.items()
        ]


type Serving = Callable[[], AbstractAsyncContextManager[AgentDirectory]]


@pytest.fixture
def secured(serve: ServeLocally) -> Serving:
    """Serve the API-key specialist; yield a directory listing it as 'secured'."""

    @asynccontextmanager
    async def serving() -> AsyncIterator[AgentDirectory]:
        def app(public_url: str) -> AgentApp:
            auth = APIKeyAuth(keys=CALLERS)
            return AgentApp(
                agent=Secured(), host=HostConfig(public_url=public_url, auth=auth)
            )

        async with (
            serve(app) as url,
            AgentDirectory(sources=[Registered(secured=url)]) as directory,
        ):
            yield directory

    return serving


def key(value: str) -> Service:
    return Service(credential=APIKeyCredential(value))


async def answer(runtime: Runtime, orchestrator: Orchestrator) -> list[str]:
    names: list[str] = await runtime.invoke(orchestrator, skill="answer")
    return names


# Who is called, with which credential


async def test_a_secured_agent_joins_the_catalog_with_the_default(
    secured: Serving,
) -> None:
    orchestrator = Orchestrator(call=True)
    async with secured() as directory:
        runtime = Runtime(directory=directory, default_remote=key("default-key"))
        names = await answer(runtime, orchestrator)

    assert names == ["secured.whoami"]
    assert orchestrator.tool_results() == ["via-default"]


async def test_a_secured_agent_is_skipped_without_the_default(
    secured: Serving, caplog: pytest.LogCaptureFixture
) -> None:
    async with secured() as directory:
        with caplog.at_level(logging.WARNING, logger=CATALOG_LOGGER):
            names = await answer(Runtime(directory=directory), Orchestrator())

    assert names == []
    assert "skipped agent 'secured': security not satisfied" in caplog.text


async def test_a_remotes_entry_wins_over_the_default(secured: Serving) -> None:
    orchestrator = Orchestrator(call=True)
    async with secured() as directory:
        runtime = Runtime(
            directory=directory,
            remotes={"secured": key("own-key")},
            default_remote=key("default-key"),
        )
        await answer(runtime, orchestrator)

    assert orchestrator.tool_results() == ["via-own-entry"]


class Cards:
    """Serves cards by host name and records every request."""

    def __init__(self, **cards: dict[str, Any]) -> None:
        self.cards = cards
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.cards[request.url.host.split(".")[0]])

    def directory(self) -> AgentDirectory:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        urls = {name: f"https://{name}.example.com" for name in self.cards}
        return AgentDirectory(sources=[Registered(**urls)], http_client=client)


def card(name: str, **fields: Any) -> dict[str, Any]:
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
        "skills": [],
        **fields,
    }


class CountingTokens:
    """Stands in for an ``azure.core`` async token credential."""

    def __init__(self) -> None:
        self.requested = 0

    async def get_token(self, *scopes: str) -> Any:
        self.requested += 1
        return type("AccessToken", (), {"token": "app-token"})()


async def test_a_default_that_cannot_meet_a_card_is_never_sent_to_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cards = Cards(keyed=card("keyed", securitySchemes=API_KEY_SCHEMES))
    tokens = CountingTokens()
    entra = EntraAppCredential(tokens, scope="api://agents/.default")
    runtime = Runtime(
        directory=cards.directory(), default_remote=Service(credential=entra)
    )

    with caplog.at_level(logging.WARNING, logger=CATALOG_LOGGER):
        names = await answer(runtime, Orchestrator())

    assert names == []
    assert "skipped agent 'keyed': security not satisfied" in caplog.text
    assert tokens.requested == 0
    assert [r.method for r in cards.requests] == ["GET"]


async def test_an_open_agent_never_gets_the_default() -> None:
    cards = Cards(open=card("open"))
    runtime = Runtime(directory=cards.directory(), default_remote=key("default-key"))
    orchestrator = Orchestrator()

    names = await answer(runtime, orchestrator)

    assert names == ["open"]
    assert orchestrator.catalog is not None
    # noinspection PyProtectedMember
    assert orchestrator.catalog._bindings["open"]._credential is None


# On behalf of the user


async def test_an_on_behalf_of_default_without_a_user_fails_and_never_falls_back() -> (
    None
):
    cards = Cards(entra=card("entra", securitySchemes=BEARER_SCHEMES))
    runtime = Runtime(
        directory=cards.directory(),
        default_remote=OnBehalfOf(scope="api://agents/.default"),
    )

    with pytest.raises(PermissionDeniedError, match="on behalf of the signed-in"):
        await answer(runtime, Orchestrator())

    assert [r.method for r in cards.requests] == ["GET"]


def test_an_on_behalf_of_default_on_an_api_key_host_fails_construction() -> None:
    host = HostConfig(
        public_url="https://orchestrator.example.com", auth=APIKeyAuth(keys=CALLERS)
    )

    with pytest.raises(DefinitionError, match="default_remote is called OnBehalfOf"):
        AgentApp(
            agent=Orchestrator(),
            host=host,
            directory=Cards().directory(),
            default_remote=OnBehalfOf(scope="api://agents/.default"),
        )


# Configuration


@agent(name="Declared", description="Calls one named agent.", version="0.1.0")
class Declared(Agent):
    @skill(id="answer", description="Answer.", tags=["orchestration"])
    async def answer(self, secured: Remote[Literal["secured"]], ctx: RunContext) -> str:
        return await secured.ask("hi")


async def test_declared_remotes_never_use_the_default(secured: Serving) -> None:
    async with secured() as directory:
        runtime = Runtime(directory=directory, default_remote=key("default-key"))

        with pytest.raises(DefinitionError, match="'secured' requires credentials"):
            await runtime.validate_remotes(Declared())


@pytest.mark.parametrize(
    "default",
    [APIKeyCredential("key"), {"secured": key("key")}, Service(credential="key")],  # type: ignore[arg-type]
    ids=["bare-credential", "mapping", "service-without-credential"],
)
def test_a_default_that_is_not_an_identity_is_refused(default: Any) -> None:
    with pytest.raises(DefinitionError, match="default_remote"):
        Runtime(default_remote=default)


def test_a_runtime_and_a_default_on_the_app_are_refused() -> None:
    host = HostConfig(
        public_url="https://orchestrator.example.com", auth=APIKeyAuth(keys=CALLERS)
    )

    with pytest.raises(DefinitionError, match="default_remote either"):
        AgentApp(
            agent=Orchestrator(),
            host=host,
            runtime=Runtime(),
            default_remote=key("default-key"),
        )
