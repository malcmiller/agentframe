"""A model-free agent, served over A2A to callers with a development API key.

No model, provider extra, inference server or cloud credential is needed. One
development API key, read from ``ECHO_API_KEY``, signs in as ``developer``::

    ECHO_API_KEY=dev-only-key agentframe run examples.echo:create_app

Callers send the key in the ``X-API-Key`` header. The app sets no
``authorization=`` policy: ``authorization=`` is for resource checks;
authenticated callers can already run skills without ``requires_permissions``.
"""

import os

from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    HostConfig,
    Inject,
    RunContext,
    Services,
    agent,
    skill,
)


class Greeter:
    """A service the ``greet`` skill receives by injection."""

    greeting = "Hello"

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}!"


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text or a greeting."""

    @skill(tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill(tags=["greeting"])
    async def greet(self, name: str, greeter: Inject[Greeter], ctx: RunContext) -> str:
        """Greet someone by name."""
        return greeter.greet(name)


def create_app() -> AgentApp:
    """Serve the echo agent over A2A, for callers with the ``ECHO_API_KEY`` key.

    Raises:
        KeyError: ``ECHO_API_KEY`` is not set.
    """
    # Development placeholder: one API key from the environment.
    keys = {os.environ["ECHO_API_KEY"]: APIKey("developer")}
    return AgentApp(
        agent=Echo(),
        host=HostConfig(
            public_url=os.environ.get("ECHO_PUBLIC_URL", "http://127.0.0.1:8000"),
            auth=APIKeyAuth(keys=keys),
        ),
        services=Services().add(Greeter, Greeter()),
    )
