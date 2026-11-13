"""Opt-in: list a real Foundry project's A2A agents, fetch one card and call it.

Needs a Foundry project with one prompt agent that has incoming A2A enabled,
and ``az login`` as an identity with the roles in docs/api/discovery.md
("FoundryAgentRegistry"). Default ``uv run pytest`` runs and CI skip it::

    AGENTFRAME_TEST_FOUNDRY_AGENTS=1 \\
        AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=<project-endpoint> \\
        AGENTFRAME_FOUNDRY_A2A_AGENT=<agent-name> uv run pytest -m foundry

Nothing in the project is created or changed; the last test sends the agent
one short message, which costs a little.
"""

import os
from typing import Literal

import pytest
from azure.identity.aio import AzureCliCredential

from agentframe import (
    Agent,
    AgentDirectory,
    AgentQuery,
    EntraAppCredential,
    FoundryAgentRegistry,
    Remote,
    RunContext,
    Runtime,
    Service,
    agent,
    skill,
)
from agentframe.discovery.foundry import FOUNDRY_SCOPE

pytestmark = [
    pytest.mark.foundry,
    pytest.mark.skipif(
        os.environ.get("AGENTFRAME_TEST_FOUNDRY_AGENTS") != "1",
        reason="set AGENTFRAME_TEST_FOUNDRY_AGENTS=1",
    ),
]


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Set {name} to run this Foundry registry test")
    return value


async def test_a_real_project_lists_its_a2a_agent_and_serves_its_card() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")

    async with AzureCliCredential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        listed = await source.list(AgentQuery())
        async with AgentDirectory(sources=[source]) as agents:
            found = await agents.get(agent_name)

    assert agent_name in [reference.name for reference in listed]
    assert found.reference.card_url.endswith(
        f"/agents/{agent_name}/endpoint/protocols/a2a/agentCard/v1.0"
    )
    assert found.interface_url.startswith(project_endpoint.rstrip("/"))
    assert found.card.name


async def test_an_orchestrator_asks_a_real_foundry_agent_with_its_app_token() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")
    # The name is only known at run time, so the binding is built here.
    target = Remote[Literal[agent_name]]  # type: ignore[valid-type]

    @agent(name="Planner", description="Asks a Foundry agent.", version="0.1.0")
    class Planner(Agent):
        @skill(id="plan", description="Ask the agent.", tags=["planning"])
        async def plan(self, goal: str, peer: target, ctx: RunContext) -> str:
            return await peer.ask(goal)

    async with AzureCliCredential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        token = EntraAppCredential(credential, scope=FOUNDRY_SCOPE)
        async with AgentDirectory(sources=[source]) as agents:
            runtime = Runtime(
                directory=agents, remotes={agent_name: Service(credential=token)}
            )
            await runtime.validate_remotes(Planner())
            answer = await runtime.invoke(
                Planner(), skill="plan", arguments={"goal": "Reply with: ready"}
            )

    assert isinstance(answer, str)
    assert answer
