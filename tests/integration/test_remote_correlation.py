"""One request's correlation and trace context across agents, over real A2A.

Every agent is served over localhost HTTP and called with the official SDK
client. AgentFrame agents are ``AgentApp``s; the non-AgentFrame peer is a bare
official-SDK server that advertises no AgentFrame context fields.
"""

import logging
import uuid
from collections.abc import Sequence
from typing import Literal

import httpx
import pytest
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
    GetTaskRequest,
    Message,
    Role,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from a2a.utils.errors import TaskNotFoundError
from starlette.applications import Starlette

from a2a_client import call_skill, connect, send
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
    HostConfig,
    Remote,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI
from local_server import AppFactory, ServeLocally

type Next = Remote[Literal["next"]]

CARD_PATH = "/.well-known/agent-card.json"
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TRACESTATE = "congo=t61rcWkgMzE,rojo=00f067aa0ba902b7"


class Fixed(AgentRegistry):
    """Lists one agent, ``next``, at a fixed card URL."""

    def __init__(self, card_url: str) -> None:
        self.card_url = card_url

    async def get(self, agent_id: str) -> AgentReference:
        return AgentReference(agent_id, self.card_url)

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [AgentReference("next", self.card_url)]


class TextRouter(SkillRouter):
    """Routes any text to the agent's first skill as its ``text`` argument."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall(skills[0].id, {"text": get_message_text(conversation[-1])})


class Relay(Agent):
    """Asks the next agent and returns its answer."""

    @skill(id="relay", description="Pass text on.", tags=["relay"])
    async def relay(self, text: str, following: Next, ctx: RunContext) -> str:
        return await following.ask(text)


@agent(name="Orchestrator", description="Starts a request.", version="0.1.0")
class Orchestrator(Relay):
    """The first agent of a request."""


@agent(name="Specialist", description="Passes work on.", version="0.1.0")
class Specialist(Relay):
    """The second agent of a request."""


@agent(name="SubSpecialist", description="Does the work.", version="0.1.0")
class SubSpecialist(Agent):
    """The last agent of a request."""

    @skill(id="handle", description="Handle a request.", tags=["handle"])
    async def handle(self, text: str, ctx: RunContext) -> str:
        return f"Handled {text}"


def directory_for(base_url: str) -> AgentDirectory:
    """A directory whose ``next`` is the agent served at ``base_url``."""
    return AgentDirectory(sources=[Fixed(base_url + CARD_PATH)])


def serve_agent(
    agent_instance: Agent,
    directory: AgentDirectory | None = None,
    *,
    auth: Authenticator | None = None,
) -> AppFactory:
    """Serve ``agent_instance``, anonymously unless ``auth`` is given."""

    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=agent_instance,
            host=HostConfig(public_url=public_url, auth=auth or AllowAnonymous()),
            router=TextRouter(),
            directory=directory,
        )

    return make_app


# A peer built with the official SDK only, as another framework would.


class PlainExecutor(AgentExecutor):
    """Answers text, recording each request's extensions, metadata and headers."""

    def __init__(self) -> None:
        self.extensions: list[set[str]] = []
        self.metadata: list[dict[str, object]] = []
        self.headers: list[dict[str, str]] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None and context.task_id and context.context_id
        self.extensions.append(set(context.requested_extensions))
        self.metadata.append(dict(message.metadata.items()))
        # The SDK stores the request's headers, with lowercase names.
        self.headers.append(context.call_context.state["headers"])
        await event_queue.enqueue_event(
            new_task(context.task_id, context.context_id, TaskState.TASK_STATE_WORKING)
        )
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        await task.add_artifact([new_text_part("plain answer")])
        await task.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError


