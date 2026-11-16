"""A hosted skill without permissions cannot let a model spend its credentials.

The orchestrator is hosted over real localhost A2A and calls the research
specialist with its own ``Service`` credentials. Without
``requires_permissions`` (or ``allow_any_caller=True``) any caller could have
its model choose those calls, so plan steps and remote tools are refused
before anything is sent. Local runs and hand-written ``Remote`` calls are the
author's own choice and stay unaffected.
"""

import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Literal

import pytest
from a2a.helpers import get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    APIKey,
    APIKeyAuth,
    APIKeyCredential,
    FinishReason,
    HostConfig,
    ModelCapabilities,
    ModelResponse,
    Plan,
    PlanStep,
    Remote,
    RunContext,
    Runtime,
    Service,
    ToolCall,
    Usage,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

CALLER_KEY = "caller-key"
PLANNER_KEY = "planner-key"
TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
TIDES = Plan(
    steps=[PlanStep(id="tides", use="research.find", arguments={"topic": "tides"})],
    result="tides",
)
REFUSED = "Skill '{}' requires no permissions"


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Research(Agent):
    """Records the topics it was asked about."""

    def __init__(self) -> None:
        super().__init__()
        self.topics: list[str] = []

    @skill(id="find", description="Find sources on a topic.", tags=["research"])
    async def find(self, topic: str, ctx: RunContext) -> str:
        self.topics.append(topic)
        return f"{topic} source"


def finds_tides() -> ScriptedModel:
    """A model that calls ``research.find`` once, then answers."""
    usage = Usage(input_tokens=1, output_tokens=1, total_tokens=2, estimated=False)
    call = ToolCall("1", "research_find", json.dumps({"topic": "tides"}))
    return ScriptedModel(
        [
            ModelResponse(
                text="",
                usage=usage,
                finish_reason=FinishReason.TOOL_CALLS,
                tool_calls=(call,),
            ),
            ModelResponse(
                text="Tides: tides source",
                usage=usage,
                finish_reason=FinishReason.COMPLETED,
            ),
        ],
        capabilities=TOOL_MODEL,
    )


@agent(name="Orchestrator", description="Delegates to research.", version="0.1.0")
class Orchestrator(Agent):
    """Uses the research agent through a plan, model tools or a direct call."""

    def __init__(self, **catalog_options: Any) -> None:
        self.scripted = finds_tides()
        super().__init__(model=self.scripted)
        self.catalog_options = catalog_options

    async def research_skills(self, ctx: RunContext) -> Any:
        return await ctx.remote_skills(agents=["research"], **self.catalog_options)

    @skill(id="plan", description="Run a fixed plan.", tags=["orchestration"])
    async def run_fixed_plan(self, ctx: RunContext) -> str:
        result = await ctx.run_plan(TIDES, await self.research_skills(ctx))
        return str(result.value)

    @skill(
        id="tools", description="Let the model call research.", tags=["orchestration"]
    )
    async def use_tools(self, ctx: RunContext) -> str:
        skills = await self.research_skills(ctx)
        return await ctx.generate("Find tides", tools=[*skills])

    @skill(
        id="protected_plan",
        description="Run a fixed plan for planners only.",
        tags=["orchestration"],
        requires_permissions=["plans:run"],
    )
    async def protected_plan(self, ctx: RunContext) -> str:
        result = await ctx.run_plan(TIDES, await self.research_skills(ctx))
        return str(result.value)


@agent(name="Direct", description="Also calls research directly.", version="0.1.0")
class DirectOrchestrator(Orchestrator):
    """Adds a hand-written call, which needs a ``remotes=`` entry at startup."""

    @skill(id="direct", description="Call research directly.", tags=["orchestration"])
    async def direct(
        self, research: Remote[Literal["research"]], ctx: RunContext
    ) -> str:
        return str(await research.call("find", topic="tides"))


