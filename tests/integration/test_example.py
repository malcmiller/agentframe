"""The README examples and ``examples/echo.py``, kept working as written."""

import httpx
import pytest
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest

from agentframe import AgentApp, Runtime, Services
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from examples.echo import Echo, Greeter, create_app
from local_server import ServeLocally


async def test_readme_example() -> None:
    services = Services()
    services.add(Greeter, Greeter())
    runtime = Runtime(services=services)

    value = await runtime.invoke(Echo(), skill="echo", arguments={"text": "hi"})
    result = await runtime.run(Echo(), skill="greet", arguments={"name": "Ada"})

    assert value == "hi"
    assert result.value == "Hello, Ada!"


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
