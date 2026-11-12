"""Tests for hosting an agent over A2A with ``AgentApp``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by uvicorn.
"""

import asyncio
import socket
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest, TaskState
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, TransportProtocol
from pydantic import BaseModel

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    AuthorizationPolicy,
    DefinitionError,
    HostConfig,
    Inject,
    Principal,
    RunContext,
    RunLimits,
    Runtime,
    Services,
    SkillDefinition,
    VerifiedCaller,
    agent,
    skill,
)
from agentframe.card import build_agent_card
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
READER_KEY = "reader-test-key"
ADMIN_KEY = "admin-test-key"


class Greeting(BaseModel):
    text: str


class Clock:
    """An application service injected into skills."""

    def now(self) -> str:
        return "noon"


@agent(version="1.0.0")
class Greeter(Agent):
    """Greets people."""

    @skill("greet", tags=["test"])
    async def greet(self, name: str, ctx: RunContext) -> str:
        """Greet someone."""
        return f"Hello, {name}!"

    @skill("greet_formally", tags=["test"])
    async def greet_formally(self, name: str, ctx: RunContext) -> Greeting:
        """Greet someone formally."""
        return Greeting(text=f"Good day, {name}.")

    @skill("time", requires_permissions=["clock:read"], tags=["test"])
    async def time(self, ctx: RunContext, clock: Inject[Clock]) -> str:
        """Tell the time."""
        return clock.now()

    @skill("slow", limits=RunLimits(timeout_seconds=0.05), tags=["test"])
    async def slow(self, ctx: RunContext) -> str:
        """Take too long."""
        await asyncio.sleep(5)
        return "too late"

    @skill("broken", tags=["test"])
    async def broken(self, ctx: RunContext) -> str:
        """Fail with a secret in the error."""
        raise RuntimeError("database password is hunter2")


def api_key_host(public_url: str) -> HostConfig:
    return HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(
            keys={
                READER_KEY: APIKey("reader"),
                ADMIN_KEY: APIKey("admin", roles=["Admin"]),
            }
        ),
        role_permissions={"Admin": ["clock:read"]},
    )


def clock_services() -> Services:
    services = Services()
    services.add(Clock, Clock())
    return services


def greeter_app(public_url: str) -> AgentApp:
    return AgentApp(
        agent=Greeter(), host=api_key_host(public_url), services=clock_services()
    )


@pytest.fixture
async def http(serve: ServeLocally) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        serve(greeter_app) as base_url,
        httpx.AsyncClient(base_url=base_url) as client,
    ):
        yield client


def rpc(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}


