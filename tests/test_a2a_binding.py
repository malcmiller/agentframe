"""Smoke tests for the pinned A2A SDK and the selected A2A 1.0 binding.

These tests use a bare SDK app, independent of ``AgentApp``, to prove that the
supported install provides an A2A 1.0 JSON-RPC over HTTP server and client, and that the
pinned SDK rejects pre-1.0 requests while 0.3 compatibility stays disabled.
"""

from collections.abc import AsyncIterator
from importlib.metadata import version

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_message_text, new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Role,
    SendMessageRequest,
)
from a2a.utils.constants import (
    AGENT_CARD_WELL_KNOWN_PATH,
    PROTOCOL_VERSION_1_0,
    TransportProtocol,
)
from starlette.applications import Starlette

BASE_URL = "http://agent.test"
RPC_PATH = "/a2a"


class EchoExecutor(AgentExecutor):
    """Replies to every message with the same text."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.message is not None
        reply = new_text_message(f"echo: {get_message_text(context.message)}")
        await event_queue.enqueue_event(reply)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def echo_card() -> AgentCard:
    return AgentCard(
        name="echo",
        description="Echoes text.",
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{BASE_URL}{RPC_PATH}",
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


@pytest.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    card = echo_card()
    handler = DefaultRequestHandler(
        agent_executor=EchoExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
        ]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        yield client


def test_sdk_is_pinned_to_the_selected_release() -> None:
    assert version("a2a-sdk") == "1.3.0"


async def test_card_advertises_only_json_rpc_for_protocol_1_0(
    http_client: httpx.AsyncClient,
) -> None:
    response = await http_client.get(AGENT_CARD_WELL_KNOWN_PATH)

    assert response.status_code == 200
    assert response.json()["supportedInterfaces"] == [
        {
            "url": f"{BASE_URL}{RPC_PATH}",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ]


async def test_official_client_sends_a_message_over_json_rpc(
    http_client: httpx.AsyncClient,
) -> None:
    config = ClientConfig(
        httpx_client=http_client,
        streaming=False,
        supported_protocol_bindings=[TransportProtocol.JSONRPC],
    )
    client = ClientFactory(config).create(echo_card())
    request = SendMessageRequest(message=new_text_message("hello", role=Role.ROLE_USER))

    responses = [response async for response in client.send_message(request)]

    assert len(responses) == 1
    assert get_message_text(responses[0].message) == "echo: hello"


SEND_MESSAGE = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "SendMessage",
    "params": {
        "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": "hi"}]}
    },
}


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing header means 0.3"),
        pytest.param({"A2A-Version": "0.3"}, id="explicit 0.3"),
    ],
)
async def test_pre_1_0_requests_are_rejected(
    http_client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await http_client.post(RPC_PATH, json=SEND_MESSAGE, headers=headers)

    error = response.json()["error"]
    assert error["data"][0]["reason"] == "VERSION_NOT_SUPPORTED"


async def test_1_0_json_rpc_requests_are_accepted(
    http_client: httpx.AsyncClient,
) -> None:
    response = await http_client.post(
        RPC_PATH, json=SEND_MESSAGE, headers={"A2A-Version": "1.0"}
    )

    reply = response.json()["result"]["message"]
    assert reply["parts"] == [{"text": "echo: hi"}]
