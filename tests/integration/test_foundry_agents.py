"""Opt-in: list a real Foundry project's A2A agents, fetch one card, call it and
evaluate it.

The Foundry agent is also the real non-AgentFrame A2A server of the #94
interoperability acceptance and the #159 evaluation acceptance;
``test_multi_agent_interop.py`` and ``test_evaluation_interop.py`` cover the
same cases against a plain SDK peer in CI.

Needs a Foundry project with one prompt agent that has incoming A2A enabled,
and ``az login`` as an identity with the roles in docs/api/discovery.md
("FoundryAgentRegistry"). Default ``uv run pytest`` runs and CI skip it::

    AGENTFRAME_TEST_FOUNDRY_AGENTS=1 \\
        AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=<project-endpoint> \\
        AGENTFRAME_FOUNDRY_A2A_AGENT=<agent-name> uv run pytest -m foundry

Nothing in the project is created or changed; two tests send the agent one
short message each, which costs a little, and the ``call`` tests send nothing.
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
    RemoteAgentError,
    RunContext,
    Runtime,
    Service,
    agent,
    skill,
)
from agentframe.discovery.foundry import FOUNDRY_SCOPE
from agentframe.evaluation import A2ATarget, Case, CaseStatus, Evaluation

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


def cli_credential() -> AzureCliCredential:
    # The default 10 s timeout can expire while a cold ``az`` starts.
    return AzureCliCredential(process_timeout=60)


async def test_a_real_project_lists_its_a2a_agent_and_serves_its_card() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")

    async with cli_credential() as credential:
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

    async with cli_credential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        token = EntraAppCredential(credential, scope=FOUNDRY_SCOPE)
        async with AgentDirectory(sources=[source]) as agents:
            runtime = Runtime(
                directory=agents, remotes={agent_name: Service(credential=token)}
            )
            await runtime.validate_remotes(Planner())
            result = await runtime.run(
                Planner(), skill="plan", arguments={"goal": "Reply with: ready"}
            )

    assert isinstance(result.value, str)
    assert result.value
    # Foundry agents report no AgentFrame usage, so it is unknown, not zero.
    assert result.usage.total_tokens is None


async def test_call_to_a_real_foundry_agent_is_refused_before_sending() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")
    target = Remote[Literal[agent_name]]  # type: ignore[valid-type]

    @agent(name="Planner", description="Calls a Foundry agent.", version="0.1.0")
    class Planner(Agent):
        @skill(id="plan", description="Call the agent.", tags=["planning"])
        async def plan(self, goal: str, peer: target, ctx: RunContext) -> str:
            answer: str = await peer.call("any_skill", goal=goal, output=str)
            return answer

    async with cli_credential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        token = EntraAppCredential(credential, scope=FOUNDRY_SCOPE)
        async with AgentDirectory(sources=[source]) as agents:
            runtime = Runtime(
                directory=agents, remotes={agent_name: Service(credential=token)}
            )
            with pytest.raises(RemoteAgentError, match="skill-call extension"):
                await runtime.invoke(Planner(), skill="plan", arguments={"goal": "x"})


def answered(case: Case[str], output: str) -> bool:
    return bool(output.strip())


async def test_an_evaluation_asks_a_real_foundry_agent_one_case() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")

    async with cli_credential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        token = EntraAppCredential(credential, scope=FOUNDRY_SCOPE)
        async with AgentDirectory(sources=[source]) as agents:
            evaluation = Evaluation(
                cases=[Case(id="ready", text="Reply with: ready")],
                target=A2ATarget(
                    agents, agent_name, identity=Service(credential=token)
                ),
                scorers=[answered],
            )
            report = await evaluation.run()

    report.require(min_pass_rate=1.0)
    # Foundry agents report no AgentFrame usage, so it is unknown, not zero.
    assert report.total_usage.total_tokens is None
    assert report.total_usage.unknown_cases == 1


async def test_an_evaluation_skill_case_is_refused_before_sending_to_foundry() -> None:
    project_endpoint = required_env("AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT")
    agent_name = required_env("AGENTFRAME_FOUNDRY_A2A_AGENT")

    async with cli_credential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=project_endpoint, credential=credential
        )
        token = EntraAppCredential(credential, scope=FOUNDRY_SCOPE)
        async with AgentDirectory(sources=[source]) as agents:
            evaluation = Evaluation(
                cases=[Case(id="any", arguments={"goal": "x"})],
                target=A2ATarget(
                    agents,
                    agent_name,
                    skill="any_skill",
                    identity=Service(credential=token),
                ),
                scorers=[answered],
            )
            [result] = (await evaluation.run()).results

    assert result.status is CaseStatus.ERRORED
    assert result.error_type == "RemoteAgentError"
