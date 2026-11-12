"""Tests for A2A task operations: send, get, list and cancel.

Requests travel over real localhost HTTP to an ``AgentApp`` served by uvicorn.
The server shares the test's event loop, so tests can pause a skill mid-run.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    APIKey,
    APIKeyAuth,
    HostConfig,
    Inject,
    RunContext,
    Services,
    agent,
    skill,
)
from local_server import ServeLocally

# Test-only values; real applications load keys from the environment.
ALICE_KEY = "alice-test-key"
BOB_KEY = "bob-test-key"

# A2A 1.0 JSON-RPC error codes.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
UNSUPPORTED_OPERATION = -32004


class Job:
    """A per-run service that records whether its cleanup ran."""

    def __init__(self) -> None:
        self.closed = False


async def open_job() -> AsyncIterator[Job]:
    job = Job()
    try:
        yield job
    finally:
        job.closed = True


@agent(version="1.0.0")
class Worker(Agent):
    """Does work that waits until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.jobs: list[Job] = []
        self.invocation_ids: list[str] = []

    @skill("wait", tags=["test"])
    async def wait(self, ctx: RunContext, job: Inject[Job]) -> str:
        """Wait to be released."""
        self.jobs.append(job)
        self.invocation_ids.append(ctx.invocation_id)
        self.started.set()
        await self.release.wait()
        return "done"


def worker_app(worker: Worker, host: HostConfig) -> AgentApp:
    services = Services()
    services.add_scoped(Job, open_job)
    return AgentApp(agent=worker, host=host, services=services)


def api_key_host(public_url: str) -> HostConfig:
    keys = {ALICE_KEY: APIKey("alice"), BOB_KEY: APIKey("bob")}
    return HostConfig(public_url=public_url, auth=APIKeyAuth(keys=keys))


def anonymous_host(public_url: str) -> HostConfig:
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


@pytest.fixture
def worker() -> Worker:
    return Worker()


@pytest.fixture
async def http(serve: ServeLocally, worker: Worker) -> AsyncIterator[httpx.AsyncClient]:
    def make_app(public_url: str) -> AgentApp:
        return worker_app(worker, api_key_host(public_url))

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        yield h


async def call(
    http: httpx.AsyncClient,
    method: str,
    params: Mapping[str, Any],
    key: str | None = ALICE_KEY,
) -> dict[str, Any]:
    """Make one JSON-RPC call and return its decoded response body."""
    headers = {"A2A-Version": "1.0"}
    if key is not None:
        headers["X-API-Key"] = key
    body = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    response = await http.post("/a2a", json=body, headers=headers)
    assert response.status_code == 200
    result: dict[str, Any] = response.json()
    return result


def wait_message(**message_fields: str) -> dict[str, Any]:
    return {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": "run"}],
        "metadata": {"agentframe.skill": "wait"},
        **message_fields,
    }


async def send(
    http: httpx.AsyncClient,
    *,
    return_immediately: bool,
    key: str | None = ALICE_KEY,
) -> dict[str, Any]:
    params = {
        "message": wait_message(),
        "configuration": {"returnImmediately": return_immediately},
    }
    task: dict[str, Any] = (await call(http, "SendMessage", params, key))["result"][
        "task"
    ]
    return task


async def start_waiting_task(
    http: httpx.AsyncClient, worker: Worker, key: str = ALICE_KEY
) -> dict[str, Any]:
    """Send a non-blocking ``wait`` request and return once the skill is running."""
    task = await send(http, return_immediately=True, key=key)
    await asyncio.wait_for(worker.started.wait(), timeout=5)
    return task


async def get_task(
    http: httpx.AsyncClient, task_id: str, key: str = ALICE_KEY
) -> dict[str, Any]:
    task: dict[str, Any] = (await call(http, "GetTask", {"id": task_id}, key))["result"]
    return task


