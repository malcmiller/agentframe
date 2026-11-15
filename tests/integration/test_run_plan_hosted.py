"""``ctx.run_plan`` runs a checked plan over real localhost A2A.

The specialist is an ``AgentApp`` whose skills record when they run, and the
plain peer is a bare official-SDK server (``tests/support/a2a_peer.py``);
both are listed in one registry. The orchestrator runs a caller-supplied
plan locally, except in the depth test, which hosts it.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from a2a.helpers import get_message_text
from a2a.types import TaskState
from pydantic import BaseModel

from a2a_client import call_skill, connect
from a2a_peer import PEER_QUESTION, PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    ClarificationNeededError,
    DefinitionError,
    ExecutionPolicy,
    FinishReason,
    HostConfig,
    InputValidationError,
    ModelResponse,
    ModelSettings,
    PermissionDeniedError,
    Plan,
    PlanResult,
    PlanStep,
    PolicyUnavailableError,
    Principal,
    RemoteAgentError,
    RunContext,
    RunLimits,
    RunOptions,
    RunResult,
    Runtime,
    SkillCatalog,
    StepResult,
    TimeoutLimitExceededError,
    TokenLimitExceededError,
    Usage,
    agent,
    skill,
)
from agentframe.authoring.definitions import SkillDefinition
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

STEP_SECONDS = 0.3


class Found(BaseModel):
    topic: str
    sources: list[str]


def fifty_tokens() -> ModelResponse:
    usage = Usage(input_tokens=40, output_tokens=10, total_tokens=50, estimated=False)
    return ModelResponse(text="ok", usage=usage, finish_reason=FinishReason.COMPLETED)


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Research(Agent):
    """Records every call, how many run at once and which were cancelled.

    ``find_sources`` uses one 50-token model call, then waits
    ``STEP_SECONDS``; the topic ``"slow"`` waits until cancelled and
    ``"fail"`` fails.
    """

    def __init__(self) -> None:
        super().__init__(model=ScriptedModel(fifty_tokens() for _ in range(20)))
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.spans: dict[str, tuple[float, float]] = {}
        self.running = 0
        self.most_at_once = 0
        self.cancelled = asyncio.Event()

    @skill(
        id="find_sources",
        description="Find sources on a topic.",
        tags=["research"],
        model_settings=ModelSettings(max_output_tokens=100),
    )
    async def find_sources(self, topic: str, ctx: RunContext) -> Found:
        self.calls.append(("find_sources", {"topic": topic}))
        loop = asyncio.get_running_loop()
        started = loop.time()
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        try:
            await ctx.generate(topic)
            if topic == "slow":
                await asyncio.sleep(60)
            await asyncio.sleep(STEP_SECONDS)
            if topic == "fail":
                raise RuntimeError("secret failure detail")
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.running -= 1
        self.spans[topic] = (started, loop.time())
        return Found(topic=topic, sources=[f"{topic} source"])

    @skill(id="brief", description="Brief from sources.", tags=["research"])
    async def brief(
        self, sources: list[str], more_sources: list[str], ctx: RunContext
    ) -> str:
        self.calls.append(("brief", {"sources": sources, "more_sources": more_sources}))
        self.spans["brief"] = (asyncio.get_running_loop().time(), 0.0)
        return " + ".join(sources + more_sources)


@agent(name="Orchestrator", description="Runs reviewed plans.", version="0.1.0")
class Orchestrator(Agent):
    """Runs the plan it is given over every registered agent's skills."""

    def __init__(
        self,
        permissions: Mapping[str, set[str]] | None = None,
        **run_options: Any,
    ) -> None:
        super().__init__()
        self.permissions = permissions
        self.run_options = run_options
        self.result: PlanResult | None = None
        self.error: Exception | None = None

    @skill(id="execute", description="Run a reviewed plan.", tags=["orchestration"])
    async def execute(self, plan: Plan, ctx: RunContext) -> str:
        skills = await ctx.remote_skills(permissions=self.permissions)
        try:
            self.result = await ctx.run_plan(plan, skills, **self.run_options)
        except Exception as error:
            self.error = error
            raise
        return str(self.result.value)


