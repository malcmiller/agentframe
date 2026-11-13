"""Tests for concurrent run admission: ``HostConfig.max_concurrent_runs``.

Requests travel over real localhost HTTP to an ``AgentApp`` served by Uvicorn.
The server shares the test's event loop, so a test can hold a run open while
it sends more requests. Every host here admits one run at a time.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import httpx
import pytest
from a2a.client import A2AClientError
from a2a.types import Message

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    Clarification,
    DefinitionError,
    HostConfig,
    RoutingContext,
    RunContext,
    RunLimits,
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

BUSY = {"error": "The agent is busy; retry later."}


@agent(version="1.0.0")
class Worker(Agent):
    """Does work, some of which waits until the test releases it."""

    def __init__(self) -> None:
        self.waits = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @skill("wait", tags=["test"])
    async def wait(self, ctx: RunContext) -> str:
        """Wait to be released."""
        self.waits += 1
        self.started.set()
        await self.release.wait()
        return "done"

    @skill("quick", tags=["test"])
    async def quick(self, ctx: RunContext) -> str:
        """Finish at once."""
        return "ok"

    @skill("fail", tags=["test"])
    async def fail(self, ctx: RunContext) -> str:
        """Raise an exception."""
        raise RuntimeError("boom")

    @skill("slow", tags=["test"], limits=RunLimits(timeout_seconds=0.05))
    async def slow(self, ctx: RunContext) -> str:
        """Outlive the run's timeout."""
        await asyncio.sleep(10)
        return "too late"


class AskFirstRouter(SkillRouter):
    """Asks a question about a new message and runs ``quick`` on the answer."""

    async def route(
        self,
        conversation: Sequence[Message],
        skills: Sequence[SkillDefinition],
        ctx: RoutingContext,
    ) -> SkillCall | Clarification:
        if len(conversation) == 1:
            return Clarification("Which one?")
        return SkillCall("quick", {})


def one_run_app(worker: Worker, public_url: str) -> AgentApp:
    host = HostConfig(
        public_url=public_url,
        auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
        max_concurrent_runs=1,
    )
    return AgentApp(agent=worker, host=host, router=AskFirstRouter())


@pytest.fixture
def worker() -> Worker:
    return Worker()


@pytest.fixture
async def base_url(serve: ServeLocally, worker: Worker) -> AsyncIterator[str]:
    async with serve(lambda public_url: one_run_app(worker, public_url)) as url:
        yield url


@pytest.fixture
async def http(base_url: str) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def post(
    http: httpx.AsyncClient,
    method: str,
    params: Mapping[str, Any],
    *,
    skill_call: bool = True,
) -> httpx.Response:
    """Make one JSON-RPC call; ``skill_call`` activates the skill-call extension."""
    headers = {"A2A-Version": "1.0", "X-API-Key": API_KEY}
    if skill_call:
        headers["A2A-Extensions"] = SKILL_CALL_EXTENSION_URI
    body = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    return await http.post("/a2a", json=body, headers=headers)


def skill_message(skill_id: str, **message_fields: str) -> dict[str, Any]:
    return {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {}}],
        "metadata": {"agentframe.skill": skill_id},
        **message_fields,
    }


def text_message(text: str, **message_fields: str) -> dict[str, Any]:
    return {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
        **message_fields,
    }


async def send(
    http: httpx.AsyncClient,
    message: Mapping[str, Any],
    *,
    return_immediately: bool = False,
    skill_call: bool = True,
) -> httpx.Response:
    params = {
        "message": message,
        "configuration": {"returnImmediately": return_immediately},
    }
    return await post(http, "SendMessage", params, skill_call=skill_call)


