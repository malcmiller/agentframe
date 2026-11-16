"""Opt-in: list a real Foundry project's A2A connections and fetch one card.

Needs a Foundry project with one A2A (``RemoteA2A``) connection whose target
serves an A2A 1.0 Agent Card, and ``az login`` as an identity with the roles in
docs/api/discovery.md ("FoundryA2AToolRegistry"). Default ``uv run pytest``
runs and CI skip it::

    AGENTFRAME_TEST_FOUNDRY_A2A_TOOLS=1 \\
        AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=<project-endpoint> \\
        AGENTFRAME_FOUNDRY_A2A_CONNECTION=<connection-name> \\
        uv run pytest -m foundry tests/integration/test_foundry_a2a_tools.py

The test only reads: nothing in the project is created or changed, no agent is
sent a message, and the connection's stored credentials are never requested.
"""

import os

import pytest
from azure.identity.aio import AzureCliCredential

from agentframe import AgentDirectory, AgentQuery, FoundryA2AToolRegistry

pytestmark = [
    pytest.mark.foundry,
    pytest.mark.skipif(
        os.environ.get("AGENTFRAME_TEST_FOUNDRY_A2A_TOOLS") != "1",
        reason="set AGENTFRAME_TEST_FOUNDRY_A2A_TOOLS=1",
    ),
]


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"Set {name} to run this Foundry A2A connections test")
    return value


async def test_a_real_project_lists_its_a2a_connection_and_serves_its_card() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    connection_name = required_env("AGENTFRAME_FOUNDRY_A2A_CONNECTION")

    # The default 10 s timeout can expire while a cold ``az`` starts.
    async with AzureCliCredential(process_timeout=60) as credential:
        source = FoundryA2AToolRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        listed = await source.list(AgentQuery())
        reference = await source.get(connection_name)
        async with AgentDirectory(sources=[source]) as agents:
            found = await agents.get(connection_name)

    assert reference in listed
    assert found.reference == reference
    assert found.card.name
