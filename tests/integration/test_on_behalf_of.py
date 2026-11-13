"""An orchestrator calls an Entra-protected specialist on behalf of the user.

Both agents are ``AgentApp``s served over localhost HTTP with ``EntraIDAuth``,
and the end user calls the orchestrator with the official A2A SDK client.
``FakeEntra`` signs every token and ``FakeTokenEndpoint`` answers the
on-behalf-of exchange that ``azure-identity`` sends, so nothing leaves the
process. The specialist records every request's headers, to prove only the
exchanged token reaches it.

The ``entra`` test at the end uses two real app registrations instead; see
docs/api/authorization.md#onbehalfof for the one-time setup. It never runs in
CI.
"""

import dataclasses
import json
import logging
import os
import subprocess
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal

import pytest
from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    APIKey,
    APIKeyAuth,
    DefinitionError,
    EntraIDAuth,
    FileAgentRegistry,
    HostConfig,
    OnBehalfOf,
    PermissionDeniedError,
    Remote,
    RunContext,
    Runtime,
    VerifiedCaller,
    agent,
    skill,
)
from fake_entra import (
    API_CLIENT_ID,
    CALLER_OID,
    TARGET_CLIENT_ID,
    TENANT_ID,
    FakeEntra,
    FakeTokenEndpoint,
)
from local_server import AppFactory, ServeLocally

type Research = Remote[Literal["research-agent"]]

TARGET_SCOPE = f"api://{TARGET_CLIENT_ID}/.default"
PLANNER_SECRET = "planner-client-secret"


@agent(name="Research", description="Says who it acts for.", version="0.1.0")
class Specialist(Agent):
    @skill(
        id="who",
        description="Name the user.",
        tags=["research"],
        requires_permissions=["documents:read"],
    )
    async def who(self, ctx: RunContext) -> str:
        assert ctx.principal is not None
        return f"Called for {ctx.principal.id}"


class RecordingAuth(EntraIDAuth):
    """The specialist's ``EntraIDAuth``, keeping every request's headers."""

    def __init__(self, entra: FakeEntra) -> None:
        super().__init__(
            tenant_id=TENANT_ID, audience=TARGET_CLIENT_ID, transport=entra.transport
        )
        self.seen: list[dict[str, str]] = []

    async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
        self.seen.append(dict(headers))
        return await super().authenticate(headers)


contexts: list[RunContext] = []


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        contexts.append(ctx)
        answer: str = await research.call("who")
        return answer


@pytest.fixture
def entra() -> FakeEntra:
    return FakeEntra()


@pytest.fixture
def token_endpoint(entra: FakeEntra) -> FakeTokenEndpoint:
    return FakeTokenEndpoint(entra)


@pytest.fixture
def specialist_auth(entra: FakeEntra) -> RecordingAuth:
    return RecordingAuth(entra)


@pytest.fixture
async def directory(
    serve: ServeLocally, specialist_auth: RecordingAuth, tmp_path: Path
) -> AsyncIterator[AgentDirectory]:
    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Specialist(),
            host=HostConfig(
                public_url=public_url,
                auth=specialist_auth,
                role_permissions={"Documents.Read": ["documents:read"]},
            ),
        )

    async with serve(app) as url:
        agents = tmp_path / "agents.yaml"
        card_url = f"{url}/.well-known/agent-card.json"
        agents.write_text(f"- name: research-agent\n  url: {card_url}\n")
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as found:
            yield found


def planner_auth(entra: FakeEntra, token_endpoint: FakeTokenEndpoint) -> EntraIDAuth:
    return EntraIDAuth(
        tenant_id=TENANT_ID,
        audience=API_CLIENT_ID,
        client_id=API_CLIENT_ID,
        client_credential=PLANNER_SECRET,
        transport=entra.transport,
        token_transport=token_endpoint,
    )


