"""Tests for readiness probes and graceful shutdown.

Each app is driven the way an ASGI server drives it: lifespan events by hand
and requests through httpx's in-process ASGI transport, all on the test's
event loop. Shutdown here starts at lifespan shutdown, as with any ASGI
server, or earlier through ``AgentApp.begin_shutdown`` as ``agentframe run``
calls it on the signal.
"""

import asyncio
import contextlib
import math
import signal
import time
import uuid
from collections.abc import AsyncIterator, Mapping, MutableMapping
from typing import Any

import httpx
import pytest

from agentframe import (
    Agent,
    AgentApp,
    AgentFrameError,
    APIKey,
    APIKeyAuth,
    DefinitionError,
    HostConfig,
    Inject,
    ModelCapabilities,
    RunContext,
    Services,
    agent,
    skill,
)
from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI
from agentframe.hosting.draining_server import DrainingServer
from agentframe.testing import ScriptedModel

# Test-only value; real applications load keys from the environment.
API_KEY = "test-key"

MARGIN_SECONDS = 1.0
"""How much longer than the grace period a shutdown may take in these tests."""


class Connection:
    """A per-run service that records whether it was cleaned up."""

    def __init__(self) -> None:
        self.closed = False


@agent(version="1.0.0")
class Worker(Agent):
    """Waits, holding a per-run connection, until the test releases it."""

    def __init__(self) -> None:
        self.running = 0
        self.finished = 0
        self.release = asyncio.Event()
        self.connections: list[Connection] = []

    @skill("wait", tags=["test"])
    async def wait(self, ctx: RunContext, connection: Inject[Connection]) -> str:
        """Wait to be released."""
        self.connections.append(connection)
        self.running += 1
        await self.release.wait()
        self.finished += 1
        return "done"


def make_app(worker: Worker, grace_seconds: float = 30) -> AgentApp:
    async def open_connection() -> AsyncIterator[Connection]:
        connection = Connection()
        yield connection
        connection.closed = True

    services = Services()
    services.add_scoped(Connection, open_connection)
    host = HostConfig(
        public_url="https://worker.example.com",
        auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
        shutdown_grace_seconds=grace_seconds,
    )
    return AgentApp(agent=worker, host=host, services=services)


class Lifespan:
    """Sends an app the ASGI lifespan events a server would."""

    def __init__(self, app: AgentApp) -> None:
        self._app = app
        self._events: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        self._replies: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()
        self._running: asyncio.Task[None] | None = None

    async def startup(self) -> str:
        """Start the app and return the reply's type."""
        scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
        self._running = asyncio.create_task(
            self._app(scope, self._events.get, self._replies.put)
        )
        await self._events.put({"type": "lifespan.startup"})
        reply_type: str = (await self._replies.get())["type"]
        if reply_type == "lifespan.startup.failed":
            # Starlette re-raises the startup error after reporting it.
            with contextlib.suppress(AgentFrameError):
                await self._running
        return reply_type

    async def shutdown(self) -> float:
        """Shut the app down and return how many seconds it took."""
        assert self._running is not None
        started_at = time.monotonic()
        await self._events.put({"type": "lifespan.shutdown"})
        reply = await self._replies.get()
        await self._running
        assert reply["type"] == "lifespan.shutdown.complete"
        return time.monotonic() - started_at


@pytest.fixture
def worker() -> Worker:
    return Worker()


@pytest.fixture
async def http(app: AgentApp) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as h:
        yield h


@pytest.fixture
def app(worker: Worker) -> AgentApp:
    return make_app(worker)


@pytest.fixture
async def lifespan(app: AgentApp) -> Lifespan:
    lifespan = Lifespan(app)
    assert await lifespan.startup() == "lifespan.startup.complete"
    return lifespan


async def post(
    http: httpx.AsyncClient, method: str, params: Mapping[str, Any]
) -> httpx.Response:
    headers = {
        "A2A-Version": "1.0",
        "X-API-Key": API_KEY,
        "A2A-Extensions": SKILL_CALL_EXTENSION_URI,
    }
    body = {"jsonrpc": "2.0", "id": "1", "method": method, "params": params}
    return await http.post("/a2a", json=body, headers=headers)


async def send_wait(
    http: httpx.AsyncClient, *, return_immediately: bool
) -> httpx.Response:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {}}],
        "metadata": {"agentframe.skill": "wait"},
    }
    params = {
        "message": message,
        "configuration": {"returnImmediately": return_immediately},
    }
    return await post(http, "SendMessage", params)


async def start_waiting(
    http: httpx.AsyncClient, worker: Worker, *, return_immediately: bool
) -> asyncio.Task[httpx.Response]:
    """Send a ``wait`` message and return once its skill is running."""
    already_running = worker.running
    sending = asyncio.create_task(
        send_wait(http, return_immediately=return_immediately)
    )
    async with asyncio.timeout(5):
        while worker.running == already_running:
            await asyncio.sleep(0.01)
    return sending


