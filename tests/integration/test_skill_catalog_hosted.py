"""A hosted orchestrator builds its skill catalog from real agents' cards.

An AgentFrame specialist, a plain official-SDK peer and the orchestrator
itself are served over localhost HTTP and listed in one registry file. The
orchestrator is called with the official A2A SDK client.
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from a2a.helpers import get_data_parts
from a2a.types import TaskState
from pydantic import BaseModel

from a2a_client import call_skill, connect
from a2a_peer import PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AllowAnonymous,
    FileAgentRegistry,
    HostConfig,
    RunContext,
    agent,
    skill,
)
from local_server import ServeLocally


class Topic(BaseModel):
    name: str


@agent(name="Research", description="Finds things out.", version="0.1.0")
class Specialist(Agent):
    @skill(id="find_sources", description="Find sources.", tags=["research"])
    async def find_sources(self, topic: Topic, ctx: RunContext) -> list[str]:
        return [topic.name]

    @skill(id="summarize", description="Summarize a text.", tags=["writing"])
    async def summarize(self, text: str, ctx: RunContext) -> str:
        return text[:10]


@agent(name="Orchestrator", description="Delegates.", version="0.1.0")
class Orchestrator(Agent):
    @skill(id="catalog", description="List remote skills.", tags=["orchestration"])
    async def catalog(self, ctx: RunContext) -> list[dict[str, object]]:
        return [
            {"name": entry.name, "schema": entry.arguments_schema}
            for entry in await ctx.remote_skills(all_registered=True)
        ]


def anonymous(public_url: str) -> HostConfig:
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[tuple[Path, AgentDirectory]]:
    agents = tmp_path / "agents.yaml"
    agents.write_text("[]\n")
    async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as directory:
        yield agents, directory


def register(agents: Path, **urls: str) -> None:
    agents.write_text(
        "".join(
            f"- name: {name}\n  url: {url}/.well-known/agent-card.json\n"
            for name, url in urls.items()
        )
    )


async def test_the_catalog_lists_registered_agents_but_not_the_orchestrator(
    serve: ServeLocally,
    registry: tuple[Path, AgentDirectory],
    caplog: pytest.LogCaptureFixture,
) -> None:
    agents, directory = registry
    caplog.set_level(logging.WARNING, logger="agentframe.orchestration.catalog")

    def orchestrator(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Orchestrator(), host=anonymous(public_url), directory=directory
        )

    def research(public_url: str) -> AgentApp:
        return AgentApp(agent=Specialist(), host=anonymous(public_url))

    async with (
        serve(research) as research_url,
        serve(a2a_peer(PeerExecutor())) as peer_url,
        serve(orchestrator) as orchestrator_url,
    ):
        register(
            agents,
            orchestrator=orchestrator_url,
            research=research_url,
            peer=peer_url,
        )
        async with await connect(orchestrator_url) as client:
            task = await call_skill(client, "catalog", {})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [result] = get_data_parts(task.artifacts[0].parts)
    entries = {entry["name"]: entry["schema"] for entry in result["value"]}
    assert list(entries) == ["research.find_sources", "research.summarize", "peer"]
    assert entries["research.summarize"] == {
        "type": "object",
        "properties": {"text": {"type": "string", "title": "Text"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    assert entries["research.find_sources"]["required"] == ["topic"]
    assert entries["peer"] is None
    # Left out as itself, not skipped as unusable.
    assert not [r for r in caplog.records if r.name.startswith("agentframe")]