class Registered(AgentRegistry):
    """Lists the research agent at one URL."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [AgentReference("research", f"{self.url}/.well-known/agent-card.json")]


ORCHESTRATOR_KEY = "orchestrator-key"
AS_ORCHESTRATOR = Service(credential=APIKeyCredential(ORCHESTRATOR_KEY))
SERVICE = {"research": AS_ORCHESTRATOR}


@pytest.fixture
def research() -> Research:
    return Research()


@pytest.fixture
def directory(
    serve: ServeLocally, research: Research
) -> Callable[[], AbstractAsyncContextManager[AgentDirectory]]:
    @asynccontextmanager
    async def serving() -> AsyncIterator[AgentDirectory]:
        def research_app(public_url: str) -> AgentApp:
            auth = APIKeyAuth(keys={ORCHESTRATOR_KEY: APIKey("orchestrator")})
            host = HostConfig(public_url=public_url, auth=auth)
            return AgentApp(agent=research, host=host)

        async with (
            serve(research_app) as url,
            AgentDirectory(sources=[Registered(url)]) as directory,
        ):
            yield directory

    return serving


@pytest.fixture
def hosted(
    serve: ServeLocally,
    directory: Callable[[], AbstractAsyncContextManager[AgentDirectory]],
) -> Callable[..., Any]:
    """Host an orchestrator; call one of its skills ``times`` with a caller's key."""

    async def call(
        orchestrator: Orchestrator,
        skill_id: str,
        key: str = CALLER_KEY,
        times: int = 1,
        **identities: Any,
    ) -> Task:
        auth = APIKeyAuth(
            keys={
                CALLER_KEY: APIKey("caller"),
                PLANNER_KEY: APIKey("planner", roles=["planner"]),
            }
        )
        async with directory() as found:

            def orchestrator_app(public_url: str) -> AgentApp:
                return AgentApp(
                    agent=orchestrator,
                    host=HostConfig(
                        public_url=public_url,
                        auth=auth,
                        role_permissions={"planner": ["plans:run"]},
                    ),
                    directory=found,
                    **(identities or {"remotes": SERVICE}),
                )

            async with (
                serve(orchestrator_app) as url,
                await connect(url, key) as client,
            ):
                for _ in range(times):
                    task = await call_skill(client, skill_id)
                return task

    return call


def refused(task: Task, skill_id: str) -> bool:
    message = get_message_text(task.status.message)
    return task.status.state == TaskState.TASK_STATE_REJECTED and message.startswith(
        REFUSED.format(skill_id)
    )


@pytest.mark.parametrize("skill_id", ["plan", "tools"])
async def test_an_unprotected_skill_is_refused_before_anything_is_sent(
    hosted: Callable[..., Any], research: Research, skill_id: str
) -> None:
    orchestrator = Orchestrator()

    task = await hosted(orchestrator, skill_id)

    assert refused(task, skill_id), task.status
    assert research.topics == []
    assert orchestrator.scripted.requests == []


@pytest.mark.parametrize("skill_id", ["plan", "tools"])
async def test_a_service_default_remote_is_guarded_too(
    hosted: Callable[..., Any], research: Research, skill_id: str
) -> None:
    orchestrator = Orchestrator()

    task = await hosted(orchestrator, skill_id, default_remote=AS_ORCHESTRATOR)

    assert refused(task, skill_id), task.status
    assert research.topics == []
    assert orchestrator.scripted.requests == []


@pytest.mark.parametrize("skill_id", ["plan", "tools"])
async def test_allow_any_caller_opts_out_of_the_guard(
    hosted: Callable[..., Any], research: Research, skill_id: str
) -> None:
    task = await hosted(Orchestrator(allow_any_caller=True), skill_id)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    assert research.topics == ["tides"]


async def test_a_skill_with_requires_permissions_runs_for_a_permitted_caller(
    hosted: Callable[..., Any], research: Research
) -> None:
    task = await hosted(Orchestrator(), "protected_plan", PLANNER_KEY)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    assert research.topics == ["tides"]


async def test_a_hand_written_remote_call_is_the_authors_choice(
    hosted: Callable[..., Any], research: Research
) -> None:
    task = await hosted(DirectOrchestrator(), "direct")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    assert research.topics == ["tides"]


@pytest.mark.parametrize("skill_id", ["plan", "tools"])
async def test_a_local_run_is_not_guarded(
    directory: Callable[[], AbstractAsyncContextManager[AgentDirectory]],
    research: Research,
    skill_id: str,
) -> None:
    async with directory() as found:
        runtime = Runtime(directory=found, remotes=SERVICE)
        await runtime.invoke(Orchestrator(), skill=skill_id)

    assert research.topics == ["tides"]


async def test_the_identity_mode_is_logged_once_without_credentials(
    hosted: Callable[..., Any], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    orchestrator = Orchestrator(allow_any_caller=True)

    await hosted(orchestrator, "plan", times=2)

    logged = [
        record.getMessage()
        for record in caplog.records
        if "skill catalog agents with identity" in record.getMessage()
    ]
    assert logged == [
        "Skill 'plan' calls its skill catalog agents with identity: Service"
    ]
    assert ORCHESTRATOR_KEY not in caplog.text
