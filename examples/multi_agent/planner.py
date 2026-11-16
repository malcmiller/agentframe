"""An orchestrator: an ordinary agent whose skills call other agents over A2A.

The planner never imports the other agents' code. It finds them by name
through its directory and describes the results it expects with its own
models, which ``call`` validates. It orchestrates two ways, side by side:

- ``plan`` is hand-written: plain ``await`` and ``asyncio.TaskGroup``, calling
  the research agent once per topic. Use this whenever the steps are known.
- ``propose`` and ``planned`` let the planner's model choose the steps: it
  plans a goal over the research and writer agents' skills with
  ``ctx.plan``, and ``planned`` runs that plan with ``ctx.run_plan``, in
  parallel where the steps allow. See docs/guides/orchestration.md.

**Open-proxy warning.** ``planned`` calls the other agents with this agent's
own development API key (``Service`` identity), whatever goal its caller
writes, so a caller could make the model call any skill in its catalog as
this agent. So the catalog holds only the two named agents, ``planned``
requires the ``plans:run`` permission, publishing requires ``posts:write``,
and the execution policy below checks every plan and every step. A
production host would prefer ``OnBehalfOf``, so every agent authorizes the
real user.

Start the agents with ``agentframe dev`` from this directory (see
``agentframe.yaml``). The research agent's briefs and the planner's plans use
local Llama through Ollama, with the installed model tag that
``AGENTFRAME_OLLAMA_MODEL`` names::

    AGENTFRAME_OLLAMA_MODEL=llama3.2:3b AGENTFRAME_DEV_CLIENT_KEY=dev-only-key \\
        agentframe dev

Then, on ``http://127.0.0.1:8002`` with the header ``X-API-Key: dev-only-key``,
call ``plan`` with ``{"goal": "A beach day", "topics": ["tides", "waves"]}``
or ``planned`` with ``{"goal": "Write a post titled Tides from sources about
tides and a short brief on them."}``.

Without ``AGENTFRAME_OLLAMA_MODEL`` the agents start without a model:
``find_sources`` and ``write_post`` still work, ``brief``, ``propose`` and
``planned`` fail with "This agent has no model configured", and ``plan``,
which asks for briefs, fails too.
"""

import asyncio
import dataclasses
import logging
import os
from collections.abc import Mapping
from typing import Any, Literal

from a2a.types import SecurityScheme
from pydantic import BaseModel, JsonValue

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    APIKeyAuth,
    ExecutionPolicy,
    HostConfig,
    Inject,
    Model,
    Plan,
    Principal,
    Remote,
    RunContext,
    RunLimits,
    Services,
    SkillCatalog,
    SkillDefinition,
    VerifiedCaller,
    agent,
    dev_settings,
    skill,
)

logger = logging.getLogger(__name__)

type Research = Remote[Literal["research-agent"]]

# Each call runs at the same time as the others, so each gets its own share of
# the run's token budget instead of everything the run has left.
SOURCES_TOKENS = 500
BRIEF_TOKENS = 2_000

# Model-planned orchestration: a narrowed catalog and a small plan.
SPECIALISTS = ["research-agent", "writer-agent"]
MAX_STEPS = 4
BRIEF = "research-agent.brief"
WRITE_POST = "writer-agent.write_post"
PUBLIC_LIBRARY = "https://example.org/"


class Source(BaseModel):
    """A source, as the planner expects the research agent to describe it."""

    title: str
    url: str


class TopicPlan(BaseModel):
    """What the research agent found on one topic."""

    topic: str
    sources: list[Source]
    brief: str


class ResearchPlan(BaseModel):
    """A plan for a goal, researched topic by topic."""

    goal: str
    researchers: list[str]
    topics: list[TopicPlan]