class Registered(AgentRegistry):
    """Lists the agents served for one test by name."""

    def __init__(self, **urls: str) -> None:
        self.urls = urls

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [
            AgentReference(name, f"{url}/.well-known/agent-card.json")
            for name, url in self.urls.items()
        ]


def anonymous(public_url: str) -> HostConfig:
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


type Agents = Callable[[], AbstractAsyncContextManager[AgentDirectory]]


@pytest.fixture
def research() -> Research:
    return Research()


@pytest.fixture
def peer() -> PeerExecutor:
    return PeerExecutor()


@pytest.fixture
def agents(serve: ServeLocally, research: Research, peer: PeerExecutor) -> Agents:
    """Serve the specialist and the plain peer; yield a directory listing both."""

    @asynccontextmanager
    async def serving() -> AsyncIterator[AgentDirectory]:
        def research_app(public_url: str) -> AgentApp:
            return AgentApp(agent=research, host=anonymous(public_url))

        async with (
            serve(research_app) as research_url,
            serve(a2a_peer(peer)) as peer_url,
            AgentDirectory(
                sources=[Registered(research=research_url, peer=peer_url)]
            ) as directory,
        ):
            yield directory

    return serving


async def execute(
    agents: Agents,
    orchestrator: Orchestrator,
    plan: Plan,
    *,
    limits: RunLimits | None = None,
    policy: ExecutionPolicy | None = None,
    principal: Principal | None = None,
) -> RunResult[str]:
    """Run the orchestrator's ``execute`` skill locally with ``plan``."""
    async with agents() as directory:
        return await Runtime(directory=directory, policy=policy).run(
            orchestrator,
            skill="execute",
            arguments={"plan": plan.model_dump()},
            principal=principal,
            options=RunOptions(limits=limits),
        )


def find(step_id: str, topic: str, **options: Any) -> PlanStep:
    return PlanStep(
        id=step_id, use="research.find_sources", arguments={"topic": topic}, **options
    )


def brief(first: str, second: str) -> PlanStep:
    return PlanStep(
        id="brief",
        use="research.brief",
        arguments={"sources": {"$ref": first}, "more_sources": {"$ref": second}},
    )


TIDES_AND_WAVES = Plan(
    steps=[
        find("tides", "tides"),
        find("waves", "waves"),
        brief("tides.sources", "waves.sources"),
    ],
    result="brief",
)


