"""Tests for routing free-text A2A messages with a custom ``SkillRouter``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers import get_artifact_text, get_message_text, new_text_part
from a2a.types import Message, Role, SendMessageRequest, TaskState
from a2a.utils.constants import TransportProtocol

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
    RunLimits,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.card import SKILL_CALL_EXTENSION_URI, build_agent_card
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"
ADMIN_KEY = "admin-test-key"

TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004


@agent(version="1.0.0")
class Greeter(Agent):
    """Greets people."""

    limits = RunLimits(timeout_seconds=0.5)

    @skill("greet", tags=["test"])
    async def greet(self, name: str, ctx: RunContext) -> str:
        """Greet someone."""
        return f"Hello, {name}!"

    @skill("audit", requires_permissions=["audit:read"], tags=["test"])
    async def audit(self, ctx: RunContext) -> str:
        """Read the audit log."""
        return "audit log"

    @skill("nap", tags=["test"])
    async def nap(self, seconds: float, ctx: RunContext) -> str:
        """Sleep for a while."""
        await asyncio.sleep(seconds)
        return "rested"


class GreetingRouter(SkillRouter):
    """Routes "greet <name>" to ``greet`` and asks for the name when it is missing.

    Records every conversation it sees, as text, and the skills offered.
    """

    def __init__(self) -> None:
        self.conversations: list[list[str]] = []
        self.offered: list[list[str]] = []

    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> SkillCall | Clarification:
        self.conversations.append([get_message_text(m) for m in conversation])
        self.offered.append([s.id for s in skills])
        words = " ".join(
            get_message_text(m) for m in conversation if m.role == Role.ROLE_USER
        ).split()
        if "greet" not in words:
            return Clarification("What would you like me to do?")
        name = words[words.index("greet") + 1 :]
        if not name:
            return Clarification("Whom should I greet?")
        return SkillCall("greet", {"name": name[-1]})


class FixedRouter(SkillRouter):
    """Always returns the same decision, or runs ``behave`` instead."""

    def __init__(self, decision: object = None, behave: Any = None) -> None:
        self.decision = decision
        self.behave = behave

    async def route(
        self, conversation: Sequence[Message], skills: Sequence[SkillDefinition]
    ) -> Any:
        if self.behave is not None:
            await self.behave()
        return self.decision


def host(public_url: str) -> HostConfig:
    return HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(
            keys={
                ALICE_KEY: APIKey("alice"),
                BOB_KEY: APIKey("bob"),
                ADMIN_KEY: APIKey("admin", roles=["Auditor"]),
            }
        ),
        role_permissions={"Auditor": ["audit:read"]},
    )


@asynccontextmanager
async def serve_router(
    serve: ServeLocally, router: object, **app_options: Any
) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Greeter(),
            host=host(public_url),
            router=router,  # type: ignore[arg-type]
            **app_options,
        )

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


@pytest.fixture
def router() -> GreetingRouter:
    return GreetingRouter()


@pytest.fixture
async def http(
    serve: ServeLocally, router: GreetingRouter
) -> AsyncIterator[httpx.AsyncClient]:
    async with serve_router(serve, router) as client:
        yield client


async def say(
    http: httpx.AsyncClient,
    text: str,
    key: str = ALICE_KEY,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Send one free-text message and return the decoded JSON-RPC response."""
    message: dict[str, Any] = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }
    if task_id is not None:
        message["taskId"] = task_id
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    headers = {"A2A-Version": "1.0", "X-API-Key": key}
    response = await http.post("/a2a", json=body, headers=headers)
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


async def say_task(
    http: httpx.AsyncClient,
    text: str,
    key: str = ALICE_KEY,
    task_id: str | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = (await say(http, text, key, task_id))["result"]["task"]
    return task


def state(task: Mapping[str, Any]) -> str:
    value: str = task["status"]["state"]
    return value


def status_text(task: Mapping[str, Any]) -> str:
    return " ".join(part["text"] for part in task["status"]["message"]["parts"])


def invocation_id(task: Mapping[str, Any]) -> str:
    value: str = task["metadata"]["invocation_id"]
    return value


# Routing and clarification


async def test_official_client_answers_a_clarification_in_the_same_task(
    http: httpx.AsyncClient, router: GreetingRouter
) -> None:
    http.headers["X-API-Key"] = ALICE_KEY
    config = ClientConfig(
        httpx_client=http,
        streaming=False,
        supported_protocol_bindings=[TransportProtocol.JSONRPC],
    )
    card = build_agent_card(Greeter.definition(), host(str(http.base_url).rstrip("/")))
    client = ClientFactory(config).create(card)

    async def send(text: str, task_id: str = "") -> Any:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[new_text_part(text)],
            task_id=task_id,
        )
        [response] = [
            r async for r in client.send_message(SendMessageRequest(message=message))
        ]
        return response.task

    asked = await send("please greet")
    answered = await send("Ada", task_id=asked.id)

    assert asked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert get_message_text(asked.status.message) == "Whom should I greet?"
    assert asked.metadata["invocation_id"]
    assert answered.id == asked.id
    assert answered.history[-1].context_id == asked.context_id
    assert answered.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(answered.artifacts[0]) == "Hello, Ada!"
    # The router saw the earlier messages, including its own question.
    assert router.conversations[-1] == ["please greet", "Whom should I greet?", "Ada"]


