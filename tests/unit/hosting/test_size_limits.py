"""Tests for ``HostConfig.max_request_bytes`` and ``max_response_bytes``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn,
both with a ``Content-Length`` and chunked. Results go through the official
A2A client.
"""

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from functools import partial
from typing import Any

import httpx
import pytest
from a2a.client import Client
from a2a.types import ListTasksRequest, Message, Task, TaskState

from a2a_client import call_skill, connect, say
from agentframe import (
    Agent,
    AgentApp,
    APIKey,
    APIKeyAuth,
    Clarification,
    DefinitionError,
    HostConfig,
    RoutingContext,
    RunContext,
    SkillCall,
    SkillDefinition,
    SkillRouter,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from local_server import ServeLocally

# Test-only value; real applications load keys from the environment.
API_KEY = "test-key"

TOO_LARGE = {"error": "Request too large."}

RESPONSE_TOO_LARGE = "The result exceeds the host's response size limit"

# A marker that must never reach the logs.
SECRET = "do-not-log-me"


@agent(version="1.0.0")
class Sizes(Agent):
    """Returns results of a requested size."""

    def __init__(self) -> None:
        self.runs = 0

    @skill("text", tags=["test"])
    async def text(self, character: str, count: int, ctx: RunContext) -> str:
        """Return ``character`` repeated ``count`` times."""
        self.runs += 1
        return character * count

    @skill("data", tags=["test"])
    async def data(self, count: int, ctx: RunContext) -> list[str]:
        """Return a list holding a string of ``count`` characters."""
        self.runs += 1
        return [SECRET[0] * count]


class QuestionRouter(SkillRouter):
    """Asks the free-text message back as a question."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        return Clarification(conversation[-1].parts[0].text)


def sizes_app(
    sizes: Sizes,
    public_url: str,
    *,
    max_request_bytes: int = 1_048_576,
    max_response_bytes: int = 4_194_304,
) -> AgentApp:
    host = HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
        max_request_bytes=max_request_bytes,
        max_response_bytes=max_response_bytes,
    )
    return AgentApp(agent=sizes, host=host, router=QuestionRouter())


def skill_call_body(text: str) -> bytes:
    """A JSON-RPC ``SendMessage`` body that runs ``text`` with ``text``."""
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {"character": text, "count": 1}}],
        "metadata": {"agentframe.skill": "text"},
    }
    body = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {"message": message},
    }
    return json.dumps(body).encode()


async def post(
    base_url: str, body: bytes, *, chunked: bool = False, api_key: str = API_KEY
) -> httpx.Response:
    """POST ``body`` to ``/a2a``, chunked (with no ``Content-Length``) if asked."""

    async def in_two_chunks() -> AsyncIterator[bytes]:
        middle = len(body) // 2
        yield body[:middle]
        yield body[middle:]

    headers = {
        "A2A-Version": "1.0",
        "A2A-Extensions": SKILL_CALL_EXTENSION_URI,
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }
    content = in_two_chunks() if chunked else body
    async with httpx.AsyncClient(base_url=base_url) as http:
        return await http.post("/a2a", content=content, headers=headers)


async def listed_tasks(client: Client) -> list[Task]:
    return list((await client.list_tasks(ListTasksRequest())).tasks)


# Configuration


@pytest.mark.parametrize(
    ("limits", "field"),
    [
        ({"max_request_bytes": 0}, "max_request_bytes"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
    ],
)
def test_a_size_limit_below_one_is_refused(limits: dict[str, int], field: str) -> None:
    with pytest.raises(DefinitionError, match=field):
        sizes_app(Sizes(), "https://sizes.example.com", **limits)


# Request size


@pytest.mark.parametrize("chunked", [False, True])
async def test_a_request_exactly_at_the_limit_is_accepted(
    serve: ServeLocally, chunked: bool
) -> None:
    sizes = Sizes()
    body = skill_call_body("a")
    make_app = partial(sizes_app, sizes, max_request_bytes=len(body))

    async with serve(make_app) as base_url:
        response = await post(base_url, body, chunked=chunked)

    assert response.status_code == 200
    assert (
        response.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    )
    assert sizes.runs == 1


@pytest.mark.parametrize("chunked", [False, True])
async def test_a_request_one_byte_over_is_refused_before_auth_and_creates_no_task(
    serve: ServeLocally, chunked: bool, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    sizes = Sizes()
    body = skill_call_body(SECRET)
    make_app = partial(sizes_app, sizes, max_request_bytes=len(body) - 1)

    async with serve(make_app) as base_url:
        # A wrong key would get 401 if authentication ran first.
        refused = await post(base_url, body, chunked=chunked, api_key="wrong")
        async with await connect(base_url, API_KEY) as client:
            tasks = await listed_tasks(client)

    assert refused.status_code == 413
    assert refused.json() == TOO_LARGE
    assert tasks == []
    assert sizes.runs == 0
    assert SECRET not in caplog.text


async def test_an_oversized_content_length_is_refused_unread() -> None:
    app = sizes_app(Sizes(), "https://sizes.example.com", max_request_bytes=10)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/a2a",
        "raw_path": b"/a2a",
        "query_string": b"",
        "headers": [(b"content-length", b"11")],
        "server": ("127.0.0.1", 8000),
    }
    sent: list[Any] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("The body must not be read.")

    async def send(message: Any) -> None:
        sent.append(message)

    await app(scope, receive, send)

    assert sent[0]["status"] == 413


# Response size


@pytest.fixture
async def small_responses(serve: ServeLocally) -> AsyncIterator[tuple[str, Sizes]]:
    """Serve an agent whose results and questions may be at most 100 bytes."""
    sizes = Sizes()
    make_app = partial(sizes_app, sizes, max_response_bytes=100)
    async with serve(make_app) as base_url:
        yield base_url, sizes


def assert_failed_for_size(task: Task) -> None:
    """The task failed with the safe message and carries no part of the result."""
    assert task.status.state == TaskState.TASK_STATE_FAILED
    assert task.status.message.parts[0].text == RESPONSE_TOO_LARGE
    assert not task.artifacts
    metadata = dict(task.metadata.items())
    assert metadata["invocation_id"]
    assert metadata["usage"]["model_calls"] == 0


@pytest.mark.parametrize(
    ("skill_id", "arguments"),
    [
        ("text", {"character": "x", "count": 100}),
        # The data part {"value":["..."]} is 14 bytes of JSON around the string.
        ("data", {"count": 86}),
    ],
)
async def test_a_result_exactly_at_the_limit_completes(
    small_responses: tuple[str, Sizes], skill_id: str, arguments: dict[str, Any]
) -> None:
    base_url, _ = small_responses
    async with await connect(base_url, API_KEY) as client:
        task = await call_skill(client, skill_id, arguments)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED


@pytest.mark.parametrize(
    ("skill_id", "arguments"),
    [
        ("text", {"character": "x", "count": 101}),
        # Measured in UTF-8 bytes: 51 two-byte characters are 102 bytes.
        ("text", {"character": "é", "count": 51}),
        ("data", {"count": 87}),
    ],
)
async def test_a_result_over_the_limit_fails_the_task_and_is_never_sent(
    small_responses: tuple[str, Sizes],
    skill_id: str,
    arguments: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    base_url, sizes = small_responses
    async with await connect(base_url, API_KEY) as client:
        task = await call_skill(client, skill_id, arguments)

    assert sizes.runs == 1
    assert_failed_for_size(task)
    oversized = arguments.get("character", SECRET[0]) * 20
    assert oversized not in caplog.text


async def test_a_question_over_the_limit_fails_the_task(
    small_responses: tuple[str, Sizes],
) -> None:
    base_url, _ = small_responses
    async with await connect(base_url, API_KEY) as client:
        asked = await say(client, "?" * 100)
        too_long = await say(client, "?" * 101)

    assert asked.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert_failed_for_size(too_long)
