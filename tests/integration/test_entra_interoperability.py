"""End-to-end: the official A2A client calls an ``EntraIDAuth`` host with a token.

The offline tests sign tokens with ``FakeEntra``'s local keys. The ``entra``
test uses a real Microsoft Entra token from the Azure CLI, so default
``uv run pytest`` runs and CI skip it. It needs an existing app registration
that exposes an API (see docs/api/authorization.md#entraidauth for the one-time
setup) and ``az login``::

    AGENTFRAME_TEST_ENTRA=1 AGENTFRAME_ENTRA_TENANT_ID=<tenant-id> \\
        AGENTFRAME_ENTRA_AUDIENCE=<api-client-id> uv run pytest -m entra
"""

import json
import os
import subprocess

import httpx
import pytest
from a2a.client import A2ACardResolver, A2AClientError
from a2a.helpers import get_artifact_text
from a2a.types import TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    EntraIDAuth,
    HostConfig,
    RunContext,
    agent,
    skill,
)
from fake_entra import API_CLIENT_ID, CALLER_OID, METADATA_URL, TENANT_ID, FakeEntra
from local_server import ServeLocally


@agent(version="1.0.0")
class Writer(Agent):
    """Writes documents for verified callers."""

    @skill("whoami", tags=["identity"], requires_permissions=["documents:read"])
    async def whoami(self, ctx: RunContext) -> str:
        """Return the verified caller's ID."""
        assert ctx.principal is not None
        return ctx.principal.id


def writer_app(auth: EntraIDAuth, public_url: str) -> AgentApp:
    return AgentApp(
        agent=Writer(),
        host=HostConfig(
            public_url=public_url,
            auth=auth,
            # The fake's tokens carry this scope; grant it in the real tenant too.
            role_permissions={"Documents.Read": ["documents:read"]},
        ),
    )


async def test_the_official_client_calls_with_an_entra_bearer_token(
    serve: ServeLocally,
) -> None:
    entra = FakeEntra()
    auth = EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )

    async with (
        serve(lambda url: writer_app(auth, url)) as base_url,
        httpx.AsyncClient() as http,
    ):
        card = await A2ACardResolver(http, base_url).get_agent_card()
        async with await connect(base_url, entra.token()) as client:
            task = await call_skill(client, "whoami")

    scheme = card.security_schemes["entraId"].open_id_connect_security_scheme
    assert scheme.open_id_connect_url == METADATA_URL
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == CALLER_OID


async def test_an_invalid_token_gets_401_with_a_bearer_challenge(
    serve: ServeLocally,
) -> None:
    entra = FakeEntra()
    auth = EntraIDAuth(
        tenant_id=TENANT_ID, audience=API_CLIENT_ID, transport=entra.transport
    )
    expired = entra.token(exp=1)

    async with (
        serve(lambda url: writer_app(auth, url)) as base_url,
        await connect(base_url, expired) as client,
    ):
        with pytest.raises(A2AClientError) as raised:
            await call_skill(client, "whoami")

    http_error = raised.value.__cause__
    assert isinstance(http_error, httpx.HTTPStatusError)
    assert http_error.response.status_code == 401
    assert http_error.response.headers["WWW-Authenticate"] == "Bearer"


def required(name: str) -> str:
    """Return an environment variable that the enabled Entra test needs."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"AGENTFRAME_TEST_ENTRA=1 also needs {name}")
    return value


def azure_cli_token(tenant_id: str, resource: str) -> str:
    """Get an access token for ``resource`` from the signed-in Azure CLI."""
    command = ["az", "account", "get-access-token", "--output", "json"]
    result = subprocess.run(
        [*command, "--tenant", tenant_id, "--resource", resource],
        capture_output=True,
        text=True,
        check=True,
    )
    token: str = json.loads(result.stdout)["accessToken"]
    return token


@pytest.mark.entra
@pytest.mark.skipif(
    os.environ.get("AGENTFRAME_TEST_ENTRA") != "1", reason="set AGENTFRAME_TEST_ENTRA=1"
)
async def test_a_real_entra_token_authenticates(serve: ServeLocally) -> None:
    tenant_id = required("AGENTFRAME_ENTRA_TENANT_ID")
    audience = required("AGENTFRAME_ENTRA_AUDIENCE")
    resource = os.environ.get("AGENTFRAME_ENTRA_RESOURCE", f"api://{audience}")
    auth = EntraIDAuth(tenant_id=tenant_id, audience=audience)

    token = azure_cli_token(tenant_id, resource)

    async with (
        serve(lambda url: writer_app(auth, url)) as base_url,
        await connect(base_url, token) as client,
    ):
        task = await call_skill(client, "whoami")

    caller = await auth.authenticate({"Authorization": f"Bearer {token}"})
    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == caller.id