def task_of(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()["result"]["task"]
    return task


async def task_state(http: httpx.AsyncClient, task_id: str) -> str:
    task = (await post(http, "GetTask", {"id": task_id})).json()["result"]
    state: str = task["status"]["state"]
    return state


async def probe(http: httpx.AsyncClient, path: str) -> tuple[int, Any]:
    response = await http.get(path)
    return response.status_code, response.json()


READY = (200, {"status": "ready"})
NOT_READY = (503, {"status": "not ready"})
ALIVE = (200, {"status": "ok"})


# Probes


async def test_ready_only_between_startup_and_shutdown(
    app: AgentApp, http: httpx.AsyncClient
) -> None:
    lifespan = Lifespan(app)
    before_startup = await probe(http, "/ready")
    alive_before_startup = await probe(http, "/health")
    await lifespan.startup()
    after_startup = await probe(http, "/ready")
    await lifespan.shutdown()

    assert before_startup == NOT_READY
    assert alive_before_startup == ALIVE
    assert after_startup == READY
    assert await probe(http, "/ready") == NOT_READY
    assert await probe(http, "/health") == ALIVE


async def test_not_ready_when_a_startup_check_fails() -> None:
    @agent(version="1.0.0")
    class Writer(Agent):
        """Has a model whose context window is unknown."""

        @skill("write", tags=["test"])
        async def write(self, ctx: RunContext) -> str:
            """Write something."""
            return await ctx.generate("Write.")

    model = ScriptedModel(capabilities=ModelCapabilities(context_window=None))
    app = AgentApp(
        agent=Writer(model=model),
        host=HostConfig(
            public_url="https://writer.example.com",
            auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
        ),
    )

    assert await Lifespan(app).startup() == "lifespan.startup.failed"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        assert await probe(http, "/ready") == NOT_READY


async def test_probes_need_no_credentials_and_are_not_on_the_card(
    http: httpx.AsyncClient, lifespan: Lifespan
) -> None:
    card = (await http.get("/.well-known/agent-card.json")).json()

    assert await probe(http, "/ready") == READY
    assert "/ready" not in str(card)
    assert "/health" not in str(card)


# Draining


async def test_while_draining_new_messages_are_refused_but_tasks_still_work(
    app: AgentApp, http: httpx.AsyncClient, worker: Worker, lifespan: Lifespan
) -> None:
    running = task_of(
        await (await start_waiting(http, worker, return_immediately=True))
    )

    app.begin_shutdown()
    refused = await send_wait(http, return_immediately=True)
    state_while_draining = await task_state(http, running["id"])
    cancelled = (await post(http, "CancelTask", {"id": running["id"]})).json()

    assert refused.status_code == 503
    assert refused.headers["Retry-After"] == "1"
    assert refused.json() == {"error": "The agent is shutting down; retry later."}
    assert worker.running == 1
    assert await probe(http, "/ready") == NOT_READY
    assert await probe(http, "/health") == ALIVE
    assert state_while_draining == "TASK_STATE_WORKING"
    assert cancelled["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    await lifespan.shutdown()


async def test_runs_that_finish_within_the_grace_period_complete(
    http: httpx.AsyncClient, worker: Worker, lifespan: Lifespan
) -> None:
    blocking = await start_waiting(http, worker, return_immediately=False)
    background = task_of(
        await (await start_waiting(http, worker, return_immediately=True))
    )

    shutting_down = asyncio.create_task(lifespan.shutdown())
    await asyncio.sleep(0.1)
    worker.release.set()
    seconds = await shutting_down

    assert seconds < 1
    assert worker.finished == 2
    assert task_of(await blocking)["status"]["state"] == "TASK_STATE_COMPLETED"
    assert await task_state(http, background["id"]) == "TASK_STATE_COMPLETED"


async def test_an_idle_app_shuts_down_at_once(lifespan: Lifespan) -> None:
    assert await lifespan.shutdown() < 0.5


# Cancelling after the grace period


@pytest.mark.parametrize("grace_seconds", [0.3])
async def test_runs_still_going_after_the_grace_period_are_cancelled(
    worker: Worker, grace_seconds: float
) -> None:
    app = make_app(worker, grace_seconds=grace_seconds)
    lifespan = Lifespan(app)
    await lifespan.startup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        blocking = await start_waiting(http, worker, return_immediately=False)
        background = task_of(
            await (await start_waiting(http, worker, return_immediately=True))
        )

        seconds = await lifespan.shutdown()

        assert grace_seconds <= seconds <= grace_seconds + MARGIN_SECONDS
        assert task_of(await blocking)["status"]["state"] == "TASK_STATE_CANCELED"
        assert await task_state(http, background["id"]) == "TASK_STATE_CANCELED"
    assert worker.finished == 0
    assert [connection.closed for connection in worker.connections] == [True, True]


async def test_the_grace_period_is_measured_from_the_first_shutdown_signal(
    worker: Worker,
) -> None:
    app = make_app(worker, grace_seconds=0.5)
    lifespan = Lifespan(app)
    await lifespan.startup()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        await start_waiting(http, worker, return_immediately=True)

        app.begin_shutdown()
        await asyncio.sleep(0.4)
        app.begin_shutdown()
        seconds = await lifespan.shutdown()

    assert seconds < 0.4
    assert worker.connections[0].closed


# agentframe run's Uvicorn server


def test_the_uvicorn_server_starts_draining_on_the_signal(worker: Worker) -> None:
    app = make_app(worker, grace_seconds=2.5)
    server = DrainingServer(app, host="127.0.0.1", port=8000)

    server.handle_exit(signal.SIGTERM, None)

    # noinspection PyProtectedMember
    assert app._lifecycle.shutting_down
    assert server.should_exit
    assert server.config.timeout_graceful_shutdown == math.ceil(2.5)


# Configuration


@pytest.mark.parametrize("grace_seconds", [0, -1, math.nan])
def test_the_grace_period_must_be_positive(grace_seconds: float) -> None:
    with pytest.raises(DefinitionError, match="shutdown_grace_seconds"):
        HostConfig(
            public_url="https://a.example.com",
            auth=APIKeyAuth(keys={API_KEY: APIKey("alice")}),
            shutdown_grace_seconds=grace_seconds,
        )
