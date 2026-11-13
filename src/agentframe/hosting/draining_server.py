"""A Uvicorn server that starts an ``AgentApp``'s shutdown on the signal.

Uvicorn waits for open requests before ASGI lifespan shutdown runs. Starting
the app's shutdown in Uvicorn's signal handler instead means new messages are
refused and the grace period is counted from the signal, so the whole
shutdown takes about one grace period, not two. Imported only by
``agentframe run``, because Uvicorn is an optional dependency.
"""

import math
from types import FrameType

import uvicorn

from agentframe.hosting.app import AgentApp


class DrainingServer(uvicorn.Server):
    """Serves ``app`` and starts its shutdown as soon as a signal arrives.

    Uvicorn's own wait for open requests is limited to the grace period, so
    blocking requests drain within it; the app's lifespan shutdown then
    waits only for what remains of it before cancelling leftover runs.

    Args:
        app: The app to serve.
        host: The address to bind to.
        port: The port to bind to.
    """

    def __init__(self, app: AgentApp, *, host: str, port: int) -> None:
        # noinspection PyProtectedMember
        grace_seconds = app._host.shutdown_grace_seconds
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            # Uvicorn takes whole seconds.
            timeout_graceful_shutdown=math.ceil(grace_seconds),
        )
        super().__init__(config)
        self._agent_app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Start the app's shutdown, then let Uvicorn handle the signal."""
        self._agent_app.begin_shutdown()
        super().handle_exit(sig, frame)
