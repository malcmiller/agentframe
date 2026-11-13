"""An orchestrator calls an API-key-protected specialist with its own identity.

Both agents are ``AgentApp``s served over localhost HTTP, and the end user
calls the orchestrator with the official A2A SDK client. The specialist
records every request's headers, to prove the end user's credentials never
reach it.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Literal

import httpx
import pytest
from a2a.client import Client
from a2a.helpers import get_artifact_text
from a2a.types import Message, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    APIKey,
    APIKeyAuth,
    APIKeyCredential,
    FileAgentRegistry,
    HostConfig,
    Remote,
    RoutingContext,
    RunContext,
    Service,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    VerifiedCaller,
    agent,
    skill,
)
from local_server import ServeLocally, lifespan_startup

type Research = Remote[Literal["research-agent"]]

PLANNER_KEY = "planner-service-key"
USER_KEY = "end-user-key"
USER_TOKEN = "Bearer end-user-token"


@agent(name="Research", description="Says who called it.", version="0.1.0")
class Specialist(Agent):
    @skill(id="who", description="Name the caller.", tags=["research"])
    async def who(self, ctx: RunContext) -> str:
        assert ctx.principal is not None
        return f"Called by {ctx.principal.id}"


class AlwaysWho(SkillRouter):
    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall:
        return SkillCall("who", {})


class RecordingAuth(APIKeyAuth):
    """Accepts only the planner's key and keeps every request's headers."""

    def __init__(self) -> None:
        super().__init__(keys={PLANNER_KEY: APIKey(caller_id="planner-agent")})
        self.seen: list[dict[str, str]] = []

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        self.seen.append(dict(headers))
        return await super().authenticate(headers)


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        return await research.ask(goal)


@pytest.fixture
def specialist_auth() -> RecordingAuth:
    return RecordingAuth()


@pytest.fixture
async def directory(
    serve: ServeLocally, specialist_auth: RecordingAuth, tmp_path: Path
) -> AsyncIterator[AgentDirectory]:
    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Specialist(),
            host=HostConfig(public_url=public_url, auth=specialist_auth),
            router=AlwaysWho(),
        )

    async with serve(app) as url:
        agents = tmp_path / "agents.yaml"
        card_url = f"{url}/.well-known/agent-card.json"
        agents.write_text(f"- name: research-agent\n  url: {card_url}\n")
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as found:
            yield found


def planner_app(public_url: str, directory: AgentDirectory, key: str) -> AgentApp:
    return AgentApp(
        agent=Planner(),
        host=HostConfig(
            public_url=public_url,
            auth=APIKeyAuth(keys={USER_KEY: APIKey(caller_id="end-user")}),
        ),
        directory=directory,
        remotes={"research-agent": Service(credential=APIKeyCredential(key))},
    )


@pytest.fixture
async def planner(
    serve: ServeLocally, directory: AgentDirectory
) -> AsyncIterator[Client]:
    async with (
        serve(lambda url: planner_app(url, directory, PLANNER_KEY)) as url,
        await connect(url, USER_KEY) as client,
    ):
        yield client


async def test_the_specialist_sees_the_orchestrators_identity(
    planner: Client,
) -> None:
    task = await call_skill(planner, "plan", {"goal": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Called by planner-agent"


async def test_the_end_users_credentials_never_reach_the_specialist(
    serve: ServeLocally, directory: AgentDirectory, specialist_auth: RecordingAuth
) -> None:
    # The user's client also sends a bearer token, which must not travel on.
    user_http = httpx.AsyncClient(headers={"Authorization": USER_TOKEN})
    async with (
        user_http,
        serve(lambda url: planner_app(url, directory, PLANNER_KEY)) as url,
        await connect(url, USER_KEY, http=user_http) as client,
    ):
        task = await call_skill(client, "plan", {"goal": "tides"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert specialist_auth.seen
    for headers in specialist_auth.seen:
        values = set(headers.values())
        assert USER_KEY not in values
        assert USER_TOKEN not in values
        assert headers["x-api-key"] == PLANNER_KEY


async def test_startup_fails_without_a_credential_for_a_protected_specialist(
    directory: AgentDirectory,
) -> None:
    app = AgentApp(
        agent=Planner(),
        host=HostConfig(
            public_url="http://127.0.0.1:1",
            auth=APIKeyAuth(keys={USER_KEY: APIKey(caller_id="end-user")}),
        ),
        directory=directory,
    )

    reply = await lifespan_startup(app)

    assert reply["type"] == "lifespan.startup.failed"
    assert "Agent 'research-agent' requires credentials" in reply["message"]


async def test_a_wrong_key_is_refused_by_the_specialist(
    serve: ServeLocally, directory: AgentDirectory
) -> None:
    async with (
        serve(lambda url: planner_app(url, directory, "wrong-key")) as url,
        await connect(url, USER_KEY) as client,
    ):
        task = await call_skill(client, "plan", {"goal": "tides"})

    assert task.status.state == TaskState.TASK_STATE_FAILED
