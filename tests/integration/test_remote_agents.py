"""An orchestrator asks a specialist over real A2A, through ``Remote.ask``.

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
from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import CancelTaskRequest, GetTaskRequest, Message, TaskState

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
from local_server import ServeLocally, lifespan_startup

type Research = Remote[Literal["research-agent"]]


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


# The orchestrator: one skill that asks the specialist.


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        return await research.ask(goal)


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