def plain_peer(executor: PlainExecutor) -> AppFactory:
    def make_app(public_url: str) -> Starlette:
        card = AgentCard(
            name="Plain",
            description="A non-AgentFrame agent.",
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


def started_runs(caplog: pytest.LogCaptureFixture) -> dict[str, logging.LogRecord]:
    """Each agent's "Run started" record, by agent name."""
    return {
        r.__dict__["agent"]: r
        for r in caplog.records
        if r.getMessage().startswith("Run started")
    }


@pytest.fixture(autouse=True)
def run_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")


# Correlation IDs


async def test_three_agents_log_the_same_root_invocation_id(
    serve: ServeLocally, caplog: pytest.LogCaptureFixture
) -> None:
    async with (
        serve(serve_agent(SubSpecialist())) as sub_url,
        directory_for(sub_url) as to_sub,
        serve(serve_agent(Specialist(), to_sub)) as specialist_url,
        directory_for(specialist_url) as to_specialist,
        serve(serve_agent(Orchestrator(), to_specialist)) as orchestrator_url,
        await connect(orchestrator_url) as client,
    ):
        task = await call_skill(client, "relay", {"text": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_message_text(task.artifacts[0]) == "Handled tides"
    root = task.metadata["invocation_id"]
    runs = started_runs(caplog)
    assert runs.keys() == {"Orchestrator", "Specialist", "SubSpecialist"}
    for record in runs.values():
        assert record.__dict__["root_invocation_id"] == root
    orchestrator = runs["Orchestrator"].__dict__
    specialist = runs["Specialist"].__dict__
    sub = runs["SubSpecialist"].__dict__
    assert orchestrator["invocation_id"] == root
    assert orchestrator["parent_invocation_id"] is None
    assert specialist["parent_invocation_id"] == root
    assert sub["parent_invocation_id"] == specialist["invocation_id"]
    # Every record of every run, not just the first, carries the root.
    for record in caplog.records:
        if record.getMessage().startswith("Run "):
            assert record.__dict__["root_invocation_id"] == root


async def test_an_invalid_incoming_root_starts_a_new_root_downstream(
    serve: ServeLocally, caplog: pytest.LogCaptureFixture
) -> None:
    forged = "not-a-uuid\nCRITICAL forged"
    async with (
        serve(serve_agent(SubSpecialist())) as sub_url,
        directory_for(sub_url) as to_sub,
        serve(serve_agent(Orchestrator(), to_sub)) as orchestrator_url,
        await connect(orchestrator_url) as client,
    ):
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part("tides")],
            metadata={"agentframe.root_invocation_id": forged},
        )
        task = await send(client, message, skill_call=True)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    root = task.metadata["invocation_id"]
    runs = started_runs(caplog)
    assert runs["Orchestrator"].__dict__["root_invocation_id"] == root
    assert runs["SubSpecialist"].__dict__["root_invocation_id"] == root
    assert forged not in caplog.text
    assert dict(task.metadata).keys() == {"invocation_id", "usage"}


# W3C trace context


async def test_a_valid_traceparent_reaches_every_following_hop_unchanged(
    serve: ServeLocally,
) -> None:
    plain = PlainExecutor()
    async with (
        serve(plain_peer(plain)) as plain_url,
        directory_for(plain_url) as to_plain,
        serve(serve_agent(Specialist(), to_plain)) as specialist_url,
        directory_for(specialist_url) as to_specialist,
        serve(serve_agent(Orchestrator(), to_specialist)) as orchestrator_url,
        await connect(orchestrator_url) as client,
    ):
        headers = {"traceparent": TRACEPARENT, "tracestate": TRACESTATE}
        task = await call_skill(client, "relay", {"text": "tides"}, headers=headers)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [received] = plain.headers
    assert received["traceparent"] == TRACEPARENT
    assert received["tracestate"] == TRACESTATE
    # A peer that lists no context fields gets plain A2A otherwise.
    assert plain.extensions == [set()]
    assert plain.metadata == [{}]


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"traceparent": "00-not-a-trace-01"}, id="invalid"),
        pytest.param(
            {"traceparent": f"00-{'0' * 32}-b7ad6b7169203331-01"}, id="zero-trace-id"
        ),
        pytest.param({"tracestate": TRACESTATE}, id="tracestate-only"),
    ],
)
async def test_an_invalid_traceparent_is_dropped_with_its_tracestate(
    serve: ServeLocally, headers: dict[str, str]
) -> None:
    plain = PlainExecutor()
    async with (
        serve(plain_peer(plain)) as plain_url,
        directory_for(plain_url) as to_plain,
        serve(serve_agent(Orchestrator(), to_plain)) as orchestrator_url,
        await connect(orchestrator_url) as client,
    ):
        headers = {"tracestate": TRACESTATE, **headers}
        task = await call_skill(client, "relay", {"text": "tides"}, headers=headers)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [received] = plain.headers
    assert "traceparent" not in received
    assert "tracestate" not in received


async def test_an_invalid_tracestate_is_dropped_but_the_traceparent_forwarded(
    serve: ServeLocally,
) -> None:
    plain = PlainExecutor()
    async with (
        serve(plain_peer(plain)) as plain_url,
        directory_for(plain_url) as to_plain,
        serve(serve_agent(Orchestrator(), to_plain)) as orchestrator_url,
        await connect(orchestrator_url) as client,
    ):
        headers = {"traceparent": TRACEPARENT, "tracestate": "Bad Key=1"}
        task = await call_skill(client, "relay", {"text": "tides"}, headers=headers)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [received] = plain.headers
    assert received["traceparent"] == TRACEPARENT
    assert "tracestate" not in received


# Correlation is not identity


ALICE_KEY, BOB_KEY = "alice-key", "bob-key"


KEYS = APIKeyAuth(keys={ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob")})


async def test_correlation_and_trace_context_do_not_authenticate(
    serve: ServeLocally,
) -> None:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": "tides"}],
        "metadata": {
            "agentframe.root_invocation_id": str(uuid.uuid4()),
            "agentframe.parent_invocation_id": str(uuid.uuid4()),
        },
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    headers = {
        "A2A-Version": "1.0",
        "A2A-Extensions": SKILL_CALL_EXTENSION_URI,
        "traceparent": TRACEPARENT,
    }
    async with (
        serve(serve_agent(SubSpecialist(), auth=KEYS)) as url,
        httpx.AsyncClient(base_url=url) as http,
    ):
        response = await http.post("/a2a", json=body, headers=headers)

    assert response.status_code == 401


async def test_another_runs_root_grants_no_access_to_its_tasks(
    serve: ServeLocally,
) -> None:
    async with (
        serve(serve_agent(SubSpecialist(), auth=KEYS)) as url,
        await connect(url, ALICE_KEY) as alice,
        await connect(url, BOB_KEY) as bob,
    ):
        alices = await call_skill(alice, "handle", {"text": "tides"})
        root = alices.metadata["invocation_id"]
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part("tides")],
            metadata={
                "agentframe.root_invocation_id": root,
                "agentframe.parent_invocation_id": root,
            },
        )
        bobs = await send(
            bob, message, skill_call=True, headers={"traceparent": TRACEPARENT}
        )

        assert bobs.status.state == TaskState.TASK_STATE_COMPLETED
        with pytest.raises(TaskNotFoundError):
            await bob.get_task(GetTaskRequest(id=alices.id))
