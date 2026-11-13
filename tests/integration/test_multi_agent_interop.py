"""AgentFrame works in a mixed A2A ecosystem, both ways (#94).

Three agents run over localhost HTTP:

- a plain peer built only on the official A2A SDK (``a2a_peer``), with none of
  AgentFrame's extensions, standing in for another framework's agent;
- an AgentFrame specialist;
- an AgentFrame orchestrator whose skills ``ask`` the plain peer and ``call``
  the specialist.

The official SDK client, again with no AgentFrame code, calls the orchestrator.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Literal

import pytest
from a2a.client import Client
from a2a.helpers import (
    get_artifact_text,
    get_data_parts,
    get_message_text,
    get_text_parts,
)
from a2a.types import Message, TaskState
from pydantic import BaseModel

from a2a_client import call_skill, connect, say
from a2a_peer import PEER_QUESTION, PEER_SECRET, PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AllowAnonymous,
    ClarificationNeededError,
    FileAgentRegistry,
    HostConfig,
    Remote,
    RemoteAgentError,
    RoutingContext,
    RunContext,
    RunLimits,
    RunOptions,
    RunResult,
    Runtime,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    TimeoutLimitExceededError,
    agent,
    skill,
)
from local_server import ServeLocally

type PlainPeer = Remote[Literal["plain-peer"]]
type Research = Remote[Literal["research-agent"]]


class Source(BaseModel):
    title: str


class Briefing(BaseModel):
    summary: str
    sources: list[Source]


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Specialist(Agent):
    """An AgentFrame specialist that records the depth of each call."""

    def __init__(self) -> None:
        super().__init__()
        self.hops: list[int] = []

    @skill(id="find_sources", description="Find sources.", tags=["research"])
    async def find_sources(self, topic: str, ctx: RunContext) -> list[Source]:
        self.hops.append(ctx.hops)
        return [Source(title=f"About {topic}")]


@agent(name="Planner", description="Plans with other agents.", version="0.1.0")
class Planner(Agent):
    @skill(id="ask_peer", description="Ask the plain peer.", tags=["planning"])
    async def ask_peer(self, text: str, peer: PlainPeer, ctx: RunContext) -> str:
        return await peer.ask(text)

    @skill(id="call_peer", description="Call the peer's echo.", tags=["planning"])
    async def call_peer(self, text: str, peer: PlainPeer, ctx: RunContext) -> str:
        echoed: str = await peer.call("echo", text=text, output=str)
        return echoed

    @skill(id="brief", description="Brief a topic.", tags=["planning"])
    async def brief(
        self, topic: str, peer: PlainPeer, research: Research, ctx: RunContext
    ) -> Briefing:
        summary = await peer.ask(topic)
        sources = await research.call("find_sources", topic=topic, output=list[Source])
        return Briefing(summary=summary, sources=sources)


class AskPeerRouter(SkillRouter):
    """Sends any free text to ``ask_peer``."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall("ask_peer", {"text": get_message_text(conversation[-1])})


@pytest.fixture
def peer() -> PeerExecutor:
    return PeerExecutor()


@pytest.fixture
def specialist() -> Specialist:
    return Specialist()


@pytest.fixture
async def directory(
    serve: ServeLocally, peer: PeerExecutor, specialist: Specialist, tmp_path: Path
) -> AsyncIterator[AgentDirectory]:
    def specialist_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=specialist, host=host)

    async with (
        serve(a2a_peer(peer)) as peer_url,
        serve(specialist_app) as specialist_url,
    ):
        agents = tmp_path / "agents.yaml"
        agents.write_text(
            f"- name: plain-peer\n  url: {peer_url}/.well-known/agent-card.json\n"
            f"- name: research-agent\n"
            f"  url: {specialist_url}/.well-known/agent-card.json\n"
        )
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as found:
            yield found


@pytest.fixture
async def orchestrator(
    serve: ServeLocally, directory: AgentDirectory
) -> AsyncIterator[Client]:
    """The official SDK client, connected to the hosted orchestrator."""

    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Planner(),
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=AskPeerRouter(),
            directory=directory,
        )

    async with serve(app) as url, await connect(url) as client:
        yield client