@pytest.fixture
def planner_app(
    directory: AgentDirectory, entra: FakeEntra, token_endpoint: FakeTokenEndpoint
) -> AppFactory:
    auth = planner_auth(entra, token_endpoint)

    def app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Planner(),
            host=HostConfig(public_url=public_url, auth=auth),
            directory=directory,
            remotes={"research-agent": OnBehalfOf(scope=TARGET_SCOPE)},
        )

    return app


async def plan_as(
    serve: ServeLocally, planner_app: AppFactory, user_token: str
) -> Task:
    """Call the planner's ``plan`` skill as the user with ``user_token``."""
    async with (
        serve(planner_app) as url,
        await connect(url, user_token) as client,
    ):
        return await call_skill(client, "plan", {"goal": "tides"})


async def test_the_specialist_acts_for_the_user_with_an_exchanged_token(
    serve: ServeLocally,
    planner_app: AppFactory,
    entra: FakeEntra,
    token_endpoint: FakeTokenEndpoint,
    specialist_auth: RecordingAuth,
) -> None:
    user_token = entra.token()

    task = await plan_as(serve, planner_app, user_token)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == f"Called for {CALLER_OID}"
    # One exchange per run, of the incoming token, for the configured scope.
    [exchange] = token_endpoint.exchanges
    assert exchange["assertion"] == user_token
    assert exchange["scope"] == TARGET_SCOPE
    assert exchange["requested_token_use"] == "on_behalf_of"
    assert exchange["client_id"] == API_CLIENT_ID
    # The specialist got only the new token, on every request.
    [new_token] = token_endpoint.issued
    assert specialist_auth.seen
    for headers in specialist_auth.seen:
        assert headers["authorization"] == f"Bearer {new_token}"
        assert user_token not in str(headers)


async def test_a_failed_exchange_fails_the_call_without_falling_back(
    serve: ServeLocally,
    planner_app: AppFactory,
    entra: FakeEntra,
    token_endpoint: FakeTokenEndpoint,
    specialist_auth: RecordingAuth,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token_endpoint.failing = True
    user_token = entra.token()
    caplog.set_level(logging.DEBUG)

    task = await plan_as(serve, planner_app, user_token)

    assert task.status.state == TaskState.TASK_STATE_FAILED
    reply = get_message_text(task.status.message)
    assert "credential could not provide its headers" in reply
    assert token_endpoint.exchanges
    # Nothing was sent: no service identity, no forwarded user token.
    assert specialist_auth.seen == []
    for secret in (user_token, PLANNER_SECRET):
        assert secret not in reply
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "claims", [{"scp": None}, {"idtyp": "app"}], ids=["no-scp", "idtyp-app"]
)
async def test_an_app_only_caller_is_denied(
    serve: ServeLocally,
    planner_app: AppFactory,
    entra: FakeEntra,
    token_endpoint: FakeTokenEndpoint,
    specialist_auth: RecordingAuth,
    claims: dict[str, Any],
) -> None:
    task = await plan_as(serve, planner_app, entra.token(**claims))

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert "the caller is not an Entra user" in get_message_text(task.status.message)
    assert token_endpoint.exchanges == []
    assert specialist_auth.seen == []


