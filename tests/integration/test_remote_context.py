"""Remote calls carry the caller's remaining time and depth over real A2A.

Every agent is served over localhost HTTP. AgentFrame peers are ``AgentApp``s;
the non-AgentFrame peers are bare official-SDK servers, one without the
AgentFrame extension and one with the extension as AgentFrame 0.1.0 declared
it (no context fields).
"""

import asyncio
import uuid
from collections.abc import Sequence
from typing import Literal

import pytest
from a2a.helpers import get_message_text, new_data_part, new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    Message,
    Role,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
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
    HostConfig,
    LimitExceededError,
    Remote,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    Runtime,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    TimeoutLimitExceededError,
    agent,
    skill,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI
from local_server import AppFactory, ServeLocally

type Peer = Remote[Literal["peer"]]

CARD_PATH = "/.well-known/agent-card.json"


class Switchboard(AgentRegistry):
    """Maps names to card URLs; tests repoint them once every agent is up."""

    def __init__(self, **card_urls: str) -> None:
        self.card_urls = card_urls

    async def get(self, agent_id: str) -> AgentReference:
        return AgentReference(agent_id, self.card_urls[agent_id])

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        names = query.names or set(self.card_urls)
        return [AgentReference(n, self.card_urls[n]) for n in names]


class TextRouter(SkillRouter):
    """Routes any text to the agent's first skill as its ``text`` argument."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall(skills[0].id, {"text": get_message_text(conversation[-1])})


@agent(name="Relay", description="Passes text on to its peer.", version="0.1.0")
class Relay(Agent):
    """Asks its peer, recording every run's depth and depth failure."""

    def __init__(self) -> None:
        self.depths: list[int] = []
        self.depth_errors: list[str] = []

    @skill(id="relay", description="Pass text on.", tags=["relay"])
    async def relay(self, text: str, peer: Peer, ctx: RunContext) -> str:
        self.depths.append(ctx.hops)
        try:
            return await peer.ask(text)
        except LimitExceededError as error:
            self.depth_errors.append(error.message)
            raise


@agent(name="Recorder", description="Records what callers send.", version="0.1.0")
class Recorder(Agent):
    """Records each run's depth and time left; waits on request."""

    def __init__(self) -> None:
        self.runs: list[tuple[int, float]] = []

    @skill(id="record", description="Record a request.", tags=["record"])
    async def record(self, text: str, ctx: RunContext) -> str:
        self.runs.append((ctx.hops, ctx.deadline - asyncio.get_running_loop().time()))
        if text == "slow":
            await asyncio.sleep(60)
        return f"Recorded {text}"


def serve_agent(
    agent_instance: Agent, directory: AgentDirectory | None = None
) -> AppFactory:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=agent_instance,
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=TextRouter(),
            directory=directory,
        )

    return make_app


# A peer built with the official SDK only, as another framework would.


class PlainExecutor(AgentExecutor):
    """Answers text, recording each request's extensions and metadata."""

    def __init__(self) -> None:
        self.requests: list[tuple[set[str], dict[str, object]]] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None and context.task_id and context.context_id
        self.requests.append(
            (set(context.requested_extensions), dict(message.metadata.items()))
        )
        await event_queue.enqueue_event(
            new_task(context.task_id, context.context_id, TaskState.TASK_STATE_WORKING)
        )
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        if get_message_text(message) == "slow":
            await asyncio.sleep(60)
        await task.add_artifact([new_text_part("plain answer")])
        await task.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        assert context.task_id and context.context_id
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()


def plain_peer(
    executor: PlainExecutor, extensions: Sequence[AgentExtension] = ()
) -> AppFactory:
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
            capabilities=AgentCapabilities(extensions=extensions),
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


AGENTFRAME_0_1_0_EXTENSION = AgentExtension(
    uri=SKILL_CALL_EXTENSION_URI, params={"skills": {"echo": {"type": "object"}}}
)


def directory_for(base_url: str) -> AgentDirectory:
    """A directory whose ``peer`` is the agent served at ``base_url``."""
    return AgentDirectory(sources=[Switchboard(peer=base_url + CARD_PATH)])


async def ask_peer(
    directory: AgentDirectory, text: str, timeout_seconds: float | None = None
) -> str:
    """Run ``Relay`` locally, a root request asking ``peer``."""
    options = RunOptions(limits=RunLimits(timeout_seconds=timeout_seconds))
    answer: str = await Runtime(directory=directory).invoke(
        Relay(), skill="relay", arguments={"text": text}, options=options
    )
    return answer


