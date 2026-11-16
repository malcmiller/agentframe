"""Tests for ``AgentApp(resources=...)`` and opening the app's directory.

The app opens its resources and directory at lifespan startup, before the
startup checks, and closes them in reverse order after shutdown.
"""

from collections.abc import Sequence
from typing import Any, Self

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
    HostConfig,
    ModelCapabilities,
    RunContext,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from local_server import Lifespan

HOST = HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous())


@agent(version="1.0.0")
class Writer(Agent):
    """Has a model, so startup checks it."""

    @skill("write", tags=["test"])
    async def write(self, text: str, ctx: RunContext) -> str:
        """Send ``text`` to the model."""
        return await ctx.generate(text)


class RecordedModel(ScriptedModel):
    """A model that records when it is opened, checked and closed."""

    def __init__(self, log: list[str], capabilities: ModelCapabilities | None = None):
        super().__init__(capabilities=capabilities)
        self.log = log

    async def __aenter__(self) -> Self:
        self.log.append("open model")
        return self

    async def capabilities(self) -> ModelCapabilities:
        self.log.append("check model")
        return await super().capabilities()

    async def close(self) -> None:
        self.log.append("close model")
        await super().close()


class Credential:
    """A resource with only ``aclose()``."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def aclose(self) -> None:
        self.log.append("close credential")


class NoAgents(AgentRegistry):
    async def get(self, agent_id: str) -> AgentReference:
        raise DefinitionError(f"Unknown agent {agent_id!r}.")

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return []


async def test_resources_open_before_startup_checks_and_close_in_reverse() -> None:
    log: list[str] = []
    model = RecordedModel(log)
    app = AgentApp(
        agent=Writer(model=model), host=HOST, resources=[Credential(log), model]
    )
    lifespan = Lifespan(app)

    assert await lifespan.startup() == "lifespan.startup.complete"
    assert log == ["open model", "check model"]
    await lifespan.shutdown()

    assert log == ["open model", "check model", "close model", "close credential"]


async def test_failed_startup_closes_what_was_opened() -> None:
    log: list[str] = []
    # No context window, so the startup check fails.
    model = RecordedModel(log, capabilities=ModelCapabilities())
    credential = Credential(log)
    app = AgentApp(agent=Writer(model=model), host=HOST, resources=[credential, model])

    assert await Lifespan(app).startup() == "lifespan.startup.failed"
    assert log == ["open model", "check model", "close model", "close credential"]


async def test_unlisted_models_are_not_closed() -> None:
    model = ScriptedModel()
    lifespan = Lifespan(AgentApp(agent=Writer(model=model), host=HOST))

    await lifespan.startup()
    await lifespan.shutdown()

    assert not model.closed


async def test_the_app_opens_and_closes_a_directory_that_is_not_open() -> None:
    directory = AgentDirectory(sources=[NoAgents()])
    lifespan = Lifespan(AgentApp(agent=Writer(), host=HOST, directory=directory))

    assert await lifespan.startup() == "lifespan.startup.complete"
    assert directory.is_open
    await lifespan.shutdown()

    assert not directory.is_open


async def test_an_open_directory_stays_open() -> None:
    async with AgentDirectory(sources=[NoAgents()]) as directory:
        lifespan = Lifespan(AgentApp(agent=Writer(), host=HOST, directory=directory))

        await lifespan.startup()
        await lifespan.shutdown()

        assert directory.is_open


async def test_a_directory_listed_in_resources_is_opened_once() -> None:
    directory = AgentDirectory(sources=[NoAgents()])
    app = AgentApp(
        agent=Writer(), host=HOST, directory=directory, resources=[directory]
    )
    lifespan = Lifespan(app)

    assert await lifespan.startup() == "lifespan.startup.complete"
    await lifespan.shutdown()

    assert not directory.is_open


async def test_an_http_client_resource_is_closed() -> None:
    client = httpx.AsyncClient()
    lifespan = Lifespan(AgentApp(agent=Writer(), host=HOST, resources=[client]))

    await lifespan.startup()
    await lifespan.shutdown()

    assert client.is_closed


@pytest.mark.parametrize(
    ("resources", "message"),
    [
        ([object()], "neither an async context manager nor has an async aclose"),
        ("model", "must be a list"),
    ],
)
def test_resources_that_cannot_be_closed_are_refused(
    resources: Sequence[Any], message: str
) -> None:
    with pytest.raises(DefinitionError, match=message):
        AgentApp(agent=Writer(), host=HOST, resources=resources)


def test_a_resource_listed_twice_is_refused() -> None:
    model = ScriptedModel()

    with pytest.raises(DefinitionError, match="same object twice"):
        AgentApp(agent=Writer(model=model), host=HOST, resources=[model, model])
