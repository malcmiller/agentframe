"""Serve an ``AgentApp`` over real localhost HTTP for end-to-end tests."""

import asyncio
import contextlib
import socket
import time
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette

from agentframe import AgentApp, AgentFrameError

# Usually an AgentApp; tests of non-AgentFrame peers serve plain SDK apps.
type AppFactory = Callable[[str], AgentApp | Starlette]
type ServeLocally = Callable[[AppFactory], AbstractAsyncContextManager[str]]


@asynccontextmanager
async def serve_locally(make_app: AppFactory) -> AsyncIterator[str]:
    """Serve an ``AgentApp`` (or another ASGI app) over real HTTP on a free
    localhost port.

    The server runs in the test's event loop and stops when the block exits.

    Args:
        make_app: Builds the app from the server's base URL, so the Agent Card
            can advertise the address the server actually listens on.

    Yields:
        The base URL, for example ``"http://127.0.0.1:54321"``.
    """
    # Bind first so the URL is known before the app (and its card) is built.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        server = uvicorn.Server(uvicorn.Config(make_app(base_url), log_level="warning"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield base_url
        finally:
            server.should_exit = True
            await serving
    finally:
        listener.close()


async def lifespan_startup(app: AgentApp) -> dict[str, Any]:
    """Run the app's ASGI lifespan startup and return the server's reply.

    The reply's ``type`` is ``lifespan.startup.complete`` or
    ``lifespan.startup.failed``, with the error in ``message``.
    """
    events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await events.put({"type": "lifespan.startup"})
    replies: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await events.get()

    async def send(message: Any) -> None:
        replies.append(message)
        await events.put({"type": "lifespan.shutdown"})

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    # Starlette re-raises a startup error after reporting it.
    with contextlib.suppress(AgentFrameError):
        await app(scope, receive, send)
    return replies[0]


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
