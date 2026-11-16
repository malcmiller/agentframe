"""Tests for sending free-text A2A messages to ``AgentApp(default_skill=...)``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn.
"""

import uuid
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import httpx
import pytest
from a2a.types import Message
from pydantic import Field

from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    Clarification,
    DefinitionError,
    HostConfig,
    Inject,
    Remote,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    agent,
    skill,
)
from agentframe.authoring.definitions import text_skill_argument
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
READER_KEY = "reader-test-key"
ADMIN_KEY = "admin-test-key"


class Clock:
    """An application service injected into skills."""


@agent(version="1.0.0")
class Echo(Agent):
    """Echoes text."""

    @skill("answer", tags=["test"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question."""
        return f"You asked: {question}"

    @skill("shout", tags=["test"])
    async def shout(
        self, text: Annotated[str, Field(max_length=5)], ctx: RunContext
    ) -> str:
        """Shout short text."""
        return text.upper()

    @skill("secret", requires_permissions=["secret:read"], tags=["test"])
    async def secret(self, text: str, ctx: RunContext) -> str:
        """Answer only admins."""
        return "classified"

    @skill("repeat", tags=["test"])
    async def repeat(self, text: str, times: int, ctx: RunContext) -> str:
        """Repeat text."""
        return text * times

    @skill("double", tags=["test"])
    async def double(self, number: int, ctx: RunContext) -> int:
        """Double a number."""
        return number * 2

    @skill("maybe", tags=["test"])
    async def maybe(self, text: str | None, ctx: RunContext) -> str:
        """Take optional text."""
        return text or ""


def host(public_url: str) -> HostConfig:
    return HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(
            keys={
                READER_KEY: APIKey("reader"),
                ADMIN_KEY: APIKey("admin", roles=["Admin"]),
            }
        ),
        role_permissions={"Admin": ["secret:read"]},
    )


async def send(
    base_url: str, parts: list[dict[str, Any]], key: str = READER_KEY
) -> dict[str, Any]:
    """Send one message without the skill-call extension; return the response."""
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "ROLE_USER",
                "parts": parts,
            }
        },
    }
    headers = {"A2A-Version": "1.0", "X-API-Key": key}
    async with httpx.AsyncClient(base_url=base_url) as http:
        response = await http.post("/a2a", json=body, headers=headers)
    result: dict[str, Any] = response.json()
    return result


def answer_text(task: dict[str, Any]) -> str:
    return " ".join(part["text"] for part in task["artifacts"][0]["parts"])


async def test_free_text_goes_to_the_default_skill_without_a_model(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Echo(), host=host(public_url), default_skill="answer")

    async with serve(make_app) as base_url:
        response = await send(base_url, [{"text": "Why?"}, {"text": "Really?"}])

    task = response["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert answer_text(task) == "You asked: Why?\nReally?"


async def test_the_default_skills_argument_is_still_validated(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Echo(), host=host(public_url), default_skill="shout")

    async with serve(make_app) as base_url:
        short = await send(base_url, [{"text": "hey"}])
        too_long = await send(base_url, [{"text": "hello there"}])

    assert answer_text(short["result"]["task"]) == "HEY"
    assert too_long["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"


class ShoutRouter:
    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        return SkillCall("shout", {"text": "hi"})


@pytest.mark.parametrize("parts", [[{"data": {"question": "Why?"}}], [{"text": "  "}]])
async def test_a_message_without_text_is_rejected(
    serve: ServeLocally, parts: list[dict[str, Any]]
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Echo(), host=host(public_url), default_skill="answer")

    async with serve(make_app) as base_url:
        response = await send(base_url, parts)

    status = response["result"]["task"]["status"]
    assert status["state"] == "TASK_STATE_REJECTED"


async def test_the_default_skills_permissions_are_still_checked(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Echo(), host=host(public_url), default_skill="secret")

    async with serve(make_app) as base_url:
        reader = await send(base_url, [{"text": "tell me"}], key=READER_KEY)
        admin = await send(base_url, [{"text": "tell me"}], key=ADMIN_KEY)

    assert reader["result"]["task"]["status"]["state"] == "TASK_STATE_REJECTED"
    assert answer_text(admin["result"]["task"]) == "classified"


async def test_a_router_decides_instead_of_the_default_skill(
    serve: ServeLocally,
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return AgentApp(
            agent=Echo(),
            host=host(public_url),
            router=ShoutRouter(),
            default_skill="answer",
        )

    async with serve(make_app) as base_url:
        response = await send(base_url, [{"text": "Why?"}])

    assert answer_text(response["result"]["task"]) == "HI"


@pytest.mark.parametrize(
    ("skill_id", "problem"),
    [
        ("missing", "has no skill 'missing'"),
        ("repeat", "cannot take plain text"),
        ("double", "cannot take plain text"),
        ("maybe", "cannot take plain text"),
    ],
)
@pytest.mark.parametrize("router", [None, ShoutRouter()])
def test_an_unusable_default_skill_fails_construction(
    skill_id: str, problem: str, router: ShoutRouter | None
) -> None:
    with pytest.raises(DefinitionError, match=problem):
        AgentApp(
            agent=Echo(),
            host=host("http://127.0.0.1:8000"),
            router=router,
            default_skill=skill_id,
        )


def test_injected_remote_and_context_parameters_do_not_count() -> None:
    @agent(version="1.0.0")
    class Helper(Agent):
        """Helps."""

        @skill("help", tags=["test"])
        async def help(
            self,
            ctx: RunContext,
            clock: Inject[Clock],
            research: Remote[Literal["research-agent"]],
            request: str,
        ) -> str:
            """Help with a request."""
            return request

    assert text_skill_argument(Helper.definition(), "help") == "request"
