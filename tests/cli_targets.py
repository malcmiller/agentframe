"""Application code that the ``agentframe run`` tests point the command at."""

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


def create_nothing() -> None:
    return None


def create_invalid_app() -> AgentApp:
    return AgentApp(
        agent=Undecorated(),
        host=HostConfig(public_url="https://echo.example.com", auth=AllowAnonymous()),
    )


not_a_function = "create_app"