async def poll_until_finished(http: httpx.AsyncClient, task_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            task = await get_task(http, task_id)
            if task["status"]["state"] != "TASK_STATE_WORKING":
                return task
            await asyncio.sleep(0.01)


def error_code(response: Mapping[str, Any]) -> int:
    code: int = response["error"]["code"]
    return code


# Sending: blocking by default, or return immediately and poll


async def test_send_blocks_until_the_task_finishes(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    sending = asyncio.create_task(send(http, return_immediately=False))
    await asyncio.wait_for(worker.started.wait(), timeout=5)
    assert not sending.done()

    worker.release.set()
    task = await sending

    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"] == [{"text": "done"}]


async def test_non_blocking_send_returns_at_once_and_the_caller_polls(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    task = await send(http, return_immediately=True)

    assert task["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING")

    worker.release.set()
    finished = await poll_until_finished(http, task["id"])

    assert finished["status"]["state"] == "TASK_STATE_COMPLETED"
    assert finished["artifacts"][0]["parts"] == [{"text": "done"}]


# Callers see only their own tasks


async def test_list_returns_only_the_callers_tasks(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    worker.release.set()
    alice_task = await send(http, return_immediately=False, key=ALICE_KEY)
    await send(http, return_immediately=False, key=BOB_KEY)

    listed = (await call(http, "ListTasks", {}, ALICE_KEY))["result"]["tasks"]

    assert [task["id"] for task in listed] == [alice_task["id"]]


async def test_another_callers_task_looks_like_it_does_not_exist(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    alice_task = await start_waiting_task(http, worker, key=ALICE_KEY)
    missing_id = str(uuid.uuid4())

    for task_id in (alice_task["id"], missing_id):
        get = await call(http, "GetTask", {"id": task_id}, BOB_KEY)
        cancel = await call(http, "CancelTask", {"id": task_id}, BOB_KEY)
        follow_up = await call(
            http, "SendMessage", {"message": wait_message(taskId=task_id)}, BOB_KEY
        )

        assert error_code(get) == TASK_NOT_FOUND
        assert error_code(cancel) == TASK_NOT_FOUND
        assert error_code(follow_up) == TASK_NOT_FOUND
    still_mine = await get_task(http, alice_task["id"], ALICE_KEY)
    assert still_mine["status"]["state"] == "TASK_STATE_WORKING"


# Cancellation


async def test_cancel_stops_the_skill_and_cleans_up_its_services(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    task = await start_waiting_task(http, worker)

    cancelled = (await call(http, "CancelTask", {"id": task["id"]}))["result"]

    assert cancelled["status"]["state"] == "TASK_STATE_CANCELED"
    [job] = worker.jobs
    assert job.closed
    stored = await get_task(http, task["id"])
    assert stored["status"]["state"] == "TASK_STATE_CANCELED"
    assert "artifacts" not in stored


async def test_cancelled_task_reports_the_run_invocation_id(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    task = await start_waiting_task(http, worker)

    cancelled = (await call(http, "CancelTask", {"id": task["id"]}))["result"]

    assert cancelled["metadata"]["invocation_id"] == worker.invocation_ids[0]
    message = cancelled["status"]["message"]["parts"][0]["text"]
    assert message == "The task was cancelled."


async def test_cancelling_a_finished_task_is_not_cancelable(
    http: httpx.AsyncClient, worker: Worker
) -> None:
    worker.release.set()
    task = await send(http, return_immediately=False)

    response = await call(http, "CancelTask", {"id": task["id"]})

    assert error_code(response) == TASK_NOT_CANCELABLE
    stored = await get_task(http, task["id"])
    assert stored["status"]["state"] == "TASK_STATE_COMPLETED"


# Anonymous callers share one task namespace


async def test_anonymous_callers_reach_tasks_by_id_but_cannot_list_them(
    serve: ServeLocally, worker: Worker
) -> None:
    def make_app(public_url: str) -> AgentApp:
        return worker_app(worker, anonymous_host(public_url))

    async with serve(make_app) as base_url, httpx.AsyncClient(base_url=base_url) as h:
        worker.release.set()
        task = await send(h, return_immediately=False, key=None)
        fetched = await call(h, "GetTask", {"id": task["id"]}, key=None)
        listed = await call(h, "ListTasks", {}, key=None)

    assert fetched["result"]["id"] == task["id"]
    assert error_code(listed) == UNSUPPORTED_OPERATION
    assert "authenticated" in listed["error"]["message"]