async def test_router_selects_a_skill_from_free_text(http: httpx.AsyncClient) -> None:
    task = await say_task(http, "greet Grace")

    assert state(task) == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["text"] == "Hello, Grace!"


async def test_router_is_offered_only_skills_the_caller_may_run(
    http: httpx.AsyncClient, router: GreetingRouter
) -> None:
    await say_task(http, "hello", key=ALICE_KEY)
    await say_task(http, "hello", key=ADMIN_KEY)

    assert router.offered == [["greet", "nap"], ["greet", "audit", "nap"]]


async def test_structured_skill_calls_never_reach_the_router(
    http: httpx.AsyncClient, router: GreetingRouter
) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": "m-1",
                "role": "ROLE_USER",
                "parts": [{"data": {"name": "Ada"}}],
                "metadata": {"agentframe.skill": "greet"},
            }
        },
    }
    headers = {
        "A2A-Version": "1.0",
        "X-API-Key": ALICE_KEY,
        "A2A-Extensions": SKILL_CALL_EXTENSION_URI,
    }

    response = await http.post("/a2a", json=body, headers=headers)

    assert state(response.json()["result"]["task"]) == "TASK_STATE_COMPLETED"
    assert router.conversations == []


async def test_free_text_without_a_router_is_refused_without_a_task(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Greeter(), host=host(public_url))

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        response = await say(h, "greet Ada")

    assert response["error"]["code"] == UNSUPPORTED_OPERATION
    assert "no skill router" in response["error"]["message"]


# Continuation


async def test_only_the_tasks_owner_can_answer(http: httpx.AsyncClient) -> None:
    asked = await say_task(http, "please greet", key=ALICE_KEY)

    response = await say(http, "Mallory", key=BOB_KEY, task_id=asked["id"])

    assert response["error"]["code"] == TASK_NOT_FOUND
    answered = await say_task(http, "Ada", key=ALICE_KEY, task_id=asked["id"])
    assert state(answered) == "TASK_STATE_COMPLETED"


async def test_a_finished_task_rejects_new_messages(http: httpx.AsyncClient) -> None:
    done = await say_task(http, "greet Ada")

    response = await say(http, "greet Grace", task_id=done["id"])

    assert response["error"]["code"] == UNSUPPORTED_OPERATION


async def test_an_answer_can_ask_again(http: httpx.AsyncClient) -> None:
    asked = await say_task(http, "hi")
    asked_again = await say_task(http, "please greet", task_id=asked["id"])

    assert status_text(asked) == "What would you like me to do?"
    assert state(asked_again) == "TASK_STATE_INPUT_REQUIRED"
    assert status_text(asked_again) == "Whom should I greet?"


async def test_answers_are_checked_by_the_authorization_policy(
    serve: ServeLocally, router: GreetingRouter
) -> None:
    class NoMallory(AuthorizationPolicy):
        # noinspection PyShadowingNames
        async def check(
            self,
            principal: Principal | None,
            skill: SkillDefinition,
            arguments: Mapping[str, object],
        ) -> bool:
            return arguments.get("name") != "Mallory"

    async with serve_router(serve, router, authorization=NoMallory()) as h:
        asked = await say_task(h, "please greet")
        answered = await say_task(h, "Mallory", task_id=asked["id"])

    assert state(answered) == "TASK_STATE_REJECTED"


# Invalid decisions, failures and timeouts


@pytest.mark.parametrize(
    "decision",
    [
        SkillCall("audit", {}),  # not offered to this caller
        SkillCall("missing", {}),
        SkillCall("greet", {"name": 42}),
        Clarification("  "),
        "greet",
    ],
    ids=["ineligible", "unknown", "invalid-arguments", "empty-question", "garbage"],
)
async def test_invalid_router_decisions_are_rejected(
    serve: ServeLocally, decision: object
) -> None:
    async with serve_router(serve, FixedRouter(decision)) as h:
        task = await say_task(h, "anything")

    assert state(task) == "TASK_STATE_REJECTED"
    assert invocation_id(task)


async def test_router_exception_fails_with_a_safe_message(
    serve: ServeLocally, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode() -> None:
        raise RuntimeError("router password is hunter2")

    async with serve_router(serve, FixedRouter(behave=explode)) as h:
        with caplog.at_level(logging.ERROR):
            task = await say_task(h, "anything")

    assert state(task) == "TASK_STATE_FAILED"
    assert status_text(task) == "The skill router failed."
    assert "hunter2" not in str(task)
    assert invocation_id(task) in caplog.text


async def test_hanging_router_times_out_and_fails(serve: ServeLocally) -> None:
    async def hang() -> None:
        await asyncio.sleep(5)

    async with serve_router(serve, FixedRouter(behave=hang)) as h:
        task = await say_task(h, "anything")

    assert state(task) == "TASK_STATE_FAILED"
    assert "timeout" in status_text(task)
    assert invocation_id(task)


async def test_routing_time_counts_against_the_runs_timeout(
    serve: ServeLocally,
) -> None:
    async def dawdle() -> None:
        await asyncio.sleep(0.3)

    # Routing takes 0.3s and the skill 0.3s: each fits the 0.5s timeout alone.
    nap = SkillCall("nap", {"seconds": 0.3})
    async with serve_router(serve, FixedRouter(nap, behave=dawdle)) as h:
        task = await say_task(h, "anything")

    assert state(task) == "TASK_STATE_FAILED"
    assert "timeout" in status_text(task)