class PlanOutcome(BaseModel):
    """What running a model-made plan produced."""

    # The plan's result step's value, or None when that step did not complete.
    value: JsonValue
    complete: bool
    # Each step's status ("completed", "failed", "skipped", ...), by step ID.
    steps: dict[str, str]


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    """Plans goals with help from a research agent and a writer agent."""

    # Foundry reports no context window, so cap a planning call's input.
    limits = RunLimits(max_input_tokens=4000)

    @skill(id="plan", tags=["planning"])
    async def plan(
        self,
        goal: str,
        topics: list[str],
        research: Research,
        directory: Inject[AgentDirectory],
        ctx: RunContext,
    ) -> ResearchPlan:
        """Research every topic of a goal at once: sources and a short brief."""
        researchers = await directory.find(skill="find_sources")
        async with asyncio.TaskGroup() as group:
            sources = {
                topic: group.create_task(
                    research.call(
                        "find_sources",
                        topic=topic,
                        output=list[Source],
                        max_total_tokens=SOURCES_TOKENS,
                    )
                )
                for topic in topics
            }
            briefs = {
                topic: group.create_task(
                    research.ask(
                        f"Brief me on {topic} for this goal: {goal}",
                        max_total_tokens=BRIEF_TOKENS,
                    )
                )
                for topic in topics
            }
        return ResearchPlan(
            goal=goal,
            researchers=[card.name for card in researchers],
            topics=[
                TopicPlan(
                    topic=topic,
                    sources=sources[topic].result(),
                    brief=briefs[topic].result(),
                )
                for topic in topics
            ],
        )

    @skill(id="propose", tags=["planning"])
    async def propose(self, goal: str, ctx: RunContext) -> Plan:
        """Plan a goal over the research and writer agents; nothing is sent to them."""
        skills = await specialists(ctx)
        return await ctx.plan(goal, skills, max_steps=MAX_STEPS)

    # Open-proxy warning: the model calls other agents as this agent.
    @skill(id="planned", tags=["planning"], requires_permissions=["plans:run"])
    async def planned(self, goal: str, ctx: RunContext) -> PlanOutcome:
        """Plan a goal over the research and writer agents, then run the plan."""
        skills = await specialists(ctx)
        plan = await ctx.plan(goal, skills, max_steps=MAX_STEPS)
        result = await ctx.run_plan(
            plan,
            skills,
            max_steps=MAX_STEPS,
            max_tokens_per_step=BRIEF_TOKENS,
            # A failed step keeps the others' results instead of failing all.
            on_failure="continue",
        )
        return PlanOutcome(
            value=result.value,
            complete=result.complete,
            steps={step_id: step.status for step_id, step in result.steps.items()},
        )


async def specialists(ctx: RunContext) -> SkillCatalog:
    """Return the skills of the two named agents, never every registered agent."""
    return await ctx.remote_skills(
        agents=SPECIALISTS,
        # Callers without this permission never see the writer.
        permissions={WRITE_POST: ["posts:write"]},
    )


async def at_most_one_brief(
    skill: SkillDefinition, principal: Principal | None, plan: Plan
) -> bool:
    """Allow at most one brief per plan: each one uses the research agent's model."""
    return sum(step.use == BRIEF for step in plan.steps) <= 1


async def public_sources_only(
    skill: SkillDefinition,
    principal: Principal | None,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    """Posts are public, so send the writer only sources from the public library.

    Runs before every step with the arguments about to be sent, after earlier
    steps' results are filled in: the place to stop data leaving.
    """
    if tool_name != WRITE_POST:
        return True
    sources = arguments.get("sources")
    return isinstance(sources, list) and all(
        isinstance(source, dict)
        and str(source.get("url", "")).startswith(PUBLIC_LIBRARY)
        for source in sources
    )


class DeveloperRoles:
    """Development placeholder: the development keys, plus the ``planner``
    role for the ``developer`` caller (``AGENTFRAME_DEV_CLIENT_KEY``).

    Production hosts get roles from their identity provider instead, such as
    Entra app roles through ``EntraIDAuth``.
    """

    def __init__(self, auth: APIKeyAuth) -> None:
        self.auth = auth

    def card_security(self) -> dict[str, SecurityScheme]:
        return self.auth.card_security()

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        caller = await self.auth.authenticate(headers)
        if caller.id != "developer":
            return caller
        return dataclasses.replace(caller, roles=frozenset({"planner"}))


def create_model() -> Model | None:
    """Return local Llama through Ollama, or ``None`` to start without a model.

    ``AGENTFRAME_OLLAMA_MODEL`` names the model tag, which must already be
    installed; without it, a warning names the skills that will fail. Only
    ``propose`` and ``planned`` use the planner's model.
    """
    tag = os.environ.get("AGENTFRAME_OLLAMA_MODEL")
    if not tag:
        logger.warning(
            "AGENTFRAME_OLLAMA_MODEL is not set; propose and planned will fail"
        )
        return None
    from agentframe import OllamaModel

    return OllamaModel(model=tag, context_window=8192)


def create_app(model: Model | None = None) -> AgentApp:
    """Serve the planner, calling the other agents with its development key.

    Args:
        model: The planning model, which must support structured output.
            Defaults to ``create_model()``, which may be no model.
    """
    dev = dev_settings()
    services = Services()
    if dev.directory is not None:
        services.add(AgentDirectory, dev.directory)
    return AgentApp(
        agent=Planner(model=model or create_model()),
        host=HostConfig(
            public_url=dev.public_url,
            auth=DeveloperRoles(dev.auth),
            role_permissions={"planner": ["plans:run", "posts:write"]},
        ),
        directory=dev.directory,
        remotes=dev.remotes,
        services=services,
        policy=ExecutionPolicy(
            plan_checks=[at_most_one_brief], tool_checks=[public_sources_only]
        ),
    )
