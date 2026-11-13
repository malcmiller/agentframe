"""``agentframe run`` shuts down within its grace period on SIGTERM.

Runs the real command in a child process, with one blocking request and one
non-blocking run in flight that would both outlive the 1-second grace period
of ``cli_targets:create_quick_shutdown_app``.
"""

import asyncio
import contextlib
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from agentframe.hosting.card import SKILL_CALL_EXTENSION_URI

SUPPORT_DIRECTORY = Path(__file__).parent.parent / "support"
GRACE_SECONDS = 1
MARGIN_SECONDS = 2


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def send_sleep(
    http: httpx.AsyncClient, *, return_immediately: bool
) -> httpx.Response:
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"data": {}}],
        "metadata": {"agentframe.skill": "sleep"},
    }
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": message,
            "configuration": {"returnImmediately": return_immediately},
        },
    }
    headers = {"A2A-Version": "1.0", "A2A-Extensions": SKILL_CALL_EXTENSION_URI}
    return await http.post("/a2a", json=body, headers=headers)


async def wait_until_ready(http: httpx.AsyncClient) -> None:
    async with asyncio.timeout(15):
        while True:
            with contextlib.suppress(httpx.TransportError):
                if (await http.get("/ready")).status_code == 200:
                    return
            await asyncio.sleep(0.1)


async def test_sigterm_exits_within_the_grace_period_with_work_in_flight() -> None:
    port = free_port()
    command = Path(sys.executable).parent / "agentframe"
    process = subprocess.Popen(
        [command, "run", "cli_targets:create_quick_shutdown_app", "--port", str(port)],
        cwd=SUPPORT_DIRECTORY,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30
        ) as http:
            await wait_until_ready(http)
            background = await send_sleep(http, return_immediately=True)
            blocking = asyncio.create_task(send_sleep(http, return_immediately=False))
            await asyncio.sleep(0.3)

            signalled_at = time.monotonic()
            process.send_signal(signal.SIGTERM)
            while process.poll() is None:
                await asyncio.sleep(0.05)
            seconds = time.monotonic() - signalled_at
            with contextlib.suppress(httpx.HTTPError):
                await blocking

        assert background.status_code == 200
        assert seconds <= GRACE_SECONDS + MARGIN_SECONDS
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
