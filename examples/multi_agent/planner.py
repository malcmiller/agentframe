"""An orchestrator: an ordinary agent whose skill calls a research agent over A2A.

The planner never imports the research agent's code. It finds it by name
(``research-agent``) through its directory, and describes the result it
expects with its own ``Source`` model, which ``call`` validates.

Start both agents with ``agentframe dev`` from this directory (see
``agentframe.yaml``); the research agent needs Ollama for its briefs::

    AGENTFRAME_DEV_CLIENT_KEY=dev-only-key agentframe dev

Then call ``plan`` on ``http://127.0.0.1:8002`` with the header
``X-API-Key: dev-only-key`` and the arguments
``{"goal": "A beach day", "topics": ["tides", "weather", "waves"]}``.
"""

import asyncio
from typing import Literal

from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    HostConfig,
    Inject,
    Remote,
    RunContext,
    Services,
    agent,
    dev_settings,
    skill,
)

type Research = Remote[Literal["research-agent"]]

# Each call runs at the same time as the others, so each gets its own share of
# the run's token budget instead of everything the run has left.
SOURCES_TOKENS = 500
BRIEF_TOKENS = 2_000


class Source(BaseModel):
    """A source, as the planner expects the research agent to describe it."""

    title: str
    url: str


class TopicPlan(BaseModel):
    """What the research agent found on one topic."""

    topic: str
    sources: list[Source]
    brief: str


class Plan(BaseModel):
    """A plan for a goal, researched topic by topic."""

    goal: str
    researchers: list[str]
    topics: list[TopicPlan]


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    """Plans a goal with help from a research agent."""

    @skill(id="plan", tags=["planning"])
    async def plan(
        self,
        goal: str,
        topics: list[str],
        research: Research,
        directory: Inject[AgentDirectory],
        ctx: RunContext,
    ) -> Plan:
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
        return Plan(
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


def create_app() -> AgentApp:
    """Serve the planner, calling ``research-agent`` with its development key."""
    dev = dev_settings()
    services = Services()
    if dev.directory is not None:
        services.add(AgentDirectory, dev.directory)
    return AgentApp(
        agent=Planner(),
        host=HostConfig(public_url=dev.public_url, auth=dev.auth),
        directory=dev.directory,
        remotes=dev.remotes,
        services=services,
    )
