"""An ``AgentDirectory`` resolving a real AgentFrame agent over localhost HTTP.

The directory creates and closes its own HTTP client, reads the agent from a
YAML registry file and validates the card the hosted agent actually serves.
"""

from pathlib import Path

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


@agent(name="Research", description="Finds sources.", version="0.1.0")
class Research(Agent):
    @skill(
        id="find_sources",
        name="Find sources",
        description="Find sources on a topic.",
        tags=["research"],
    )
    async def find_sources(self, topic: str, ctx: RunContext) -> str:
        return f"Sources on {topic}"


def research_app(base_url: str) -> AgentApp:
    return AgentApp(
        agent=Research(), host=HostConfig(public_url=base_url, auth=AllowAnonymous())
    )


async def test_directory_resolves_a_hosted_agents_card(
    serve: ServeLocally, tmp_path: Path
) -> None:
    async with serve(research_app) as base_url:
        registry_file = tmp_path / "agents.yaml"
        registry_file.write_text(
            f"- name: research-agent\n  url: {base_url}/.well-known/agent-card.json\n"
        )

        async with AgentDirectory(sources=[FileAgentRegistry(registry_file)]) as d:
            found = await d.get("research-agent")

    assert found.card.name == "Research"
    assert [s.id for s in found.card.skills] == ["find_sources"]
    assert found.interface_url == f"{base_url}/a2a"
    assert found.stale is False