async def run_planner(
    directory: AgentDirectory,
    skill_id: str,
    arguments: dict[str, object],
    options: RunOptions | None = None,
) -> RunResult[object]:
    return await Runtime(directory=directory).run(
        Planner(), skill=skill_id, arguments=arguments, options=options
    )


# AgentFrame calling a non-AgentFrame server


async def test_ask_returns_the_plain_peers_text_and_usage_stays_unknown(
    directory: AgentDirectory,
) -> None:
    result = await run_planner(directory, "ask_peer", {"text": "tides"})

    assert result.value == "Echo: tides"
    assert result.usage.total_tokens is None
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None


async def test_the_plain_peer_receives_no_agentframe_metadata_or_extension(
    directory: AgentDirectory, peer: PeerExecutor, specialist: Specialist
) -> None:
    result = await run_planner(directory, "brief", {"topic": "tides"})

    assert result.value == Briefing(
        summary="Echo: tides", sources=[Source(title="About tides")]
    )
    [request] = peer.requests
    assert request.extensions == set()
    assert request.metadata == {}
    assert get_text_parts(request.message.parts) == ["tides"]
    # In the same run, the AgentFrame specialist did get the caller context.
    assert specialist.hops == [1]


async def test_the_local_deadline_stops_a_slow_plain_peer(
    directory: AgentDirectory, peer: PeerExecutor
) -> None:
    options = RunOptions(limits=RunLimits(timeout_seconds=1))
    started = asyncio.get_running_loop().time()

    with pytest.raises(TimeoutLimitExceededError):
        await run_planner(directory, "ask_peer", {"text": "wait"}, options)

    assert asyncio.get_running_loop().time() - started < 3
    # The peer's task is cancelled too, with a standard CancelTask.
    await asyncio.wait_for(peer.cancelled.wait(), timeout=5)


async def test_call_to_the_plain_peer_fails_before_sending(
    directory: AgentDirectory, peer: PeerExecutor
) -> None:
    with pytest.raises(RemoteAgentError) as raised:
        await run_planner(directory, "call_peer", {"text": "tides"})

    assert "does not declare the skill-call extension" in raised.value.message
    # Nothing was sent, not even as free text.
    assert peer.requests == []


async def test_a_peer_question_raises_clarification_needed(
    directory: AgentDirectory,
) -> None:
    with pytest.raises(ClarificationNeededError) as raised:
        await run_planner(directory, "ask_peer", {"text": "question"})

    assert raised.value.question == PEER_QUESTION
    assert raised.value.task_id


async def test_a_peer_failure_raises_a_safe_remote_agent_error(
    directory: AgentDirectory,
) -> None:
    with pytest.raises(RemoteAgentError) as raised:
        await run_planner(directory, "ask_peer", {"text": "fail"})

    error = raised.value
    assert error.task_state == "TASK_STATE_FAILED"
    assert error.message == "Agent 'plain-peer' did not complete its task"
    assert PEER_SECRET not in str(error)


# A non-AgentFrame client calling AgentFrame


async def test_the_official_client_gets_a_standard_task_with_a_typed_result(
    orchestrator: Client,
) -> None:
    task = await call_skill(orchestrator, "brief", {"topic": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(task.artifacts[0].parts)
    assert data == {
        "value": {"summary": "Echo: tides", "sources": [{"title": "About tides"}]}
    }


async def test_the_official_client_can_send_plain_text(orchestrator: Client) -> None:
    task = await say(orchestrator, "tides")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Echo: tides"


async def test_the_official_client_sees_a_peer_failure_as_a_safe_failed_task(
    orchestrator: Client,
) -> None:
    task = await say(orchestrator, "fail")

    assert task.status.state == TaskState.TASK_STATE_FAILED
    text = get_message_text(task.status.message)
    assert text == "Agent 'plain-peer' did not complete its task"
