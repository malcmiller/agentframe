"""Application code that the ``agentframe run`` tests point the command at."""

import asyncio

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    HostConfig,
    RunContext,
    agent,
    skill,
)


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text."""

    @skill(id="echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text


@agent(version="0.1.0")
class Sleeper(Agent):
    """Has a skill that outlives any short shutdown grace period."""

    @skill(id="sleep", tags=["test"])
    async def sleep(self, ctx: RunContext) -> str:
        """Sleep for a minute."""
        await asyncio.sleep(60)
        return "awake"


class Undecorated(Agent):
    """An agent class someone forgot to decorate with ``@agent``."""


@agent(version="0.1.0")
class NeedsSettings(Echo):
    """An agent that cannot be built without arguments."""

    def __init__(self, greeting: str) -> None:
        self.greeting = greeting


def create_app() -> AgentApp:
    return AgentApp(
        agent=Echo(),
        host=HostConfig(public_url="https://echo.example.com", auth=AllowAnonymous()),
    )


def create_quick_shutdown_app() -> AgentApp:
    return AgentApp(
        agent=Sleeper(),
        host=HostConfig(
            public_url="https://sleeper.example.com",
            auth=AllowAnonymous(),
            shutdown_grace_seconds=1,
        ),
    )


def create_nothing() -> None:
    return None


def create_invalid_app() -> AgentApp:
    return AgentApp(
        agent=Undecorated(),
        host=HostConfig(public_url="https://echo.example.com", auth=AllowAnonymous()),
    )


not_a_function = "create_app"
