"""Opt-in: list a real Foundry project's A2A agents and fetch one agent's card.

Needs a Foundry project with one prompt agent that has incoming A2A enabled,
and ``az login`` as an identity with the roles in docs/api/discovery.md
("FoundryAgentRegistry"). Default ``uv run pytest`` runs and CI skip it::

    AGENTFRAME_TEST_FOUNDRY_AGENTS=1 \\
        AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=<project-endpoint> \\
        AGENTFRAME_FOUNDRY_A2A_AGENT=<agent-name> uv run pytest -m foundry

The calls only read: nothing in the project is created or changed.
"""

import os

import pytest
from azure.identity.aio import AzureCliCredential

from agentframe import AgentDirectory, AgentQuery, FoundryAgentRegistry

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
