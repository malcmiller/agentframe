"""End-to-end: an independent official A2A client talks to an ``AgentApp``.

Every request goes over real localhost HTTP through the official ``a2a-sdk``
client, the way any other framework's A2A caller would connect: fetch the
public Agent Card, pick its JSON-RPC interface and authenticate with the
security scheme the card declares. No test reaches into AgentFrame internals,
and nothing leaves localhost.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Annotated

import httpx
import pytest
from a2a.client import A2ACardResolver, A2AClientError, Client
from a2a.helpers import (
    get_artifact_text,
    get_data_parts,
    get_message_text,
    new_text_part,
)
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Role,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotFoundError
from pydantic import BaseModel, Field

from a2a_client import call_skill, connect, send
from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    AuthorizationPolicy,
    Clarification,
    HostConfig,
    Principal,
    RunContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"


# The application: an agent, a custom router and a resource policy


class Badge(BaseModel):
    name: str
    access: str


class Visit(BaseModel):
    name: str
    guests: Annotated[int, Field(ge=1, le=10)]


@agent(version="1.0.0")
class Receptionist(Agent):
    """Welcomes visitors."""

    def __init__(self) -> None:
        self.taxi_called = asyncio.Event()
        self.taxi_arrived = asyncio.Event()

    @skill("greet", tags=["visitors"])
    async def greet(self, name: str, ctx: RunContext) -> str:
        """Greet a visitor."""
        return f"Hello, {name}!"

    @skill("badge", tags=["visitors"])
    async def badge(self, name: str, ctx: RunContext) -> Badge:
        """Print a visitor badge."""
        return Badge(name=name, access="lobby")

    @skill("register", tags=["visitors"])
    async def register(self, visit: Visit, ctx: RunContext) -> Badge:
        """Register a visit and print a group badge."""
        return Badge(name=visit.name, access=f"lobby, party of {visit.guests}")

    @skill("guest_list", requires_permissions=["guests:read"], tags=["visitors"])
    async def guest_list(self, ctx: RunContext) -> list[str]:
        """List today's guests."""
        return ["Ada", "Grace"]

    @skill("crash", tags=["test"])
    async def crash(self, ctx: RunContext) -> str:
        """Fail with a secret in the error."""
        raise RuntimeError("database password is hunter2")

    @skill("call_taxi", tags=["visitors"])
    async def call_taxi(self, ctx: RunContext) -> str:
        """Wait until the taxi arrives."""
        self.taxi_called.set()
        await self.taxi_arrived.wait()
        return "Your taxi is here."


class GreetingRouter(SkillRouter):
    """Routes "greet <name>" to ``greet`` and asks for the name when it is missing."""

    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> SkillCall | Clarification:
        words = " ".join(
            get_message_text(m) for m in conversation if m.role == Role.ROLE_USER
        ).split()
        if "greet" not in words:
            return Clarification("What would you like me to do?")
        name = words[words.index("greet") + 1 :]
        if not name:
            return Clarification("Whom should I greet?")
        return SkillCall("greet", {"name": name[-1]})


class BannedVisitors(AuthorizationPolicy):
    """Resource policy: nobody greets a banned visitor."""

    banned = frozenset({"Mallory"})

    # noinspection PyShadowingNames
    async def check(
        self,
        principal: Principal | None,
        skill: SkillDefinition,
        arguments: Mapping[str, object],
    ) -> bool:
        return arguments.get("name") not in self.banned


def receptionist_app(receptionist: Receptionist, public_url: str) -> AgentApp:
    keys = {ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob", roles=["Staff"])}
    return AgentApp(
        agent=receptionist,
        host=HostConfig(
            public_url=public_url,
            auth=APIKeyAuth(keys=keys),
            role_permissions={"Staff": ["guests:read"]},
        ),
        router=GreetingRouter(),
        authorization=BannedVisitors(),
    )


# The caller: the official A2A SDK client (see a2a_client.py)


async def say(client: Client, text: str, task_id: str = "") -> Task:
    """Send free text, optionally answering a question in an existing task."""
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[new_text_part(text)],
        task_id=task_id,
    )
    return await send(client, message, skill_call=False)


async def get_task(client: Client, task_id: str) -> Task:
    return await client.get_task(GetTaskRequest(id=task_id))


async def wait_until_finished(client: Client, task_id: str) -> Task:
    async with asyncio.timeout(5):
        while True:
            task = await get_task(client, task_id)
            if task.status.state != TaskState.TASK_STATE_WORKING:
                return task
            await asyncio.sleep(0.01)


def status_text(task: Task) -> str:
    return get_message_text(task.status.message)


@pytest.fixture
def receptionist() -> Receptionist:
    return Receptionist()


@pytest.fixture
async def base_url(
    serve: ServeLocally, receptionist: Receptionist
) -> AsyncIterator[str]:
    def app(public_url: str) -> AgentApp:
        return receptionist_app(receptionist, public_url)

    async with serve(app) as url:
        yield url


@pytest.fixture
async def alice(base_url: str) -> AsyncIterator[Client]:
    async with await connect(base_url, ALICE_KEY) as client:
        yield client


@pytest.fixture
async def bob(base_url: str) -> AsyncIterator[Client]:
    async with await connect(base_url, BOB_KEY) as client:
        yield client


# Discovery


# The extension parameters are a protobuf Struct, whose values PyCharm cannot narrow.
# noinspection PyUnresolvedReferences,PyTypeChecker
async def test_card_declares_the_skill_call_extension_and_argument_schemas(
    base_url: str,
) -> None:
    async with httpx.AsyncClient() as http:
        card = await A2ACardResolver(http, base_url).get_agent_card()

    [extension] = card.capabilities.extensions
    schemas = extension.params["skills"]
    assert extension.uri == SKILL_CALL_EXTENSION_URI
    assert schemas["greet"]["properties"]["name"]["type"] == "string"
    assert list(schemas["greet"]["required"]) == ["name"]
    assert sorted(schemas.keys()) == sorted(s.id for s in card.skills)
    assert "apiKey" in card.security_schemes
    assert not card.capabilities.streaming