def task_of(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


async def run_quick(http: httpx.AsyncClient) -> str:
    """Run the ``quick`` skill and return its task's final state."""
    state: str = task_of(await send(http, skill_message("quick")))["status"]["state"]
    return state


async def start_waiting(
    http: httpx.AsyncClient, worker: Worker, *, return_immediately: bool
) -> asyncio.Task[httpx.Response]:
    """Send a ``wait`` message and return once its skill is running."""
    sending = asyncio.create_task(
        send(http, skill_message("wait"), return_immediately=return_immediately)
    )
    await asyncio.wait_for(worker.started.wait(), timeout=5)
    return sending


async def listed_task_ids(http: httpx.AsyncClient) -> list[str]:
    tasks = (await post(http, "ListTasks", {})).json()["result"]["tasks"]
    return [task["id"] for task in tasks]


# Excess messages are refused at once


@pytest.mark.parametrize("first_returns_immediately", [False, True])
@pytest.mark.parametrize("second_returns_immediately", [False, True])
async def test_a_message_beyond_the_limit_is_refused_at_once(
    http: httpx.AsyncClient,
    worker: Worker,
    first_returns_immediately: bool,
    second_returns_immediately: bool,
) -> None:
    first = await start_waiting(
        http, worker, return_immediately=first_returns_immediately
    )

    refused = await send(
        http, skill_message("wait"), return_immediately=second_returns_immediately
    )

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "1"
    assert refused.json() == BUSY
    assert worker.waits == 1
    worker.release.set()
    first_task = task_of(await first)
    assert await listed_task_ids(http) == [first_task["id"]]


async def test_official_client_gets_a_clear_busy_error(
    base_url: str, http: httpx.AsyncClient, worker: Worker
) -> None:
    await start_waiting(http, worker, return_immediately=True)

    async with await connect(base_url, API_KEY) as client:
        with pytest.raises(A2AClientError) as raised:
            await call_skill(client, "quick")

    http_error = raised.value.__cause__
    assert isinstance(http_error, httpx.HTTPStatusError)
    assert http_error.response.status_code == 503
    assert http_error.response.json() == BUSY
    worker.release.set()


async def test_task_reads_lists_and_cancels_work_while_the_host_is_full(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    task = task_of(await (await start_waiting(http, worker, return_immediately=True)))

    fetched = (await post(http, "GetTask", {"id": task["id"]})).json()["result"]
    listed = await listed_task_ids(http)
    cancelled = (await post(http, "CancelTask", {"id": task["id"]})).json()["result"]

    assert fetched["status"]["state"] == "TASK_STATE_WORKING"
    assert listed == [task["id"]]
    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"


# The slot is freed however a run ends


@pytest.mark.parametrize(
    ("skill_id", "state"),
    [
        ("quick", "TASK_STATE_COMPLETED"),
        ("fail", "TASK_STATE_FAILED"),
        pytest.param("slow", "TASK_STATE_FAILED", id="timeout"),
    ],
)
async def test_the_slot_is_freed_when_a_run_ends(
    http: httpx.AsyncClient, skill_id: str, state: str
) -> None:
    task = task_of(await send(http, skill_message(skill_id)))

    assert task["status"]["state"] == state
    assert await run_quick(http) == "TASK_STATE_COMPLETED"


async def test_the_slot_is_freed_when_a_run_is_cancelled(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    task = task_of(await (await start_waiting(http, worker, return_immediately=True)))

    await post(http, "CancelTask", {"id": task["id"]})

    assert await run_quick(http) == "TASK_STATE_COMPLETED"


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(skill_message("quick", taskId="no-such-task"), id="unknown task"),
        pytest.param(
            {**skill_message("quick"), "metadata": {}}, id="malformed skill call"
        ),
    ],
)
async def test_the_slot_is_freed_when_a_message_is_refused_before_running(
    http: httpx.AsyncClient, worker: Worker, message: dict[str, Any]
) -> None:
    refused = await send(http, message)

    assert refused.status_code == 200
    assert "error" in refused.json()
    assert await run_quick(http) == "TASK_STATE_COMPLETED"


# A task waiting for input holds no slot


async def test_a_question_frees_the_slot_and_its_answer_needs_one(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    asked = task_of(await send(http, text_message("do it"), skill_call=False))
    assert asked["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert question_text(asked) == "Which one?"

    waiting = await start_waiting(http, worker, return_immediately=True)
    answer = text_message("that one", taskId=asked["id"])
    refused = await send(http, answer, skill_call=False)
    worker.release.set()
    await poll_until_done(http, task_of(await waiting)["id"])
    answered = task_of(await send(http, answer, skill_call=False))

    assert refused.status_code == 503
    assert answered["id"] == asked["id"]
    assert answered["status"]["state"] == "TASK_STATE_COMPLETED"


def question_text(task: Mapping[str, Any]) -> str:
    text: str = task["status"]["message"]["parts"][0]["text"]
    return text


async def poll_until_done(http: httpx.AsyncClient, task_id: str) -> None:
    async with asyncio.timeout(5):
        while True:
            task = (await post(http, "GetTask", {"id": task_id})).json()["result"]
            if task["status"]["state"] != "TASK_STATE_WORKING":
                return
            await asyncio.sleep(0.01)


# Configuration


@pytest.mark.parametrize("max_runs", [0, -1])
def test_fewer_than_one_concurrent_run_is_refused(max_runs: int) -> None:
    with pytest.raises(DefinitionError, match="max_concurrent_runs"):
        HostConfig(
            public_url="https://a.example.com",
            auth=AllowAnonymous(),
            max_concurrent_runs=max_runs,
        )
