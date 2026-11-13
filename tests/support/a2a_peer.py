"""A minimal A2A 1.0 agent built only on the official SDK, as another framework
would build one.

It contains no AgentFrame code and declares none of AgentFrame's extensions.
What it does depends on the text it is sent:

- ``"wait"``: waits until cancelled.
- ``"fail"``: fails its task with a message holding a secret detail.
- ``"question"``: asks a question back (``TASK_STATE_INPUT_REQUIRED``).
- anything else: echoes the text as its answer.

Every request is recorded, so tests can check exactly what reached it.
"""

import asyncio
from dataclasses import dataclass

from a2a.helpers import get_message_text, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from starlette.applications import Starlette

from local_server import AppFactory

PEER_QUESTION = "Which ocean do you mean?"
PEER_SECRET = "secret peer detail"


@dataclass
class ReceivedRequest:
    """What one request brought to the peer."""

    extensions: set[str]
    metadata: dict[str, object]
    message: Message


class PeerExecutor(AgentExecutor):
    """Echoes, waits, fails or asks back, recording every request."""

    def __init__(self) -> None:
        self.requests: list[ReceivedRequest] = []
        self.waiting = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None and context.task_id and context.context_id
        self.requests.append(
            ReceivedRequest(
                extensions=set(context.requested_extensions),
                metadata=dict(message.metadata.items()),
                message=message,
            )
        )
        await event_queue.enqueue_event(
            new_task(context.task_id, context.context_id, TaskState.TASK_STATE_WORKING)
        )
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        text = get_message_text(message)
        if text == "wait":
            self.waiting.set()
            await asyncio.sleep(60)
        elif text == "fail":
            await task.failed(task.new_agent_message([new_text_part(PEER_SECRET)]))
        elif text == "question":
            question = task.new_agent_message([new_text_part(PEER_QUESTION)])
            await task.requires_input(question)
        else:
            await task.add_artifact([new_text_part(f"Echo: {text}")])
            await task.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        self.cancelled.set()
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def a2a_peer(executor: PeerExecutor) -> AppFactory:
    """Build the peer's app for ``serve_locally``: card plus JSON-RPC endpoint.

    The card lists one skill, ``echo``, but no extensions, so AgentFrame can
    only reach it with free text.
    """

    def make_app(public_url: str) -> Starlette:
        card = AgentCard(
            name="Plain peer",
            description="An A2A agent built on the official SDK only.",
            version="1.0.0",
            supported_interfaces=[
                AgentInterface(
                    url=f"{public_url}/a2a",
                    protocol_binding=TransportProtocol.JSONRPC,
                    protocol_version=PROTOCOL_VERSION_1_0,
                )
            ],
            capabilities=AgentCapabilities(),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
            skills=[AgentSkill(id="echo", name="Echo", description="Echoes text.")],
        )
        handler = DefaultRequestHandler(
            agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
        )
        return Starlette(
            routes=[
                *create_agent_card_routes(card),
                *create_jsonrpc_routes(handler, rpc_url="/a2a"),
            ]
        )

    return make_app