def send_skill(
    skill_id: str, arguments: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    # A message needs at least one part; a skill without arguments gets text.
    part = {"text": "run"} if arguments is None else {"data": dict(arguments)}
    message = {
        "messageId": "m-1",
        "role": "ROLE_USER",
        "parts": [part],
        "metadata": {"agentframe.skill": skill_id},
    }
    return rpc("SendMessage", {"message": message})


async def post(
    http: httpx.AsyncClient, body: Mapping[str, Any], key: str | None = READER_KEY
) -> httpx.Response:
    headers = {"A2A-Version": "1.0"}
    if key is not None:
        headers["X-API-Key"] = key
    return await http.post("/a2a", json=body, headers=headers)


async def run_task(
    http: httpx.AsyncClient,
    skill_id: str,
    arguments: Mapping[str, Any] | None = None,
    key: str = READER_KEY,
) -> dict[str, Any]:
    response = await post(http, send_skill(skill_id, arguments), key)
    assert response.status_code == 200
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


def status_text(task: Mapping[str, Any]) -> str:
    return " ".join(part["text"] for part in task["status"]["message"]["parts"])


# Serving the card and the A2A endpoint


async def test_serves_the_generated_card_without_credentials(
    http: httpx.AsyncClient,
) -> None:
    response = await http.get(AGENT_CARD_WELL_KNOWN_PATH)

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "Greeter"
    assert card["supportedInterfaces"][0]["url"] == f"{http.base_url}/a2a"
    assert card["securitySchemes"]["apiKey"]


async def test_official_client_runs_a_skill_over_http(
    http: httpx.AsyncClient,
) -> None:
    http.headers["X-API-Key"] = READER_KEY
    config = ClientConfig(
        httpx_client=http,
        streaming=False,
        supported_protocol_bindings=[TransportProtocol.JSONRPC],
    )
    card = build_agent_card(
        Greeter.definition(), api_key_host(str(http.base_url).rstrip("/"))
    )
    client = ClientFactory(config).create(card)
    message = Message(
        message_id="m-1",
        role=Role.ROLE_USER,
        parts=[new_data_part({"name": "Ada"})],
        metadata={"agentframe.skill": "greet"},
    )

    [response] = [
        response
        async for response in client.send_message(SendMessageRequest(message=message))
    ]

    assert response.task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(response.task.artifacts[0]) == "Hello, Ada!"


# Every request runs through the Runtime


async def test_text_result_is_a_text_artifact_with_the_invocation_id(
    http: httpx.AsyncClient,
) -> None:
    task = await run_task(http, "greet", {"name": "Ada"})

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"] == [{"text": "Hello, Ada!"}]
    assert task["metadata"]["invocation_id"]


async def test_structured_result_is_a_data_artifact(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "greet_formally", {"name": "Ada"})

    assert task["artifacts"][0]["parts"] == [
        {"data": {"value": {"text": "Good day, Ada."}}}
    ]


async def test_invalid_arguments_fail_without_echoing_the_value(
    http: httpx.AsyncClient,
) -> None:
    task = await run_task(http, "greet", {"name": ["secret-value"]})

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "name" in status_text(task)
    assert "secret-value" not in str(task["status"])
    assert task["metadata"]["invocation_id"]


async def test_unknown_skill_fails(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "missing")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "missing" in status_text(task)


async def test_message_without_a_skill_id_fails(http: httpx.AsyncClient) -> None:
    body = send_skill("greet", {"name": "Ada"})
    del body["params"]["message"]["metadata"]

    response = await post(http, body)

    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "agentframe.skill" in status_text(task)


async def test_permissions_come_from_verified_roles(http: httpx.AsyncClient) -> None:
    denied = await run_task(http, "time", key=READER_KEY)
    allowed = await run_task(http, "time", key=ADMIN_KEY)

    assert denied["status"]["state"] == "TASK_STATE_FAILED"
    assert "clock:read" in status_text(denied)
    assert allowed["artifacts"][0]["parts"] == [{"text": "noon"}]


async def test_skill_timeout_fails_the_task(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "slow")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "timeout" in status_text(task)


async def test_skill_exception_fails_with_a_safe_message(
    http: httpx.AsyncClient,
) -> None:
    task = await run_task(http, "broken")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert status_text(task) == "The skill failed."
    assert "hunter2" not in str(task)


async def test_authorization_policy_is_applied(serve: ServeLocally) -> None:
    class DenyAll:
        async def check(
            self,
            principal: Principal | None,
            skill: SkillDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            return False

    def make_app(public_url: str) -> AgentApp:
        policy: AuthorizationPolicy = DenyAll()
        return AgentApp(
            agent=Greeter(),
            host=api_key_host(public_url),
            services=clock_services(),
            authorization=policy,
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        task = await run_task(h, "greet", {"name": "Ada"})

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "denied" in status_text(task)


async def test_explicit_runtime_is_used(serve: ServeLocally) -> None:
    def make_app(public_url: str) -> AgentApp:
        runtime = Runtime(services=clock_services())
        return AgentApp(agent=Greeter(), host=api_key_host(public_url), runtime=runtime)

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        task = await run_task(h, "time", key=ADMIN_KEY)

    assert task["artifacts"][0]["parts"] == [{"text": "noon"}]


# Authentication


@pytest.mark.parametrize(
    "key",
    [pytest.param(None, id="missing"), pytest.param("wrong-key", id="invalid")],
)
async def test_bad_credentials_get_401_and_create_no_task(
    http: httpx.AsyncClient, key: str | None
) -> None:
    response = await post(http, send_skill("greet", {"name": "Ada"}), key=key)

    assert response.status_code == 401
    assert response.json() == {"error": "Authentication required."}
    tasks = await post(http, rpc("ListTasks", {}), key=READER_KEY)
    assert tasks.json()["result"].get("tasks", []) == []


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("SendMessage", send_skill("greet", {"name": "Ada"})["params"]),
        ("GetTask", {"id": "any-task"}),
        ("ListTasks", {}),
        ("CancelTask", {"id": "any-task"}),
    ],
)
async def test_every_task_operation_requires_authentication(
    http: httpx.AsyncClient, method: str, params: dict[str, Any]
) -> None:
    response = await post(http, rpc(method, params), key=None)

    assert response.status_code == 401


async def test_anonymous_callers_must_be_allowed_explicitly(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(public_url=public_url, auth=AllowAnonymous())
        return AgentApp(agent=Greeter(), host=host, services=clock_services())

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        greeted = await run_task(h, "greet", {"name": "Ada"})
        protected = await run_task(h, "time")

    assert greeted["status"]["state"] == "TASK_STATE_COMPLETED"
    assert protected["status"]["state"] == "TASK_STATE_FAILED"


# Startup validation


def test_construction_starts_no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("constructing an AgentApp must not open sockets")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)

    greeter_app("https://greeter.example.com")


def test_missing_injected_service_is_refused_at_construction() -> None:
    with pytest.raises(DefinitionError, match="no service registered"):
        AgentApp(agent=Greeter(), host=api_key_host("https://greeter.example.com"))


def test_undecorated_agent_is_refused() -> None:
    class Plain(Agent):
        pass

    with pytest.raises(DefinitionError, match="not decorated"):
        AgentApp(agent=Plain(), host=api_key_host("https://greeter.example.com"))


@pytest.mark.parametrize("conflict", ["services", "authorization"])
def test_runtime_conflicts_with_app_level_settings(conflict: str) -> None:
    class AllowAll:
        async def check(self, *args: object) -> bool:
            return True

    settings: dict[str, Any] = {
        "services": Services(),
        "authorization": AllowAll(),
    }
    with pytest.raises(DefinitionError, match="not both"):
        AgentApp(
            agent=Greeter(),
            host=api_key_host("https://greeter.example.com"),
            runtime=Runtime(),
            **{conflict: settings[conflict]},
        )


def test_authenticator_without_card_security_is_refused_at_construction() -> None:
    class HeaderAuth:
        async def authenticate(self, headers: Mapping[str, str]) -> VerifiedCaller:
            return VerifiedCaller(id="caller")

    host = HostConfig(public_url="https://greeter.example.com", auth=HeaderAuth())

    with pytest.raises(DefinitionError, match="card_security"):
        AgentApp(agent=Greeter(), host=host, services=clock_services())


# HostConfig


def test_host_config_requires_an_explicit_auth_choice() -> None:
    with pytest.raises(DefinitionError, match="AllowAnonymous"):
        HostConfig(public_url="https://greeter.example.com", auth=None)  # type: ignore[arg-type]


def test_host_config_rejects_a_bare_string_of_permissions() -> None:
    with pytest.raises(DefinitionError, match="Admin"):
        HostConfig(
            public_url="https://greeter.example.com",
            auth=AllowAnonymous(),
            role_permissions={"Admin": "clock:read"},
        )


def test_host_config_role_permissions_are_read_only() -> None:
    granted = ["clock:read"]
    host = HostConfig(
        public_url="https://greeter.example.com",
        auth=AllowAnonymous(),
        role_permissions={"Admin": granted},
    )
    granted.append("clock:write")

    assert host.role_permissions == {"Admin": frozenset({"clock:read"})}
    with pytest.raises(TypeError):
        host.role_permissions["Admin"] = ["clock:write"]  # type: ignore[index]
