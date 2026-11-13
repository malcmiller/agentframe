"""Remote calls share the caller's token budget and charge back what peers used.

Every agent is served over localhost HTTP. AgentFrame peers are ``AgentApp``s
with scripted models; the other peers are bare official-SDK servers, one
without the AgentFrame extension and one declaring it as #89 did (deadline
and depth only), which refuses any other ``agentframe.`` metadata.
"""

import asyncio
import uuid
from collections.abc import Sequence
from typing import Any, Literal

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
    AgentSkill,
    Message,
    Role,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol
from starlette.applications import Starlette

from a2a_client import connect, send
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    HostConfig,
    ModelResponse,
    Remote,
    RunContext,
    RunLimits,
    RunOptions,
    RunResult,
    Runtime,
    TokenLimitExceededError,
    Usage,
    agent,
    skill,
)
from agentframe.hosting.skill_call import SKILL_CALL_EXTENSION_URI
from agentframe.runtime.usage import FinishReason
from agentframe.testing import ScriptedModel
from local_server import AppFactory, ServeLocally

type Peer = Remote[Literal["peer"]]

CARD_PATH = "/.well-known/agent-card.json"


class OnePeer(AgentRegistry):
    """Lists one agent, ``peer``, at a card URL."""

    def __init__(self, card_url: str) -> None:
        self.card_url = card_url

    async def get(self, agent_id: str) -> AgentReference:
        return AgentReference(agent_id, self.card_url)

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [AgentReference("peer", self.card_url)]


def directory_for(base_url: str) -> AgentDirectory:
    """A directory whose ``peer`` is the agent served at ``base_url``."""
    return AgentDirectory(sources=[OnePeer(base_url + CARD_PATH)])


def answers(*replies: tuple[str, int]) -> ScriptedModel:
    """A model answering each ``(text, total_tokens)`` with reported usage."""
    return ScriptedModel(
        ModelResponse(
            text=text,
            usage=Usage(
                input_tokens=total - 20,
                output_tokens=20,
                total_tokens=total,
                estimated=False,
            ),
            finish_reason=FinishReason.COMPLETED,
        )
        for text, total in replies
    )


@agent(name="Specialist", description="Answers questions.", version="0.2.0")
class Specialist(Agent):
    """Answers with its model; its own token limit is the default 100000."""

    @skill(id="answer", description="Answer a question.", tags=["answer"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        return await ctx.generate(question)


@agent(name="Middle", description="Thinks, then asks its peer.", version="0.2.0")
class Middle(Agent):
    """Uses its model once, then calls its peer's ``answer`` skill."""

    @skill(id="answer", description="Answer a question.", tags=["answer"])
    async def answer(self, question: str, peer: Peer, ctx: RunContext) -> str:
        thought = await ctx.generate(question)
        found: str = await peer.call("answer", question=thought)
        return found


@agent(name="Orchestrator", description="Delegates work.", version="0.2.0")
class Orchestrator(Agent):
    """Delegates to its peer, alone or fanned out."""

    @skill(id="delegate", description="Think, then delegate.", tags=["delegate"])
    async def delegate(self, question: str, peer: Peer, ctx: RunContext) -> str:
        thought = await ctx.generate(question)
        found: str = await peer.call("answer", question=thought)
        return found

    @skill(id="fan_out", description="Ask twice at once.", tags=["fan-out"])
    async def fan_out(
        self, ceiling: int | None, peer: Peer, ctx: RunContext
    ) -> list[str]:
        async def one(question: str) -> str:
            try:
                found: str = await peer.call(
                    "answer", question=question, max_total_tokens=ceiling
                )
            except TokenLimitExceededError as error:
                return error.message
            return found

        async with asyncio.TaskGroup() as group:
            first = group.create_task(one("tides"))
            second = group.create_task(one("winds"))
        return [first.result(), second.result()]

    @skill(id="ask_twice", description="Ask in plain text twice.", tags=["ask"])
    async def ask_twice(self, peer: Peer, ctx: RunContext) -> list[str]:
        async with asyncio.TaskGroup() as group:
            first = group.create_task(peer.ask("tides"))
            second = group.create_task(peer.ask("winds"))
        return [first.result(), second.result()]


def serve_agent(
    agent_instance: Agent, directory: AgentDirectory | None = None
) -> AppFactory:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=agent_instance,
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            directory=directory,
        )

    return make_app


async def run_orchestrator(
    directory: AgentDirectory,
    skill_id: str,
    arguments: dict[str, object],
    model: ScriptedModel | None = None,
    max_total_tokens: int | None = None,
) -> RunResult[Any]:
    """Run ``Orchestrator`` locally, a root request calling ``peer``."""
    options = RunOptions(limits=RunLimits(max_total_tokens=max_total_tokens))
    return await Runtime(directory=directory).run(
        Orchestrator(model=model),
        skill=skill_id,
        arguments=arguments,
        options=options,
    )


# The receiver's ceiling


async def test_an_incoming_ceiling_of_1000_stops_a_specialist_allowed_100000(
    serve: ServeLocally,
) -> None:
    model = answers(("never sent", 300))
    async with (
        serve(serve_agent(Specialist(model=model))) as url,
        await connect(url) as client,
    ):
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_data_part({"question": "tides"})],
            metadata={
                "agentframe.skill": "answer",
                "agentframe.max_total_tokens": 1000,
            },
        )
        task = await send(client, message, skill_call=True)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    # The model call reserves its 4096 output tokens, more than 1000 allow.
    assert "only 1000 of the run's 1000 remain" in get_message_text(task.status.message)
    assert model.requests == []