# Deadlines


async def test_ask_sends_the_remaining_time_and_depth_to_an_agentframe_peer(
    serve: ServeLocally,
) -> None:
    recorder = Recorder()
    async with serve(serve_agent(recorder)) as url, directory_for(url) as directory:
        answer = await ask_peer(directory, "tides", timeout_seconds=3)

    assert answer == "Recorded tides"
    [(hops, time_left)] = recorder.runs
    assert hops == 1
    # Its own timeout is 120 seconds; the caller had about 3 left.
    assert 2 < time_left < 3


async def test_a_receiver_called_with_2_seconds_left_times_out_after_about_2(
    serve: ServeLocally,
) -> None:
    async with serve(serve_agent(Recorder())) as url, await connect(url) as client:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_data_part({"text": "slow"})],
            metadata={"agentframe.skill": "record", "agentframe.deadline_ms": 2000},
        )
        started = asyncio.get_running_loop().time()
        task = await send(client, message, skill_call=True)
        elapsed = asyncio.get_running_loop().time() - started

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert "timeout" in get_message_text(task.status.message)
    assert 1.9 < elapsed < 3


async def test_a_longer_incoming_deadline_does_not_extend_the_receivers_timeout(
    serve: ServeLocally,
) -> None:
    recorder = Recorder()
    async with serve(serve_agent(recorder)) as url, await connect(url) as client:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_data_part({"text": "quick"})],
            metadata={"agentframe.skill": "record", "agentframe.deadline_ms": 600_000},
        )
        task = await send(client, message, skill_call=True)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [(_, time_left)] = recorder.runs
    assert 119 < time_left <= 120


# Depth


async def test_a_recursive_loop_fails_at_hop_6_at_the_caller(
    serve: ServeLocally,
) -> None:
    a, b = Relay(), Relay()
    # Startup checks each agent's peer, so both start pointing at a plain
    # agent and are pointed at each other once both are up.
    async with serve(plain_peer(PlainExecutor())) as plain:
        a_peers = Switchboard(peer=plain + CARD_PATH)
        b_peers = Switchboard(peer=plain + CARD_PATH)
        async with (
            AgentDirectory(sources=[a_peers]) as a_directory,
            AgentDirectory(sources=[b_peers]) as b_directory,
            serve(serve_agent(a, a_directory)) as a_url,
            serve(serve_agent(b, b_directory)) as b_url,
            await connect(a_url) as client,
        ):
            a_peers.card_urls["peer"] = b_url + CARD_PATH
            b_peers.card_urls["peer"] = a_url + CARD_PATH

            task = await call_skill(client, "relay", {"text": "loop"})

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert a.depths == [0, 2, 4]
    assert b.depths == [1, 3, 5]
    # B, at depth 5, refused to make hop 6 without sending it.
    assert b.depth_errors == ["Maximum remote call depth of 5 exceeded"]


async def test_a_request_at_hop_6_is_rejected_by_the_receiver(
    serve: ServeLocally,
) -> None:
    recorder = Recorder()
    async with serve(serve_agent(recorder)) as url, await connect(url) as client:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part("deep")],
            metadata={"agentframe.hops": 6},
        )
        task = await send(client, message, skill_call=True)

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    text = get_message_text(task.status.message)
    assert text == "Maximum remote call depth of 5 exceeded"
    assert recorder.runs == []


# Peers that advertise no context fields


@pytest.mark.parametrize(
    "extensions",
    [
        pytest.param([], id="non-agentframe"),
        pytest.param([AGENTFRAME_0_1_0_EXTENSION], id="agentframe-0.1.0"),
    ],
)
async def test_a_peer_without_context_fields_gets_plain_a2a(
    serve: ServeLocally, extensions: list[AgentExtension]
) -> None:
    executor = PlainExecutor()
    async with (
        serve(plain_peer(executor, extensions)) as url,
        directory_for(url) as directory,
    ):
        answer = await ask_peer(directory, "tides")

    assert answer == "plain answer"
    [(activated, metadata)] = executor.requests
    assert activated == set()
    assert metadata == {}


async def test_a_non_agentframe_peer_still_stops_at_the_local_deadline(
    serve: ServeLocally,
) -> None:
    async with (
        serve(plain_peer(PlainExecutor())) as url,
        directory_for(url) as directory,
    ):
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutLimitExceededError):
            await ask_peer(directory, "slow", timeout_seconds=1)
        elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 3
