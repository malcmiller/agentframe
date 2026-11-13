"""Tests for hosting an agent over A2A with ``AgentApp``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import socket
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, ClassVar

import httpx
import pytest
from a2a.client import ClientCallContext, ClientConfig, ClientFactory
from a2a.client.service_parameters import (
    ServiceParametersFactory,
    with_a2a_extensions,
)
from a2a.helpers import get_artifact_text, new_data_part
from a2a.types import Message, Role, SendMessageRequest, TaskState
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH, TransportProtocol
from pydantic import BaseModel, PositiveInt

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Authenticator,
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
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI, build_agent_card
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
READER_KEY = "reader-test-key"
ADMIN_KEY = "admin-test-key"


class Greeting(BaseModel):
    text: str


class Clock:
    """An application service injected into skills."""

    time = "noon"

    def now(self) -> str:
        return self.time


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

    failed_invocation_ids: ClassVar[list[str]] = []

    @skill("broken", tags=["test"])
    async def broken(self, ctx: RunContext) -> str:
        """Fail with a secret in the error."""
        Greeter.failed_invocation_ids.append(ctx.invocation_id)
        raise RuntimeError("database password is hunter2")

    @skill("count", tags=["test"])
    async def count(self, ctx: RunContext) -> PositiveInt:
        """Return a value that breaks the declared return type."""
        return 0

    @skill("opaque", tags=["test"])
    async def opaque(self, ctx: RunContext) -> object:
        """Return a value that has no JSON form."""
        return object()


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
    """Build a skill-call extension request: skill id in metadata, one data part."""
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": dict(arguments or {})}],
        "metadata": {"agentframe.skill": skill_id},
    }
    return rpc("SendMessage", {"message": message})


async def post(
    http: httpx.AsyncClient,
    body: Mapping[str, Any],
    key: str | None = READER_KEY,
    activate_extension: bool = True,
) -> httpx.Response:
    headers = {"A2A-Version": "1.0"}
    if key is not None:
        headers["X-API-Key"] = key
    if activate_extension:
        headers["A2A-Extensions"] = SKILL_CALL_EXTENSION_URI
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


async def list_tasks(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await post(http, rpc("ListTasks", {}))
    tasks: list[dict[str, Any]] = response.json()["result"].get("tasks", [])
    return tasks


@asynccontextmanager
async def serve_with_policy(
    serve: ServeLocally, policy: AuthorizationPolicy
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Greeter(),
            host=api_key_host(public_url),
            services=clock_services(),
            authorization=policy,
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


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
    activate = ClientCallContext(
        service_parameters=ServiceParametersFactory.create(
            [with_a2a_extensions([SKILL_CALL_EXTENSION_URI])]
        )
    )

    [response] = [
        response
        async for response in client.send_message(
            SendMessageRequest(message=message), context=activate
        )
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


# Mapping failures to A2A: one section per row of the documented table.
# Missing or invalid credentials (HTTP 401) are tested under Authentication.


@pytest.mark.parametrize(
    ("message_change", "problem"),
    [
        pytest.param({"metadata": {}}, "agentframe.skill", id="no-skill-id"),
        pytest.param(
            {"metadata": {"agentframe.skill": ""}},
            "agentframe.skill",
            id="empty-skill-id",
        ),
        pytest.param(
            {"parts": [{"data": {"name": "Ada"}}, {"data": {"name": "Bo"}}]},
            "exactly one part",
            id="two-data-parts",
        ),
        pytest.param(
            {"parts": [{"data": {"name": "Ada"}}, {"text": "hello"}]},
            "exactly one part",
            id="extra-text-part",
        ),
        pytest.param(
            {"parts": [{"text": "greet Ada"}]},
            "exactly one part",
            id="text-instead-of-data",
        ),
        pytest.param(
            {"parts": [{"data": ["Ada"]}]}, "exactly one part", id="not-an-object"
        ),
        pytest.param(
            {"metadata": {"agentframe.skill": "greet", "agentframe.skills": "x"}},
            "agentframe.skills",
            id="unknown-metadata-field",
        ),
    ],
)
async def test_malformed_skill_call_is_a_protocol_error_without_a_task(
    http: httpx.AsyncClient, message_change: dict[str, Any], problem: str
) -> None:
    body = send_skill("greet", {"name": "Ada"})
    body["params"]["message"].update(message_change)

    response = await post(http, body)

    error = response.json()["error"]
    assert error["code"] == -32602
    assert problem in error["message"]
    assert await list_tasks(http) == []


OTHER_EXTENSION_URI = "https://example.com/ext/other/v1"


async def test_response_acknowledges_the_activated_extension(
    http: httpx.AsyncClient,
) -> None:
    response = await post(http, send_skill("greet", {"name": "Ada"}))

    assert response.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI


async def test_extension_is_activated_alongside_unsupported_ones(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post(
        "/a2a",
        json=send_skill("greet", {"name": "Ada"}),
        headers={
            "A2A-Version": "1.0",
            "X-API-Key": READER_KEY,
            "A2A-Extensions": f"{OTHER_EXTENSION_URI}, {SKILL_CALL_EXTENSION_URI}",
        },
    )

    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert response.headers["A2A-Extensions"] == SKILL_CALL_EXTENSION_URI


async def test_other_extensions_metadata_is_ignored(http: httpx.AsyncClient) -> None:
    body = send_skill("greet", {"name": "Ada"})
    body["params"]["message"]["metadata"][f"{OTHER_EXTENSION_URI}/key"] = "value"

    task = (await post(http, body)).json()["result"]["task"]

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_without_the_extension_a_message_is_free_text_not_a_skill_call(
    http: httpx.AsyncClient,
) -> None:
    # The skill id in metadata is ignored: only activation makes a skill call.
    # This app has no skill router, so free text is refused.
    response = await post(
        http, send_skill("greet", {"name": "Ada"}), activate_extension=False
    )

    assert response.json()["error"]["code"] == -32004  # unsupported operation
    assert "A2A-Extensions" not in response.headers
    assert await list_tasks(http) == []


async def test_arguments_follow_the_schema_published_on_the_card(
    http: httpx.AsyncClient,
) -> None:
    card = (await http.get(AGENT_CARD_WELL_KNOWN_PATH)).json()
    [extension] = card["capabilities"]["extensions"]
    schema = extension["params"]["skills"]["greet"]
    assert schema["required"] == ["name"]
    assert schema["additionalProperties"] is False

    matching = await run_task(http, "greet", {"name": "Ada"})
    undeclared = await run_task(http, "greet", {"name": "Ada", "title": "Dr"})

    assert matching["status"]["state"] == "TASK_STATE_COMPLETED"
    assert undeclared["status"]["state"] == "TASK_STATE_REJECTED"
    assert "title" in status_text(undeclared)


async def test_malformed_json_rpc_is_a_protocol_error_without_a_task(
    http: httpx.AsyncClient,
) -> None:
    response = await http.post(
        "/a2a",
        content=b"{not json",
        headers={"A2A-Version": "1.0", "X-API-Key": READER_KEY},
    )

    assert response.json()["error"]["code"] == -32700
    assert await list_tasks(http) == []


async def test_unknown_skill_is_rejected(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "missing")

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "no skill" in status_text(task)
    assert "missing" not in status_text(task)
    assert task["metadata"]["invocation_id"]


async def test_invalid_arguments_are_rejected_without_echoing_the_value(
    http: httpx.AsyncClient,
) -> None:
    task = await run_task(http, "greet", {"name": ["secret-value"]})

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "name" in status_text(task)
    assert "secret-value" not in str(task["status"])
    assert task["metadata"]["invocation_id"]


async def test_missing_permission_is_rejected(http: httpx.AsyncClient) -> None:
    denied = await run_task(http, "time", key=READER_KEY)
    allowed = await run_task(http, "time", key=ADMIN_KEY)

    assert denied["status"]["state"] == "TASK_STATE_REJECTED"
    assert "clock:read" in status_text(denied)
    assert denied["metadata"]["invocation_id"]
    assert allowed["artifacts"][0]["parts"] == [{"text": "noon"}]


async def test_policy_denial_is_rejected(serve: ServeLocally) -> None:
    class DenyAll(AuthorizationPolicy):
        # noinspection PyShadowingNames
        async def check(
            self,
            principal: Principal | None,
            skill: SkillDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            return False

    async with serve_with_policy(serve, DenyAll()) as h:
        task = await run_task(h, "greet", {"name": "Ada"})

    assert task["status"]["state"] == "TASK_STATE_REJECTED"
    assert "denied" in status_text(task)
    assert task["metadata"]["invocation_id"]


async def test_skill_timeout_fails_the_task(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "slow")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "timeout" in status_text(task)
    assert task["metadata"]["invocation_id"]


async def test_invalid_output_fails_the_task(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "count")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "invalid value" in status_text(task)
    assert task["metadata"]["invocation_id"]


async def test_unserializable_result_fails_the_task(http: httpx.AsyncClient) -> None:
    task = await run_task(http, "opaque")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "serialized" in status_text(task)
    assert "artifacts" not in task
    assert task["metadata"]["invocation_id"]


async def test_skill_exception_fails_with_a_safe_message(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    task = await run_task(http, "broken")

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert status_text(task) == "The skill failed."
    assert "hunter2" not in str(task)
    # The caller, the skill and the log all see the run's one ID.
    invocation_id = task["metadata"]["invocation_id"]
    assert Greeter.failed_invocation_ids[-1] == invocation_id
    assert any(
        invocation_id in record.getMessage() and record.exc_info
        for record in caplog.records
    )


async def test_policy_outage_fails_the_task(serve: ServeLocally) -> None:
    class BrokenPolicy(AuthorizationPolicy):
        # noinspection PyShadowingNames
        async def check(
            self,
            principal: Principal | None,
            skill: SkillDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            raise ConnectionError("policy service at 10.0.0.7 is down")

    async with serve_with_policy(serve, BrokenPolicy()) as h:
        task = await run_task(h, "greet", {"name": "Ada"})

    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "unavailable" in status_text(task)
    assert "10.0.0.7" not in str(task)
    assert task["metadata"]["invocation_id"]


# Clarification (INPUT_REQUIRED) and cancellation (CANCELED) need routing (#6)
# and task operations (#8); test_a2a_executor.py covers their mapping.


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
    assert response.headers["WWW-Authenticate"] == (
        'APIKey location="header", name="X-API-Key"'
    )
    assert response.json() == {"error": "Authentication required."}
    assert await list_tasks(http) == []


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
    assert protected["status"]["state"] == "TASK_STATE_REJECTED"


# Startup validation


def test_construction_starts_no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> None:
        raise AssertionError("constructing an AgentApp must not open sockets")

    # PyCharm cannot see socket methods implemented in C.
    # noinspection PyUnresolvedReferences
    monkeypatch.setattr(socket.socket, "bind", refuse)
    # noinspection PyUnresolvedReferences
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
    class AllowAll(AuthorizationPolicy):
        # noinspection PyShadowingNames
        async def check(
            self,
            principal: Principal | None,
            skill: SkillDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
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
    class HeaderAuth(Authenticator):
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