class Recorder:
    """A tool check that allows everything and records what it saw."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    async def __call__(
        self,
        skill: SkillDefinition,
        principal: Principal | None,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        self.seen.append((tool_name, dict(arguments)))
        return True


async def test_independent_steps_overlap_and_the_dependent_step_waits_for_both(
    agents: Agents, research: Research
) -> None:
    recorder = Recorder()
    orchestrator = Orchestrator()

    result = await execute(
        agents,
        orchestrator,
        TIDES_AND_WAVES,
        policy=ExecutionPolicy(tool_checks=[recorder]),
    )

    tides, waves = research.spans["tides"], research.spans["waves"]
    assert tides[0] < waves[1] and waves[0] < tides[1]
    assert research.spans["brief"][0] >= max(tides[1], waves[1])
    assert result.value == "tides source + waves source"
    # The values reached the dependent step, and its tool check, exactly.
    expected = {"sources": ["tides source"], "more_sources": ["waves source"]}
    assert research.calls[-1] == ("brief", expected)
    assert ("research.brief", expected) in recorder.seen
    assert orchestrator.result is not None
    assert list(orchestrator.result.steps) == ["tides", "waves", "brief"]
    steps = orchestrator.result.steps
    assert all(
        step.status == "completed" and step.remote_task_id for step in steps.values()
    )
    assert steps["tides"].value == {"topic": "tides", "sources": ["tides source"]}
    assert steps["tides"].usage is not None
    assert steps["tides"].usage.total_tokens == 50
    assert steps["tides"].use == "research.find_sources"
    assert (steps["tides"].duration_ms or 0) >= STEP_SECONDS * 1000
    # The run's usage is the steps' reported usage: two 50-token model calls,
    # charged once, and the plan's usage adds up the same steps.
    assert result.usage.total_tokens == 100
    assert orchestrator.result.usage is not None
    assert orchestrator.result.usage.total_tokens == 100
    assert result.tool_calls == 3


async def test_a_tool_check_cannot_change_the_nested_arguments_a_step_sends(
    agents: Agents, research: Research
) -> None:
    recorder = Recorder()

    async def tamper(
        skill: SkillDefinition,
        principal: Principal | None,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        for value in arguments.values():
            if isinstance(value, list):
                value.append("tampered")
        return True

    await execute(
        agents,
        Orchestrator(),
        TIDES_AND_WAVES,
        policy=ExecutionPolicy(tool_checks=[tamper, recorder]),
    )

    expected = {"sources": ["tides source"], "more_sources": ["waves source"]}
    assert research.calls[-1] == ("brief", expected)
    assert ("research.brief", expected) in recorder.seen


async def test_at_most_max_parallel_steps_run_at_once(
    agents: Agents, research: Research
) -> None:
    topics = ["a", "b", "c", "d", "e"]
    plan = Plan(steps=[find(topic, topic) for topic in topics], result="a")

    await execute(agents, Orchestrator(max_parallel=2), plan)

    assert research.most_at_once == 2
    assert len(research.calls) == 5


async def test_four_steps_run_at_once_by_default(
    agents: Agents, research: Research
) -> None:
    topics = ["a", "b", "c", "d", "e", "f"]
    plan = Plan(steps=[find(topic, topic) for topic in topics], result="a")

    await execute(agents, Orchestrator(), plan)

    assert research.most_at_once == 4


async def test_a_tool_check_sees_an_upstream_value_and_stops_it_leaving(
    agents: Agents, research: Research, peer: PeerExecutor
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def no_payroll_to_peer(
        skill: SkillDefinition,
        principal: Principal | None,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        seen.append((tool_name, dict(arguments)))
        return not (tool_name == "peer" and "payroll" in arguments["text"])

    plan = Plan(
        steps=[
            find("secret", "payroll"),
            PlanStep(id="leak", use="peer", text="Publish {secret.sources}"),
        ],
        result="leak",
    )

    with pytest.raises(PermissionDeniedError, match="Tool 'peer'"):
        await execute(
            agents,
            Orchestrator(),
            plan,
            policy=ExecutionPolicy(tool_checks=[no_payroll_to_peer]),
        )

    assert seen == [
        ("research.find_sources", {"topic": "payroll"}),
        ("peer", {"text": 'Publish ["payroll source"]'}),
    ]
    assert peer.requests == []


async def test_a_caller_supplied_plan_cannot_use_an_entry_the_caller_lacks(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(permissions={"research.find_sources": {"research:use"}})
    plan = Plan(steps=[find("tides", "tides")], result="tides")

    # As in ctx.plan, a hidden entry looks exactly like a missing one.
    with pytest.raises(InputValidationError) as raised:
        await execute(agents, orchestrator, plan, principal=Principal("caller"))

    assert raised.value.message == (
        "Plan step 'tides' field 'use' names no entry of the skill catalog"
    )
    assert research.calls == []


async def test_a_caller_with_the_permission_runs_the_step(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(permissions={"research.find_sources": {"research:use"}})
    plan = Plan(steps=[find("tides", "tides")], result="tides")
    caller = Principal("caller", frozenset({"research:use"}))

    await execute(agents, orchestrator, plan, principal=caller)

    assert research.calls == [("find_sources", {"topic": "tides"})]


async def test_placeholders_insert_text_and_json_and_a_plain_peer_makes_usage_unknown(
    agents: Agents, peer: PeerExecutor
) -> None:
    orchestrator = Orchestrator()
    plan = Plan(
        steps=[
            find("found", "tides"),
            PlanStep(id="post", use="peer", text="Post on {found.topic}: {found} {{}}"),
        ],
        result="post",
    )

    result = await execute(agents, orchestrator, plan)

    # Objects are rendered as compact JSON with sorted keys.
    sent = 'Post on tides: {"sources":["tides source"],"topic":"tides"} {}'
    assert [get_message_text(request.message) for request in peer.requests] == [sent]
    assert result.value == f"Echo: {sent}"
    assert orchestrator.result is not None
    assert orchestrator.result.steps["post"].usage is None
    assert orchestrator.result.steps["found"].usage is not None
    # One unknown step makes the plan's usage unknown, never a partial sum.
    assert orchestrator.result.usage is None
    assert result.usage.total_tokens is None


async def test_a_missing_field_fails_its_step_without_naming_the_value(
    agents: Agents, research: Research
) -> None:
    plan = Plan(
        steps=[
            find("tides", "tides"),
            find("waves", "waves"),
            brief("tides.missing", "waves.sources"),
        ],
        result="brief",
    )

    with pytest.raises(InputValidationError) as raised:
        await execute(agents, Orchestrator(), plan)

    assert raised.value.message == (
        "Plan step 'brief' field 'arguments.sources' refers to field 'missing' "
        "of 'tides', whose result has no such field"
    )
    assert [name for name, _ in research.calls] == ["find_sources", "find_sources"]


async def test_a_failing_step_cancels_its_running_sibling_and_raises_itself(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator()
    plan = Plan(
        steps=[
            find("slow", "slow"),
            find("bad", "fail"),
            find("later", "later", after=["bad"]),
        ],
        result="later",
    )

    with pytest.raises(RemoteAgentError, match="did not complete its task"):
        await execute(agents, orchestrator, plan)

    assert type(orchestrator.error) is RemoteAgentError
    assert "secret" not in str(orchestrator.error)
    # The slow step's task was cancelled at the specialist.
    async with asyncio.timeout(5):
        await research.cancelled.wait()
    assert ("find_sources", {"topic": "later"}) not in research.calls


async def test_the_plan_stops_at_the_run_deadline(
    agents: Agents, research: Research
) -> None:
    plan = Plan(steps=[find("slow", "slow")], result="slow")

    with pytest.raises(TimeoutLimitExceededError):
        await execute(agents, Orchestrator(), plan, limits=RunLimits(timeout_seconds=1))

    async with asyncio.timeout(5):
        await research.cancelled.wait()


async def test_steps_count_toward_max_tool_calls(
    agents: Agents, research: Research
) -> None:
    with pytest.raises(InputValidationError, match="more than the 2 this run allows"):
        await execute(
            agents,
            Orchestrator(),
            TIDES_AND_WAVES,
            limits=RunLimits(max_tool_calls=2),
        )

    assert research.calls == []


async def test_step_token_holds_cannot_exceed_the_run_budget(
    agents: Agents, research: Research
) -> None:
    plan = Plan(steps=[find("tides", "tides"), find("waves", "waves")], result="tides")

    with pytest.raises(TokenLimitExceededError, match="needs up to 600 tokens"):
        await execute(
            agents,
            Orchestrator(max_tokens_per_step=600),
            plan,
            limits=RunLimits(max_total_tokens=1000),
        )


async def test_the_plan_fails_when_too_few_tokens_are_left_to_share(
    agents: Agents, research: Research
) -> None:
    plan = Plan(steps=[find("tides", "tides"), find("waves", "waves")], result="tides")

    with pytest.raises(TokenLimitExceededError, match="too few to share"):
        await execute(
            agents, Orchestrator(), plan, limits=RunLimits(max_total_tokens=1)
        )

    assert research.calls == []


async def test_a_step_never_goes_deeper_than_max_remote_hops(
    serve: ServeLocally, agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator()

    async with agents() as directory:

        def orchestrator_app(public_url: str) -> AgentApp:
            return AgentApp(
                agent=orchestrator, host=anonymous(public_url), directory=directory
            )

        async with serve(orchestrator_app) as url:
            client = await connect(url)
            plan = Plan(steps=[find("tides", "tides")], result="tides")
            task = await call_skill(
                client,
                "execute",
                {"plan": plan.model_dump()},
                metadata={"agentframe.hops": 5},
            )

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert "Maximum remote call depth of 5 exceeded" in str(orchestrator.error)
    assert research.calls == []


async def test_a_catalog_from_elsewhere_is_refused(agents: Agents) -> None:
    @agent(name="Stray", description="Uses a hand-built catalog.", version="0.1.0")
    class Stray(Agent):
        @skill(id="execute", description="Run a plan.", tags=["orchestration"])
        async def execute(self, plan: Plan, ctx: RunContext) -> str:
            await ctx.run_plan(plan, SkillCatalog([], {}))
            return "unreachable"

    plan = Plan(steps=[find("tides", "tides")], result="tides")

    async with agents() as directory:
        with pytest.raises(DefinitionError, match="catalog this run built"):
            await Runtime(directory=directory).run(
                Stray(), skill="execute", arguments={"plan": plan.model_dump()}
            )


async def test_continue_keeps_the_independent_branch_and_skips_dependents_of_a_failure(
    agents: Agents, research: Research
) -> None:
    recorder = Recorder()
    orchestrator = Orchestrator(on_failure="continue")
    plan = Plan(
        steps=[
            find("tides", "tides"),
            find("bad", "fail"),
            find("later", "later", after=["bad"]),
            brief("tides.sources", "bad.sources"),
        ],
        result="brief",
    )

    result = await execute(
        agents, orchestrator, plan, policy=ExecutionPolicy(tool_checks=[recorder])
    )

    assert orchestrator.result is not None
    assert orchestrator.result.complete is False
    assert orchestrator.result.value is None
    steps = orchestrator.result.steps
    assert {name: step.status for name, step in steps.items()} == {
        "tides": "completed",
        "bad": "failed",
        "later": "skipped",
        "brief": "skipped",
    }
    assert steps["tides"].value == {"topic": "tides", "sources": ["tides source"]}
    bad = steps["bad"]
    assert bad.value is None and bad.usage is None and bad.remote_task_id
    assert bad.error_type == "RemoteAgentError"
    assert bad.error_message == "Agent 'research' did not complete its task"
    assert steps["later"] == StepResult(
        "skipped", None, None, None, "research.find_sources", None
    )
    # The failed step was sent, and its usage is unknown, so the plan's is.
    assert orchestrator.result.usage is None
    # Skipped steps are never counted, checked or sent.
    assert result.tool_calls == 2
    assert [name for name, _ in recorder.seen] == ["research.find_sources"] * 2
    assert [arguments for _, arguments in research.calls] == [
        {"topic": "tides"},
        {"topic": "fail"},
    ]


async def test_continue_returns_the_result_value_when_another_step_fails(
    agents: Agents, peer: PeerExecutor
) -> None:
    orchestrator = Orchestrator(on_failure="continue")
    plan = Plan(
        steps=[find("tides", "tides"), PlanStep(id="post", use="peer", text="fail")],
        result="tides",
    )

    await execute(agents, orchestrator, plan)

    assert orchestrator.result is not None
    assert orchestrator.result.complete is False
    assert orchestrator.result.value == {
        "topic": "tides",
        "sources": ["tides source"],
    }
    post = orchestrator.result.steps["post"]
    assert post.status == "failed"
    assert post.error_message is not None
    assert "secret" not in post.error_message


async def test_continue_keeps_a_remote_question_and_a_remote_cancellation(
    agents: Agents, peer: PeerExecutor
) -> None:
    orchestrator = Orchestrator(on_failure="continue")
    plan = Plan(
        steps=[
            PlanStep(id="asked", use="peer", text="question"),
            PlanStep(id="stopped", use="peer", text="cancel"),
        ],
        result="asked",
    )

    await execute(agents, orchestrator, plan)

    assert orchestrator.result is not None
    asked = orchestrator.result.steps["asked"]
    assert asked.status == "input_required"
    assert asked.question == PEER_QUESTION
    assert asked.error_type == "ClarificationNeededError"
    assert asked.error_message is not None
    assert PEER_QUESTION not in asked.error_message
    assert asked.remote_task_id
    stopped = orchestrator.result.steps["stopped"]
    assert stopped.status == "cancelled"
    assert stopped.error_type == "RemoteAgentError"
    # Nothing answers the question: the peer got exactly one request each.
    assert len(peer.requests) == 2


async def test_stop_raises_a_remote_question(
    agents: Agents, peer: PeerExecutor
) -> None:
    plan = Plan(
        steps=[PlanStep(id="asked", use="peer", text="question")], result="asked"
    )

    with pytest.raises(ClarificationNeededError) as raised:
        await execute(agents, Orchestrator(), plan)

    assert raised.value.question == PEER_QUESTION


async def test_continue_still_stops_at_a_token_limit(
    agents: Agents, research: Research
) -> None:
    plan = Plan(steps=[find("tides", "tides"), find("waves", "waves")], result="tides")

    with pytest.raises(TokenLimitExceededError, match="needs up to 600 tokens"):
        await execute(
            agents,
            Orchestrator(max_tokens_per_step=600, on_failure="continue"),
            plan,
            limits=RunLimits(max_total_tokens=1000),
        )


async def test_continue_still_stops_at_a_tool_check_denial(
    agents: Agents, research: Research, peer: PeerExecutor
) -> None:
    async def no_peer(
        skill: SkillDefinition,
        principal: Principal | None,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        return tool_name != "peer"

    plan = Plan(
        steps=[find("slow", "slow"), PlanStep(id="post", use="peer", text="hello")],
        result="slow",
    )

    with pytest.raises(PermissionDeniedError, match="Tool 'peer'"):
        await execute(
            agents,
            Orchestrator(on_failure="continue"),
            plan,
            policy=ExecutionPolicy(tool_checks=[no_peer]),
        )

    assert peer.requests == []
    async with asyncio.timeout(5):
        await research.cancelled.wait()


async def test_continue_still_stops_at_a_missing_field(
    agents: Agents, research: Research
) -> None:
    plan = Plan(
        steps=[
            find("tides", "tides"),
            find("waves", "waves"),
            brief("tides.missing", "waves.sources"),
        ],
        result="brief",
    )

    with pytest.raises(InputValidationError, match="has no such field"):
        await execute(agents, Orchestrator(on_failure="continue"), plan)


async def test_continue_still_stops_when_the_run_is_cancelled(
    agents: Agents, research: Research
) -> None:
    orchestrator = Orchestrator(on_failure="continue")
    plan = Plan(steps=[find("slow", "slow"), find("tides", "tides")], result="tides")

    async with agents() as directory:
        running = asyncio.create_task(
            Runtime(directory=directory).run(
                orchestrator, skill="execute", arguments={"plan": plan.model_dump()}
            )
        )
        async with asyncio.timeout(5):
            while ("find_sources", {"topic": "slow"}) not in research.calls:
                await asyncio.sleep(0.01)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        async with asyncio.timeout(5):
            await research.cancelled.wait()

    assert orchestrator.result is None


async def test_on_failure_must_be_stop_or_continue(
    agents: Agents, research: Research
) -> None:
    plan = Plan(steps=[find("tides", "tides")], result="tides")

    with pytest.raises(DefinitionError, match="on_failure="):
        await execute(agents, Orchestrator(on_failure="ignore"), plan)

    assert research.calls == []


# Plan checks (#174)


async def test_a_plan_check_sees_every_step_before_any_and_tool_checks_still_run(
    agents: Agents, research: Research
) -> None:
    seen: list[Plan] = []
    recorder = Recorder()

    async def allow(
        skill: SkillDefinition, principal: Principal | None, plan: Plan
    ) -> bool:
        assert research.calls == []
        seen.append(plan.model_copy(deep=True))
        # Each check gets a copy, so changing it changes nothing that runs.
        plan.steps[0].arguments["topic"] = "changed"
        return True

    result = await execute(
        agents,
        Orchestrator(),
        TIDES_AND_WAVES,
        policy=ExecutionPolicy(plan_checks=[allow, allow], tool_checks=[recorder]),
    )

    # The second check saw the whole plan, references included, unchanged
    # by the first.
    assert seen[1] == TIDES_AND_WAVES
    assert seen[1].steps[2].arguments["sources"] == {"$ref": "tides.sources"}
    assert ("find_sources", {"topic": "tides"}) in research.calls
    assert result.value == "tides source + waves source"
    assert len(recorder.seen) == 3


@pytest.mark.parametrize("decision", [False, None, 1, "yes"])
@pytest.mark.parametrize("on_failure", ["stop", "continue"])
async def test_a_denying_plan_check_sends_no_step_under_either_failure_policy(
    agents: Agents,
    research: Research,
    peer: PeerExecutor,
    decision: object,
    on_failure: str,
) -> None:
    async def deny(
        skill: SkillDefinition, principal: Principal | None, plan: Plan
    ) -> Any:
        return decision

    plan = Plan(
        steps=[find("tides", "tides"), PlanStep(id="post", use="peer", text="hi")],
        result="post",
    )

    with pytest.raises(PermissionDeniedError) as raised:
        await execute(
            agents,
            Orchestrator(on_failure=on_failure),
            plan,
            policy=ExecutionPolicy(plan_checks=[deny]),
        )

    assert raised.value.message == "The plan was blocked by an execution policy check"
    assert research.calls == []
    assert peer.requests == []


async def test_a_raising_plan_check_fails_closed_without_revealing_the_plan(
    agents: Agents, research: Research
) -> None:
    async def broken(
        skill: SkillDefinition, principal: Principal | None, plan: Plan
    ) -> bool:
        raise RuntimeError(f"secret {plan.steps[0].arguments}")

    with pytest.raises(PolicyUnavailableError) as raised:
        await execute(
            agents,
            Orchestrator(),
            TIDES_AND_WAVES,
            policy=ExecutionPolicy(plan_checks=[broken]),
        )

    assert raised.value.message == (
        "The plan could not be checked: an execution policy check is unavailable"
    )
    assert research.calls == []


async def test_a_plan_check_still_running_at_the_deadline_fails_closed(
    agents: Agents, research: Research
) -> None:
    async def slow(
        skill: SkillDefinition, principal: Principal | None, plan: Plan
    ) -> bool:
        await asyncio.sleep(60)
        return True

    with pytest.raises(PolicyUnavailableError):
        await execute(
            agents,
            Orchestrator(),
            TIDES_AND_WAVES,
            limits=RunLimits(timeout_seconds=0.5),
            policy=ExecutionPolicy(plan_checks=[slow]),
        )

    assert research.calls == []


async def test_plan_checks_run_only_on_a_plan_the_catalog_check_accepted(
    agents: Agents,
) -> None:
    checked: list[Plan] = []

    async def record(
        skill: SkillDefinition, principal: Principal | None, plan: Plan
    ) -> bool:
        checked.append(plan)
        return True

    plan = Plan(steps=[find("tides", "tides")], result="missing")

    with pytest.raises(InputValidationError):
        await execute(
            agents, Orchestrator(), plan, policy=ExecutionPolicy(plan_checks=[record])
        )

    assert checked == []


STEP_LOG_FIELDS = {
    "agent",
    "invocation_id",
    "root_invocation_id",
    "parent_invocation_id",
    "plan_step",
    "use",
    "status",
    "error_type",
    "remote_task_id",
    "duration_ms",
    "total_tokens",
}


def records(caplog: pytest.LogCaptureFixture, prefix: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith(prefix)]


async def test_steps_are_logged_with_correlation_and_without_content(
    serve: ServeLocally,
    agents: Agents,
    research: Research,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    orchestrator = Orchestrator()
    plan = Plan(
        steps=[find("found", "hush"), brief("found.sources", "found.sources")],
        result="brief",
    )

    async with agents() as directory:

        def orchestrator_app(public_url: str) -> AgentApp:
            return AgentApp(
                agent=orchestrator, host=anonymous(public_url), directory=directory
            )

        async with serve(orchestrator_app) as url:
            client = await connect(url)
            task = await call_skill(client, "execute", {"plan": plan.model_dump()})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    runs = {r.__dict__["agent"]: r.__dict__ for r in records(caplog, "Run started")}
    run = runs["Orchestrator"]
    started = records(caplog, "Plan step started")
    completed = records(caplog, "Plan step completed")
    assert [r.__dict__["plan_step"] for r in started] == ["found", "brief"]
    assert [r.__dict__["plan_step"] for r in completed] == ["found", "brief"]
    for record in started + completed:
        fields = record.__dict__
        assert fields.keys() >= STEP_LOG_FIELDS
        assert fields["agent"] == "Orchestrator"
        assert fields["invocation_id"] == run["invocation_id"]
        assert fields["root_invocation_id"] == run["root_invocation_id"]
    found, briefed = (r.__dict__ for r in completed)
    assert found["use"] == "research.find_sources"
    assert found["status"] == "completed"
    assert found["remote_task_id"]
    assert found["duration_ms"] >= STEP_SECONDS * 1000
    assert found["total_tokens"] == 50
    # The brief skill makes no model call, so its agent reports zero tokens.
    assert briefed["total_tokens"] == 0
    # The specialist sees the orchestrator's run as its parent, same root.
    specialist_runs = [
        r.__dict__
        for r in records(caplog, "Run started")
        if r.__dict__["agent"] == "Research"
    ]
    assert len(specialist_runs) == 2
    for specialist in specialist_runs:
        assert specialist["parent_invocation_id"] == run["invocation_id"]
        assert specialist["root_invocation_id"] == run["root_invocation_id"]
    # No argument or result reaches the orchestrator's plan logs.
    for record in started + completed:
        assert "hush" not in record.getMessage()
        assert all("hush" not in str(value) for value in record.__dict__.values())


async def test_a_failing_step_is_logged_without_the_peers_error(
    agents: Agents, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    plan = Plan(steps=[find("bad", "fail")], result="bad")

    with pytest.raises(RemoteAgentError):
        await execute(agents, Orchestrator(), plan)

    (record,) = records(caplog, "Plan step completed")
    assert record.__dict__["status"] == "failed"
    assert record.__dict__["error_type"] == "RemoteAgentError"
    assert record.__dict__["remote_task_id"]
    assert record.__dict__["total_tokens"] is None
    for content in ("did not complete", "secret"):
        assert content not in record.getMessage()
        assert all(content not in str(value) for value in record.__dict__.values())


async def test_continue_logs_the_error_type_only_and_skipped_steps(
    agents: Agents, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="agentframe")
    plan = Plan(
        steps=[find("bad", "fail"), find("later", "later", after=["bad"])],
        result="later",
    )

    await execute(agents, Orchestrator(on_failure="continue"), plan)

    completed = {
        r.__dict__["plan_step"]: r for r in records(caplog, "Plan step completed")
    }
    bad, later = completed["bad"].__dict__, completed["later"].__dict__
    assert bad["status"] == "failed"
    assert bad["error_type"] == "RemoteAgentError"
    assert bad["remote_task_id"]
    assert later["status"] == "skipped"
    assert later["duration_ms"] is None
    assert [r.__dict__["plan_step"] for r in records(caplog, "Plan step started")] == [
        "bad"
    ]
    # Neither the safe error message nor the peer's own error is logged.
    for record in completed.values():
        for content in ("did not complete", "secret"):
            assert content not in record.getMessage()
            assert all(content not in str(value) for value in record.__dict__.values())
