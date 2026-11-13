"""``agentframe dev`` starts real agents from ``agentframe.yaml`` and wires them.

Each test runs the real command in a child process, in a temporary project
directory whose ``agentframe.yaml`` points at ``tests/support/dev_targets.py``.
"""

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from a2a.helpers import get_artifact_text
from a2a.types import TaskState

from a2a_client import call_skill, connect

SUPPORT_DIRECTORY = Path(__file__).parent.parent / "support"
CLIENT_KEY = "developer-test-key"
STARTUP_SECONDS = 30
# The agents are idle, so they stop well within HostConfig's 30-second grace.
STOP_SECONDS = 10


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def write_project(directory: Path, agents: dict[str, tuple[str, int]]) -> Path:
    project = {
        "agents": {
            name: {"app": app, "port": port} for name, (app, port) in agents.items()
        }
    }
    path = directory / "agentframe.yaml"
    path.write_text(yaml.safe_dump(project, sort_keys=False))
    return path


def start_dev(
    project: Path, *extra: str, client_key: str | None = CLIENT_KEY
) -> subprocess.Popen[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SUPPORT_DIRECTORY)
    if client_key is not None:
        environment["AGENTFRAME_DEV_CLIENT_KEY"] = client_key
    return subprocess.Popen(
        [sys.executable, "-m", "agentframe", "dev", "--config", str(project), *extra],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@contextlib.contextmanager
def dev_running(project: Path, *extra: str) -> Iterator[subprocess.Popen[str]]:
    process = start_dev(project, *extra)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


async def wait_until_ready(base_url: str, process: subprocess.Popen[str]) -> None:
    async with (
        httpx.AsyncClient(base_url=base_url) as http,
        asyncio.timeout(STARTUP_SECONDS),
    ):
        while process.poll() is None:
            with contextlib.suppress(httpx.TransportError):
                if (await http.get("/ready")).status_code == 200:
                    return
            await asyncio.sleep(0.1)
    raise AssertionError(f"agentframe dev exited: {process.communicate()}")


@pytest.fixture
def ports() -> tuple[int, int]:
    return free_port(), free_port()


@pytest.fixture
def two_agents(tmp_path: Path, ports: tuple[int, int]) -> Path:
    research_port, planner_port = ports
    return write_project(
        tmp_path,
        {
            "research-agent": ("dev_targets:create_research_app", research_port),
            "planner": ("dev_targets:create_planner_app", planner_port),
        },
    )


async def test_the_planner_calls_the_researcher_by_name_with_a_dev_key(
    two_agents: Path, ports: tuple[int, int]
) -> None:
    planner_url = f"http://127.0.0.1:{ports[1]}"
    with dev_running(two_agents) as dev:
        await wait_until_ready(planner_url, dev)
        client = await connect(planner_url, CLIENT_KEY)
        task = await call_skill(client, "plan")
        keys_task = await call_skill(client, "dev_keys")
        await client.close()

        dev.send_signal(signal.SIGINT)
        output, errors = dev.communicate(timeout=STOP_SECONDS)

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Called by planner"
    assert dev.returncode == 0
    assert all(is_free(port) for port in ports)

    registry = two_agents.parent / ".agentframe" / "agents.yaml"
    assert yaml.safe_load(registry.read_text()) == [
        {
            "name": "research-agent",
            "url": f"http://127.0.0.1:{ports[0]}/.well-known/agent-card.json",
        },
        {"name": "planner", "url": f"{planner_url}/.well-known/agent-card.json"},
    ]
    keys = [
        pair.partition("=")[2]
        for pair in get_artifact_text(keys_task.artifacts[0]).split(",")
    ]
    assert CLIENT_KEY in keys
    assert len(keys) == 3
    for key in keys:
        assert key not in output + errors
        assert key not in registry.read_text()

    assert "[research-agent]" in output
    assert "[planner] INFO:     Run started (invocation_id=" in output
    assert "agentframe dev: planner is ready at" in output


async def test_a_port_in_use_stops_before_starting_anything(
    two_agents: Path, ports: tuple[int, int]
) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", ports[1]))
        taken.listen()
        with dev_running(two_agents) as dev:
            _, errors = dev.communicate(timeout=STARTUP_SECONDS)

    assert dev.returncode == 1
    assert errors.strip() == (
        f"agentframe: error: Port {ports[1]} for agent 'planner' is already in use."
    )
    assert is_free(ports[0])


async def test_an_import_error_stops_the_agents_already_started(
    tmp_path: Path, ports: tuple[int, int]
) -> None:
    project = write_project(
        tmp_path,
        {
            "research-agent": ("dev_targets:create_research_app", ports[0]),
            "planner": ("no_such_module:create_app", ports[1]),
        },
    )
    with dev_running(project) as dev:
        output, errors = dev.communicate(timeout=STARTUP_SECONDS)

    assert dev.returncode == 1
    assert "[planner] agentframe: error: Cannot import module" in output
    assert errors.strip() == (
        "agentframe: error: Agent 'planner' exited with code 1 before it was "
        "ready; see its log above."
    )
    assert is_free(ports[0])


async def test_an_agent_not_ready_in_time_is_stopped(
    tmp_path: Path, ports: tuple[int, int]
) -> None:
    project = write_project(
        tmp_path, {"slow": ("dev_targets:create_never_ready_app", ports[0])}
    )
    with dev_running(project, "--startup-timeout", "2") as dev:
        _, errors = dev.communicate(timeout=STARTUP_SECONDS)

    assert dev.returncode == 1
    assert errors.strip() == (
        "agentframe: error: Agent 'slow' was not ready within 2 s."
    )
    pid = int((tmp_path / "never_ready.pid").read_text())
    deadline = time.monotonic() + STOP_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError("The agent that was not ready is still running.")
