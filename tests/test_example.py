"""The README examples, kept working as written."""

import os
from collections.abc import Mapping

import httpx
import pytest
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest

from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    AuthorizationPolicy,
    HostConfig,
    Inject,
    Principal,
    RunContext,
    Runtime,
    Services,
    SkillDefinition,
    agent,
    skill,
)
from agentframe.card import SKILL_CALL_EXTENSION_URI
from local_server import ServeLocally


class Greeter:
    greeting = "Hello"

    def greet(self, name: str) -> str:
        return f"{self.greeting}, {name}!"


@agent(version="0.1.0")
class Echo(Agent):
    """Returns supplied text or a greeting."""

    @skill(id="echo", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        """Return the text unchanged."""
        return text

    @skill(id="greet", tags=["greeting"])
    async def greet(self, name: str, greeter: Inject[Greeter], ctx: RunContext) -> str:
        """Greet someone by name."""
        return greeter.greet(name)


async def test_readme_example() -> None:
    services = Services()
    services.add(Greeter, Greeter())
    runtime = Runtime(services=services)

    value = await runtime.invoke(Echo(), skill="echo", arguments={"text": "hi"})
    result = await runtime.run(Echo(), skill="greet", arguments={"name": "Ada"})

    assert value == "hi"
    assert result.value == "Hello, Ada!"


# Hosting over A2A


class DevelopmentPolicy(AuthorizationPolicy):
    """Development placeholder: allow any authenticated caller."""

    # noinspection PyShadowingNames
    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        return principal is not None


def create_app() -> AgentApp:
    services = Services()
    services.add(Greeter, Greeter())
    # Development placeholder: one API key from the environment.
    keys = {os.environ["ECHO_API_KEY"]: APIKey("developer")}
    return AgentApp(
        agent=Echo(),
        host=HostConfig(
            public_url=os.environ.get("ECHO_PUBLIC_URL", "http://127.0.0.1:8000"),
            auth=APIKeyAuth(keys=keys),
        ),
        services=services,
        authorization=DevelopmentPolicy(),
    )


async def test_readme_hosting_example(
    serve: ServeLocally, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECHO_API_KEY", "dev-only-key")

    def make_app(public_url: str) -> AgentApp:
        monkeypatch.setenv("ECHO_PUBLIC_URL", public_url)
        return create_app()

    async with (
        serve(make_app) as base_url,
        httpx.AsyncClient(headers={"X-API-Key": "dev-only-key"}) as http,
    ):
        client = await ClientFactory(ClientConfig(httpx_client=http)).create_from_url(
            base_url
        )
        message = Message(
            message_id="m-1",
            role=Role.ROLE_USER,
            parts=[new_data_part({"name": "Ada"})],
            metadata={"agentframe.skill": "greet"},
        )
        activate = ClientCallContext(
            service_parameters=ServiceParametersFactory.create(
                [with_a2a_extensions([SKILL_CALL_EXTENSION_URI])]
            )
        )
        [response] = [
            r
            async for r in client.send_message(
                SendMessageRequest(message=message), context=activate
            )
        ]

    assert get_artifact_text(response.task.artifacts[0]) == "Hello, Ada!"
