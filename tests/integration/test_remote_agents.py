"""An orchestrator asks and calls a specialist over real A2A.

``Remote.ask`` sends free text; ``Remote.call`` runs a skill by ID through the
skill-call extension and validates its typed result.

Both agents are ``AgentApp``s served over localhost HTTP. The orchestrator
finds the specialist through an ``AgentDirectory`` and is itself called with
the official A2A SDK client.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Literal

import pytest
from a2a.client import Client
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import CancelTaskRequest, GetTaskRequest, Message, TaskState
from pydantic import BaseModel

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AllowAnonymous,
    Clarification,
    ClarificationNeededError,
    FileAgentRegistry,
    HostConfig,
    OutputValidationError,
    Remote,
    RemoteAgentError,
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
from agentframe.discovery.remote import bind_remote
from local_server import ServeLocally, lifespan_startup

type Research = Remote[Literal["research-agent"]]


class Topic(BaseModel):
    name: str


class Source(BaseModel):
    title: str
    url: str


# The specialist: answers free text, asks back, fails or waits on request.


@agent(name="Research", description="Finds things out.", version="0.1.0")
class Specialist(Agent):
    def __init__(self) -> None:
        super().__init__()
        self.slow_started = asyncio.Event()
        self.slow_cancelled = asyncio.Event()

    @skill(id="research", description="Research a topic.", tags=["research"])
    async def research(self, topic: str, ctx: RunContext) -> str:
        if topic == "fail":
            raise RuntimeError("secret remote detail")
        if topic == "slow":
            self.slow_started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.slow_cancelled.set()
                raise
        return f"Findings on {topic}"

    @skill(id="find_sources", description="Find sources.", tags=["research"])
    async def find_sources(self, topic: Topic, ctx: RunContext) -> list[Source]:
        return [Source(title=f"About {topic.name}", url="https://example.com/")]


class TopicRouter(SkillRouter):
    """Routes any text to ``research``, but asks back for "unclear"."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        text = get_message_text(conversation[-1])
        if text == "unclear":
            return Clarification("Which topic do you mean?")
        return SkillCall("research", {"topic": text})


# The orchestrator: asks the specialist, or calls its skill.


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        return await research.ask(goal)

    @skill(id="sources", description="Find sources for a goal.", tags=["planning"])
    async def sources(
        self, goal: str, research: Research, ctx: RunContext
    ) -> list[Source]:
        topic = Topic(name=goal)
        return await research.call("find_sources", topic=topic, output=list[Source])


@pytest.fixture
def specialist() -> Specialist:
    return Specialist()


@pytest.fixture
async def directory(
    serve: ServeLocally, specialist: Specialist, tmp_path: Path
) -> AsyncIterator[AgentDirectory]:
    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=specialist,
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            router=TopicRouter(),
        )

    async with serve(app) as url:
        agents = tmp_path / "agents.yaml"
        card_url = f"{url}/.well-known/agent-card.json"
        agents.write_text(f"- name: research-agent\n  url: {card_url}\n")
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as found:
            yield found


@pytest.fixture
async def planner(
    serve: ServeLocally, directory: AgentDirectory
) -> AsyncIterator[Client]:
    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Planner(),
            host=HostConfig(public_url=public_url, auth=AllowAnonymous()),
            directory=directory,
        )

    async with serve(app) as url, await connect(url) as client:
        yield client


