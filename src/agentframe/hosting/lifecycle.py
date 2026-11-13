"""Readiness and the graceful-shutdown deadline of a hosted agent.

An ``AgentApp`` is ready once its startup checks have passed, until shutdown
starts. Shutdown starts once: on the server's shutdown signal when the server
reports it (``agentframe run`` does), otherwise at ASGI lifespan shutdown.
The grace period is measured from that moment, so a later start changes
nothing and a shutdown never waits two grace periods.
"""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager

from agentframe.hosting.admission import RunAdmission

_POLL_SECONDS = 0.05
"""How often draining checks whether the running runs have finished."""


class Lifecycle:
    """Whether the app is ready, and when its shutdown's grace period ends.

    Args:
        grace_seconds: The host's ``shutdown_grace_seconds``.
    """

    def __init__(self, grace_seconds: float) -> None:
        self._grace_seconds = grace_seconds
        self._started = False
        self._shutdown_deadline: float | None = None
        self._open_sends = 0

    @property
    def ready(self) -> bool:
        """Whether startup succeeded and shutdown has not started."""
        return self._started and not self.shutting_down

    @property
    def shutting_down(self) -> bool:
        """Whether shutdown has started; new messages are then refused."""
        return self._shutdown_deadline is not None

    def mark_started(self) -> None:
        """Record that the startup checks passed."""
        self._started = True

    def begin_shutdown(self) -> None:
        """Start shutdown now, unless it has already started.

        It only reads the clock and sets a field, so a server may call it
        from a signal handler.
        """
        if self._shutdown_deadline is None:
            self._shutdown_deadline = time.monotonic() + self._grace_seconds

    @contextmanager
    def track_send(self) -> Iterator[None]:
        """Count one admitted ``SendMessage`` until its response is ready.

        A run releases its slot as it publishes its last state, slightly
        before the SDK delivers that state as the response, so draining
        waits for both.
        """
        self._open_sends += 1
        try:
            yield
        finally:
            self._open_sends -= 1

    async def wait_for_runs(self, admission: RunAdmission) -> None:
        """Wait until runs and sends are done or the grace period has ended.

        Starts shutdown first if nothing has yet.
        """
        self.begin_shutdown()
        assert self._shutdown_deadline is not None
        while (admission.running or self._open_sends) and (
            time.monotonic() < self._shutdown_deadline
        ):
            await asyncio.sleep(_POLL_SECONDS)