async def test_tokens_never_reach_logs_or_the_run_context(
    serve: ServeLocally,
    planner_app: AppFactory,
    entra: FakeEntra,
    token_endpoint: FakeTokenEndpoint,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    contexts.clear()
    user_token = entra.token()

    task = await plan_as(serve, planner_app, user_token)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    [ctx] = contexts
    fields = {f.name: getattr(ctx, f.name) for f in dataclasses.fields(ctx)}
    for secret in (user_token, *token_endpoint.issued, PLANNER_SECRET):
        assert secret not in repr(fields)
        assert secret not in caplog.text


async def test_a_local_run_has_no_user_to_act_for(directory: AgentDirectory) -> None:
    runtime = Runtime(
        directory=directory, remotes={"research-agent": OnBehalfOf(scope=TARGET_SCOPE)}
    )

    with pytest.raises(PermissionDeniedError, match="not an Entra user"):
        await runtime.run(Planner(), skill="plan", arguments={"goal": "tides"})


def unopened_directory(tmp_path: Path) -> AgentDirectory:
    return AgentDirectory(sources=[FileAgentRegistry(tmp_path / "agents.yaml")])


def test_api_key_callers_cannot_use_on_behalf_of(tmp_path: Path) -> None:
    with pytest.raises(DefinitionError, match="needs EntraIDAuth callers"):
        AgentApp(
            agent=Planner(),
            host=HostConfig(
                public_url="http://127.0.0.1:1",
                auth=APIKeyAuth(keys={"k": APIKey(caller_id="user")}),
            ),
            directory=unopened_directory(tmp_path),
            remotes={"research-agent": OnBehalfOf(scope=TARGET_SCOPE)},
        )


def test_entra_without_a_client_credential_cannot_use_on_behalf_of(
    tmp_path: Path,
) -> None:
    with pytest.raises(DefinitionError, match="client_credential"):
        AgentApp(
            agent=Planner(),
            host=HostConfig(
                public_url="http://127.0.0.1:1",
                auth=EntraIDAuth(tenant_id=TENANT_ID, audience=API_CLIENT_ID),
            ),
            directory=unopened_directory(tmp_path),
            remotes={"research-agent": OnBehalfOf(scope=TARGET_SCOPE)},
        )


# Real Microsoft Entra (opt-in, two app registrations)


def required(name: str) -> str:
    """Return an environment variable that the enabled OBO test needs."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"AGENTFRAME_TEST_ENTRA_OBO=1 also needs {name}")
    return value


def azure_cli_token(tenant_id: str, scope: str) -> str:
    """Get a delegated user token for ``scope`` from the signed-in Azure CLI."""
    command = ["az", "account", "get-access-token", "--output", "json"]
    result = subprocess.run(
        [*command, "--tenant", tenant_id, "--scope", scope],
        capture_output=True,
        text=True,
        check=True,
    )
    token: str = json.loads(result.stdout)["accessToken"]
    return token


@pytest.mark.entra
@pytest.mark.skipif(
    os.environ.get("AGENTFRAME_TEST_ENTRA_OBO") != "1",
    reason="set AGENTFRAME_TEST_ENTRA_OBO=1",
)
async def test_a_real_on_behalf_of_exchange(
    serve: ServeLocally, tmp_path: Path
) -> None:
    tenant_id = required("AGENTFRAME_ENTRA_TENANT_ID")
    target = required("AGENTFRAME_ENTRA_AUDIENCE")
    planner = required("AGENTFRAME_ENTRA_OBO_CLIENT_ID")
    secret = required("AGENTFRAME_ENTRA_OBO_CLIENT_SECRET")
    user_token = azure_cli_token(tenant_id, f"api://{planner}/.default")

    def specialist(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Specialist(),
            host=HostConfig(
                public_url=public_url,
                auth=EntraIDAuth(tenant_id=tenant_id, audience=target),
                role_permissions={"Documents.Read": ["documents:read"]},
            ),
        )

    async with serve(specialist) as specialist_url:
        agents = tmp_path / "agents.yaml"
        card_url = f"{specialist_url}/.well-known/agent-card.json"
        agents.write_text(f"- name: research-agent\n  url: {card_url}\n")
        async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as directory:
            auth = EntraIDAuth(
                tenant_id=tenant_id,
                audience=planner,
                client_id=planner,
                client_credential=secret,
            )

            def planner_app(public_url: str) -> AgentApp:
                return AgentApp(
                    agent=Planner(),
                    host=HostConfig(public_url=public_url, auth=auth),
                    directory=directory,
                    remotes={
                        "research-agent": OnBehalfOf(scope=f"api://{target}/.default")
                    },
                )

            async with (
                serve(planner_app) as planner_url,
                await connect(planner_url, user_token) as client,
            ):
                task = await call_skill(client, "plan", {"goal": "tides"})

    user = await auth.authenticate({"Authorization": f"Bearer {user_token}"})
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == f"Called for {user.id}"
