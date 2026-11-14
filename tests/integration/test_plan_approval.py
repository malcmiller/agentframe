"""The propose/execute approval pattern of ``examples/plan_approval.py`` over A2A.

The approvals agent is the example's, with a scripted planning model. Its
research agent is a localhost ``AgentApp`` that records every call, so each
test can see that nothing was sent before a plan was approved and checked.
Callers use the official A2A client with the example's two API keys.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from a2a.client import Client
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    HostConfig,
    ModelCapabilities,
    RunContext,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from examples import plan_approval
from local_server import ServeLocally

REQUESTER_KEY = "requester-key"
REVIEWER_KEY = "reviewer-key"
PLANNING_MODEL = ModelCapabilities(context_window=8192, structured_output=True)


@agent(name="Research", version="0.1.0")
class Research(Agent):
    """Records every call it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    @skill(id="find_sources", tags=["research"])
    async def find_sources(self, topic: str, ctx: RunContext) -> list[str]:
        """List sources on a topic."""
        self.calls.append(("find_sources", topic))
        return [f"{topic} source"]

    @skill(id="brief", tags=["research"])
    async def brief(self, question: str, ctx: RunContext) -> str:
        """Write a short brief."""
        self.calls.append(("brief", question))
        return f"Brief: {question}"


class Registered(AgentRegistry):
    """Lists the research agent under the name the example declares."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def get(self, agent_id: str) -> AgentReference:
        raise NotImplementedError

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        card_url = f"{self.url}/.well-known/agent-card.json"
        return [AgentReference("research-agent", card_url)]


def planned(step_id: str, use: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One step as the model writes it: arguments are a JSON string."""
    return {"id": step_id, "use": use, "arguments": json.dumps(arguments), "after": []}


TIDES_PLAN = json.dumps(
    {
        "steps": [
            planned("tides", "research-agent.find_sources", {"topic": "tides"}),
            planned("brief", "research-agent.brief", {"question": "Why tides?"}),
        ],
        "result": "brief",
        "reason": None,
    }
)

type Approvals = Callable[[], AbstractAsyncContextManager[tuple[Client, Client]]]


@pytest.fixture
def research() -> Research:
    return Research()


@pytest.fixture
def approvals(
    serve: ServeLocally, research: Research, monkeypatch: pytest.MonkeyPatch
) -> Approvals:
    """Serve both agents; yield a requester's and a reviewer's client."""
    monkeypatch.setenv("APPROVALS_REQUESTER_KEY", REQUESTER_KEY)
    monkeypatch.setenv("APPROVALS_REVIEWER_KEY", REVIEWER_KEY)
    monkeypatch.delenv("RESEARCH_API_KEY", raising=False)

    @asynccontextmanager
    async def serving() -> AsyncIterator[tuple[Client, Client]]:
        def research_app(public_url: str) -> AgentApp:
            host = HostConfig(public_url=public_url, auth=AllowAnonymous())
            return AgentApp(agent=research, host=host)

        async with serve(research_app) as research_url:
            directory = AgentDirectory(sources=[Registered(research_url)])

            def approvals_app(public_url: str) -> AgentApp:
                monkeypatch.setenv("APPROVALS_PUBLIC_URL", public_url)
                model = ScriptedModel([TIDES_PLAN], capabilities=PLANNING_MODEL)
                return plan_approval.create_app(model, directory)

            async with directory, serve(approvals_app) as url:
                requester = await connect(url, REQUESTER_KEY)
                reviewer = await connect(url, REVIEWER_KEY)
                yield requester, reviewer

    return serving


def value(task: Task) -> Any:
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(task.artifacts[0].parts)
    return data["value"]


def status_text(task: Task) -> str:
    return get_message_text(task.status.message)


async def test_a_proposed_plan_runs_only_once_a_reviewer_sends_it_back(
    approvals: Approvals, research: Research
) -> None:
    async with approvals() as (requester, reviewer):
        proposed = await call_skill(requester, "propose", {"goal": "Explain tides"})
        plan = value(proposed)
        assert research.calls == []

        executed = await call_skill(reviewer, "execute", {"plan": plan})

    assert plan["steps"][0]["use"] == "research-agent.find_sources"
    assert executed.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(executed.artifacts[0]) == "Brief: Why tides?"
    assert research.calls == [("find_sources", "tides"), ("brief", "Why tides?")]


async def test_a_caller_without_plans_run_cannot_execute_a_plan(
    approvals: Approvals, research: Research
) -> None:
    async with approvals() as (requester, _):
        plan = value(await call_skill(requester, "propose", {"goal": "Tides"}))
        task = await call_skill(requester, "execute", {"plan": plan})

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert research.calls == []


async def test_a_tampered_plan_naming_an_entry_outside_the_catalog_is_rejected(
    approvals: Approvals, research: Research
) -> None:
    async with approvals() as (requester, reviewer):
        plan = value(await call_skill(requester, "propose", {"goal": "Tides"}))
        plan["steps"][0]["use"] = "billing-agent.refund"
        task = await call_skill(reviewer, "execute", {"plan": plan})

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert status_text(task) == (
        "Plan step 'tides' field 'use' names no entry of the skill catalog"
    )
    assert research.calls == []


async def test_the_plan_check_refuses_a_plan_with_two_briefs(
    approvals: Approvals, research: Research
) -> None:
    brief = {"id": "again", "use": "research-agent.brief"}
    async with approvals() as (requester, reviewer):
        plan = value(await call_skill(requester, "propose", {"goal": "Tides"}))
        plan["steps"].append({**brief, "arguments": {"question": "Why waves?"}})
        task = await call_skill(reviewer, "execute", {"plan": plan})

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert status_text(task) == "The plan was blocked by an execution policy check"
    assert research.calls == []