# Structured skill calls


async def test_structured_call_returns_a_str_result_as_text(alice: Client) -> None:
    task = await call_skill(alice, "greet", {"name": "Ada"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Hello, Ada!"


async def test_structured_call_returns_a_model_result_as_a_value_data_part(
    alice: Client,
) -> None:
    task = await call_skill(alice, "badge", {"name": "Ada"})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_data_parts(task.artifacts[0].parts) == [
        {"value": {"name": "Ada", "access": "lobby"}}
    ]


async def test_structured_call_validates_a_model_argument(alice: Client) -> None:
    task = await call_skill(alice, "register", {"visit": {"name": "Ada", "guests": 3}})

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_data_parts(task.artifacts[0].parts) == [
        {"value": {"name": "Ada", "access": "lobby, party of 3"}}
    ]


async def test_a_constraint_violation_is_rejected_without_echoing_the_value(
    alice: Client,
) -> None:
    task = await call_skill(
        alice, "register", {"visit": {"name": "Ada", "guests": 4242}}
    )

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert "4242" not in status_text(task)
    assert not task.artifacts


# Free text and clarification


async def test_free_text_is_routed_to_a_skill(alice: Client) -> None:
    task = await say(alice, "please greet Grace")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Hello, Grace!"


async def test_a_clarification_is_answered_in_the_same_task(alice: Client) -> None:
    asked = await say(alice, "please greet")
    answered = await say(alice, "Ada", task_id=asked.id)

    assert asked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert status_text(asked) == "Whom should I greet?"
    assert answered.id == asked.id
    assert answered.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(answered.artifacts[0]) == "Hello, Ada!"


# Authentication and authorization


@pytest.mark.parametrize("api_key", [None, "wrong-key"])
async def test_a_missing_or_wrong_api_key_gets_401(
    base_url: str, api_key: str | None
) -> None:
    async with await connect(base_url, api_key) as stranger:
        with pytest.raises(A2AClientError) as raised:
            await call_skill(stranger, "greet", {"name": "Ada"})

    http_error = raised.value.__cause__
    assert isinstance(http_error, httpx.HTTPStatusError)
    assert http_error.response.status_code == 401


async def test_a_permitted_caller_runs_a_protected_skill(bob: Client) -> None:
    task = await call_skill(bob, "guest_list")

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_data_parts(task.artifacts[0].parts) == [{"value": ["Ada", "Grace"]}]


async def test_a_missing_permission_is_rejected(alice: Client) -> None:
    task = await call_skill(alice, "guest_list")

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert not task.artifacts


async def test_a_policy_denial_is_rejected(alice: Client) -> None:
    task = await call_skill(alice, "greet", {"name": "Mallory"})

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert not task.artifacts


# Rejections and failures


async def test_an_unknown_skill_is_rejected(alice: Client) -> None:
    task = await call_skill(alice, "teleport")

    assert task.status.state == TaskState.TASK_STATE_REJECTED


async def test_invalid_arguments_are_rejected_without_echoing_them(
    alice: Client,
) -> None:
    task = await call_skill(alice, "greet", {"name": {"secret": "s3cret"}})

    assert task.status.state == TaskState.TASK_STATE_REJECTED
    assert "s3cret" not in status_text(task)


async def test_a_skill_exception_fails_with_a_safe_message(alice: Client) -> None:
    task = await call_skill(alice, "crash")

    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert status_text(task) == "The skill failed."
    assert "hunter2" not in str(task)
    assert task.metadata["invocation_id"]


# Task operations


async def test_non_blocking_send_then_get(
    alice: Client, receptionist: Receptionist
) -> None:
    task = await call_skill(alice, "call_taxi", return_immediately=True)

    assert task.status.state in (
        TaskState.TASK_STATE_SUBMITTED,
        TaskState.TASK_STATE_WORKING,
    )

    receptionist.taxi_arrived.set()
    finished = await wait_until_finished(alice, task.id)

    assert finished.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(finished.artifacts[0]) == "Your taxi is here."


async def test_cancel_a_running_task(alice: Client, receptionist: Receptionist) -> None:
    task = await call_skill(alice, "call_taxi", return_immediately=True)
    await asyncio.wait_for(receptionist.taxi_called.wait(), timeout=5)

    cancelled = await alice.cancel_task(CancelTaskRequest(id=task.id))

    assert cancelled.status.state == TaskState.TASK_STATE_CANCELED
    stored = await get_task(alice, task.id)
    assert stored.status.state == TaskState.TASK_STATE_CANCELED


async def test_another_caller_cannot_see_the_task(
    alice: Client, bob: Client, receptionist: Receptionist
) -> None:
    task = await call_skill(alice, "call_taxi", return_immediately=True)
    await asyncio.wait_for(receptionist.taxi_called.wait(), timeout=5)

    with pytest.raises(TaskNotFoundError):
        await get_task(bob, task.id)
    with pytest.raises(TaskNotFoundError):
        await bob.cancel_task(CancelTaskRequest(id=task.id))
    bobs_tasks = await bob.list_tasks(ListTasksRequest())
    assert task.id not in [t.id for t in bobs_tasks.tasks]

    receptionist.taxi_arrived.set()
    finished = await wait_until_finished(alice, task.id)
    assert finished.status.state == TaskState.TASK_STATE_COMPLETED
