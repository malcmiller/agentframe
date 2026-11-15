"""Run agents as separate processes for release acceptance tests.

Each agent is ``agentframe run`` on a free localhost port, wired through the
environment variables ``dev_settings()`` reads and logging to a file. The
plain A2A peer (``a2a_peer.py``) can run as its own process too.
"""

import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent.parent
SUPPORT = ROOT / "tests" / "support"
CARD_PATH = "/.well-known/agent-card.json"
STARTUP_SECONDS = 30


@dataclass
class AgentProcess:
    """One agent served in its own process, logging to a file."""

    url: str
    process: subprocess.Popen[bytes]
    log: Path

    @property
    def card_url(self) -> str:
        return self.url + CARD_PATH

    def started_runs(self) -> list[tuple[str, str]]:
        """Return each ``Run started`` line's invocation and root invocation IDs."""
        pattern = r"Run started \(invocation_id=(\S+), root_invocation_id=(\S+)\)"
        return re.findall(pattern, self.log.read_text())

    def runs_in(self, root: str) -> list[str]:
        """Return the invocation IDs of this agent's runs with root ``root``."""
        return [run for run, run_root in self.started_runs() if run_root == root]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until_ready(agent: AgentProcess, path: str = "/ready") -> None:
    """Wait until ``path`` answers 200, or fail with the agent's log."""
    deadline = time.monotonic() + STARTUP_SECONDS
    with httpx.Client(base_url=agent.url) as http:
        while agent.process.poll() is None and time.monotonic() < deadline:
            with contextlib.suppress(httpx.TransportError):
                if http.get(path).status_code == 200:
                    return
            time.sleep(0.1)
    raise AssertionError(f"{agent.url} did not start:\n{agent.log.read_text()}")


@contextlib.contextmanager
def agent_process(app: str, directory: Path, **settings: str) -> Iterator[AgentProcess]:
    """Start ``agentframe run app`` on a free port with ``dev_settings()`` wiring.

    Call ``wait_until_ready`` before using it.

    ``settings`` are environment variables without their ``AGENTFRAME_``
    prefix, such as ``DEV_API_KEYS="planner=planner-key"``.
    """
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    environment = {f"AGENTFRAME_{name}": value for name, value in settings.items()}
    environment |= {"AGENTFRAME_DEV": "1", "AGENTFRAME_PUBLIC_URL": url}
    command = ["-m", "agentframe", "run", app, "--port", str(port)]
    log = directory / f"{app.replace(':', '.')}.{port}.log"
    with _process(command, url, log, environment) as agent:
        yield agent


@contextlib.contextmanager
def peer_process(directory: Path) -> Iterator[AgentProcess]:
    """Start the plain A2A peer (``a2a_peer.py``) on a free port.

    Call ``wait_until_ready(peer, CARD_PATH)`` before using it.
    """
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    command = [str(SUPPORT / "peer_process.py"), str(port)]
    with _process(command, url, directory / f"peer.{port}.log", {}) as peer:
        yield peer


@contextlib.contextmanager
def _process(
    arguments: list[str], url: str, log: Path, settings: dict[str, str]
) -> Iterator[AgentProcess]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENTFRAME_")
    }
    environment |= settings | {
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(SUPPORT)]),
        "PYTHONUNBUFFERED": "1",
    }
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        yield AgentProcess(url, process, log)
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