# Charging usage back


async def test_usage_two_hops_down_is_charged_to_the_orchestrator_once(
    serve: ServeLocally,
) -> None:
    middle = Middle(model=answers(("Think about tides", 200)))
    specialist = Specialist(model=answers(("High tide is at noon", 300)))
    async with (
        serve(serve_agent(specialist)) as specialist_url,
        directory_for(specialist_url) as middle_directory,
        serve(serve_agent(middle, middle_directory)) as middle_url,
        directory_for(middle_url) as directory,
    ):
        result = await run_orchestrator(
            directory,
            "delegate",
            {"question": "When is high tide?"},
            model=answers(("Ask about tides", 100)),
        )

    assert result.value == "High tide is at noon"
    assert result.usage == Usage(
        input_tokens=540, output_tokens=60, total_tokens=600, estimated=False
    )
    # Only the orchestrator's own model calls.
    assert result.model_calls == 1


# Fanning out


async def test_explicit_ceilings_over_the_budget_fail_the_second_before_sending(
    serve: ServeLocally,
) -> None:
    model = answers(("High tide", 300), ("unused", 300))
    async with (
        serve(serve_agent(Specialist(model=model))) as url,
        directory_for(url) as directory,
    ):
        result = await run_orchestrator(
            directory, "fan_out", {"ceiling": 6_000}, max_total_tokens=10_000
        )

    first, second = result.value
    assert first == "High tide"
    assert second == (
        "Agent 'peer': the call needs up to 6000 tokens, but only 4000 of the "
        "run's 10000 remain"
    )
    assert len(model.requests) == 1
    assert result.usage.total_tokens == 300


async def test_the_default_ceiling_fails_a_second_concurrent_call_with_advice(
    serve: ServeLocally,
) -> None:
    model = answers(("High tide", 300), ("unused", 300))
    async with (
        serve(serve_agent(Specialist(model=model))) as url,
        directory_for(url) as directory,
    ):
        result = await run_orchestrator(directory, "fan_out", {"ceiling": None})

    first, second = result.value
    assert first == "High tide"
    assert "pass max_total_tokens to each call" in second
    assert len(model.requests) == 1


# Peers that do not take a ceiling


class PlainExecutor(AgentExecutor):
    """Answers text and records each message's metadata.

    Like an AgentFrame #89 receiver, it fails any request carrying
    ``agentframe.`` metadata it does not know.
    """

    KNOWN = frozenset(["agentframe.skill", "agentframe.deadline_ms", "agentframe.hops"])

    def __init__(self) -> None:
        self.metadata: list[dict[str, object]] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None and context.task_id and context.context_id
        metadata = dict(message.metadata.items())
        self.metadata.append(metadata)
        await event_queue.enqueue_event(
            new_task(context.task_id, context.context_id, TaskState.TASK_STATE_WORKING)
        )
        task = TaskUpdater(event_queue, context.task_id, context.context_id)
        unknown = [k for k in metadata if k.startswith("agentframe.")]
        if set(unknown) - self.KNOWN:
            await task.failed()
            return
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
            description="Not built with AgentFrame.",
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
            skills=[AgentSkill(id="answer", name="Answer", description="Answers.")],
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


async def test_a_non_agentframe_peer_holds_nothing_and_leaves_usage_unknown(
    serve: ServeLocally,
) -> None:
    executor = PlainExecutor()
    async with (
        serve(plain_peer(executor)) as url,
        directory_for(url) as directory,
    ):
        # Two concurrent default calls: neither holds the budget.
        result = await run_orchestrator(directory, "ask_twice", {})

    assert result.value == ["plain answer", "plain answer"]
    assert executor.metadata == [{}, {}]
    assert result.usage.total_tokens is None
    assert result.usage.input_tokens is None


async def test_an_agentframe_peer_from_89_never_gets_the_ceiling_field(
    serve: ServeLocally,
) -> None:
    executor = PlainExecutor()
    extension = AgentExtension(
        uri=SKILL_CALL_EXTENSION_URI,
        params={
            "skills": {"answer": {"type": "object"}},
            "context": ["deadline_ms", "hops"],
        },
    )
    async with (
        serve(plain_peer(executor, [extension])) as url,
        directory_for(url) as directory,
    ):
        result = await run_orchestrator(
            directory,
            "delegate",
            {"question": "tides"},
            model=answers(("Ask about tides", 100)),
        )

    assert result.value == "plain answer"
    [metadata] = executor.metadata
    assert set(metadata) == {
        "agentframe.skill",
        "agentframe.deadline_ms",
        "agentframe.hops",
    }
    assert result.usage.total_tokens is None