async def test_the_orchestrator_returns_the_specialists_answer(
    planner: Client,
) -> None:
    task = await call_skill(planner, "plan", {"goal": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Findings on tides"


async def test_a_local_run_asks_the_specialist(directory: AgentDirectory) -> None:
    runtime = Runtime(directory=directory)

    answer = await runtime.invoke(Planner(), skill="plan", arguments={"goal": "moon"})

    assert answer == "Findings on moon"


async def test_a_question_raises_clarification_needed(
    directory: AgentDirectory,
) -> None:
    runtime = Runtime(directory=directory)

    with pytest.raises(ClarificationNeededError) as raised:
        await runtime.invoke(Planner(), skill="plan", arguments={"goal": "unclear"})

    error = raised.value
    assert error.question == "Which topic do you mean?"
    assert error.task_id and error.context_id and error.invocation_id
    assert "Which topic" not in error.message


async def test_a_failed_remote_task_raises_a_safe_remote_agent_error(
    directory: AgentDirectory,
) -> None:
    runtime = Runtime(directory=directory)

    with pytest.raises(RemoteAgentError) as raised:
        await runtime.invoke(Planner(), skill="plan", arguments={"goal": "fail"})

    error = raised.value
    assert error.task_id
    assert error.task_state == "TASK_STATE_FAILED"
    assert error.message == "Agent 'research-agent' did not complete its task"


async def test_a_slow_peer_is_cut_off_at_the_deadline_and_cancelled(
    directory: AgentDirectory, specialist: Specialist
) -> None:
    runtime = Runtime(directory=directory)
    options = RunOptions(limits=RunLimits(timeout_seconds=1))

    with pytest.raises(TimeoutLimitExceededError):
        await runtime.invoke(
            Planner(), skill="plan", arguments={"goal": "slow"}, options=options
        )

    await asyncio.wait_for(specialist.slow_cancelled.wait(), timeout=5)


async def test_an_unanswered_question_fails_the_hosted_orchestrator_task(
    planner: Client,
) -> None:
    task = await call_skill(planner, "plan", {"goal": "unclear"})

    assert task.status.state == TaskState.TASK_STATE_FAILED
    text = get_message_text(task.status.message)
    assert text == "Agent 'research-agent' needs more input before it can answer"


async def test_cancelling_the_orchestrator_task_cancels_the_peer_task(
    planner: Client, specialist: Specialist
) -> None:
    task = await call_skill(planner, "plan", {"goal": "slow"}, return_immediately=True)
    await asyncio.wait_for(specialist.slow_started.wait(), timeout=5)

    await planner.cancel_task(CancelTaskRequest(id=task.id))

    await asyncio.wait_for(specialist.slow_cancelled.wait(), timeout=5)
    stored = await planner.get_task(GetTaskRequest(id=task.id))
    assert stored.status.state == TaskState.TASK_STATE_CANCELED


async def test_startup_fails_for_an_unknown_remote_name(tmp_path: Path) -> None:
    agents = tmp_path / "agents.yaml"
    agents.write_text("- name: other-agent\n  url: https://other.example.com/\n")
    async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as directory:
        app = AgentApp(
            agent=Planner(),
            host=HostConfig(public_url="http://127.0.0.1:1", auth=AllowAnonymous()),
            directory=directory,
        )

        reply = await lifespan_startup(app)

    assert reply["type"] == "lifespan.startup.failed"
    assert "research-agent" in reply["message"]


# Calling a skill


async def bound(directory: AgentDirectory) -> Research:
    """A binding like the one a run gets, for calls no Planner skill makes."""
    deadline = asyncio.get_running_loop().time() + 10
    return await bind_remote(
        directory,
        "research-agent",
        deadline=deadline,
        invocation_id="run-1",
        principal=None,
        identity=None,
    )


async def test_the_orchestrator_returns_the_specialists_typed_result(
    planner: Client,
) -> None:
    task = await call_skill(planner, "sources", {"goal": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(task.artifacts[0].parts)
    assert data == {"value": [{"title": "About tides", "url": "https://example.com/"}]}


async def test_a_local_run_calls_the_specialist(directory: AgentDirectory) -> None:
    runtime = Runtime(directory=directory)

    found = await runtime.invoke(Planner(), skill="sources", arguments={"goal": "moon"})

    assert found == [Source(title="About moon", url="https://example.com/")]


async def test_a_skill_not_on_the_card_is_refused_with_no_fallback(
    directory: AgentDirectory,
) -> None:
    research = await bound(directory)

    with pytest.raises(RemoteAgentError, match="no skill 'summarize'"):
        await research.call("summarize", text="tides")


async def test_invalid_arguments_are_rejected_by_the_specialist(
    directory: AgentDirectory,
) -> None:
    research = await bound(directory)

    with pytest.raises(RemoteAgentError) as raised:
        await research.call("find_sources", topic={"secret": "remote detail"})

    assert raised.value.task_state == "TASK_STATE_REJECTED"
    assert "secret" not in raised.value.message


async def test_a_result_of_the_wrong_type_raises_output_validation_error(
    directory: AgentDirectory,
) -> None:
    research = await bound(directory)

    with pytest.raises(OutputValidationError):
        await research.call("find_sources", topic={"name": "x"}, output=list[int])
