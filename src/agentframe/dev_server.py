"""``agentframe dev``: run every agent of ``agentframe.yaml`` locally, wired together.

Each agent runs as its own ``agentframe run`` process on ``127.0.0.1``, started
in file order, each one only after the previous one answers ``/ready``, so an
agent's startup check can find the agents listed before it. The command:

- checks that every port is free before starting anything;
- writes ``.agentframe/agents.yaml`` next to ``agentframe.yaml``: the agents'
  names and local Agent Card URLs, without keys;
- generates one random API key per agent for this session. Each agent calls
  the others with its own key, and they verify it as that agent's name. The
  keys reach the agents only through their environment (see
  ``agentframe.dev``) and are never written to disk or logged;
- also lets every agent accept ``AGENTFRAME_DEV_CLIENT_KEY``, when set in its
  own environment, as caller ``developer``, so a person or test can call them;
- prints every agent's output prefixed with ``[agent-name]``.

Any startup failure, or an agent exiting later, stops all agents and fails
the command. Ctrl+C or SIGTERM sends SIGTERM to every agent, waits for their
graceful shutdown, then kills any still running.
"""

import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import IO

import httpx
import yaml
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH

from agentframe.dev import (
    AGENTS_VARIABLE,
    API_KEYS_VARIABLE,
    DEV_VARIABLE,
    PUBLIC_URL_VARIABLE,
    REGISTRY_VARIABLE,
    REMOTE_KEYS_VARIABLE,
    format_pairs,
)
from agentframe.hosting.app import READY_PATH
from agentframe.project import ProjectAgent, load_project

CLIENT_KEY_VARIABLE = "AGENTFRAME_DEV_CLIENT_KEY"
CLIENT_CALLER = "developer"
REGISTRY_FILE = Path(".agentframe") / "agents.yaml"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30.0

_HOST = "127.0.0.1"
# HostConfig's default shutdown grace period, plus time for the process to exit.
_SHUTDOWN_WAIT_SECONDS = 35.0
_POLL_SECONDS = 0.25


class DevError(Exception):
    """A problem ``agentframe dev`` reports as one line after stopping every agent."""


@dataclass
class _Child:
    agent: ProjectAgent
    process: subprocess.Popen[bytes]
    output: threading.Thread


def run_dev(
    project_file: Path,
    *,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
) -> None:
    """Start every agent in ``project_file`` and run them until stopped.

    Args:
        project_file: The ``agentframe.yaml`` file. Agents run in its directory.
        startup_timeout: Seconds each agent has to answer ``/ready``.

    Raises:
        DefinitionError: The project file is invalid.
        DevError: A port is in use, an agent failed to start or become ready
            in time, or exited while running. Every agent is stopped first.
    """
    agents = load_project(project_file)
    client_key = _client_key()
    _check_ports_free(agents)
    directory = project_file.resolve().parent
    registry = _write_registry(directory, agents)
    keys = {agent.name: secrets.token_urlsafe(32) for agent in agents}
    stop = threading.Event()
    children: list[_Child] = []
    with _stop_on_signals(stop):
        try:
            for agent in agents:
                environment = child_environment(
                    agent, agents, keys, client_key, registry
                )
                children.append(_start(agent, environment, directory))
                _wait_until_ready(children[-1], startup_timeout, stop)
            if not stop.is_set():
                _announce_ready(agents)
                _wait_for_stop(children, stop)
        finally:
            _stop_all(children)


def child_environment(
    agent: ProjectAgent,
    agents: Sequence[ProjectAgent],
    keys: Mapping[str, str],
    client_key: str | None,
    registry: Path,
) -> dict[str, str]:
    """Return the environment one agent's process runs with.

    Args:
        agent: The agent to start.
        agents: Every agent of the project.
        keys: The API key each agent calls the others with, by agent name.
        client_key: The key a person or test calls agents with, if any.
        registry: The generated registry file.

    Returns:
        This process's environment plus the agent's development settings.
    """
    others = [other for other in agents if other.name != agent.name]
    accepted = {other.name: keys[other.name] for other in others}
    if client_key is not None:
        accepted[CLIENT_CALLER] = client_key
    # The generated registry replaces any exported AGENTFRAME_AGENTS, which
    # would otherwise list the same names twice.
    not_passed_on = {CLIENT_KEY_VARIABLE, AGENTS_VARIABLE}
    environment = {
        name: value for name, value in os.environ.items() if name not in not_passed_on
    }
    environment.update(
        {
            DEV_VARIABLE: "1",
            PUBLIC_URL_VARIABLE: _base_url(agent),
            REGISTRY_VARIABLE: str(registry),
            API_KEYS_VARIABLE: format_pairs(accepted),
            REMOTE_KEYS_VARIABLE: format_pairs(
                {other.name: keys[agent.name] for other in others}
            ),
            # Each output line is shown as soon as it is written.
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _client_key() -> str | None:
    key = os.environ.get(CLIENT_KEY_VARIABLE, "").strip()
    # The key travels inside comma-separated name=key pairs.
    if "," in key or "=" in key:
        raise DevError(f"{CLIENT_KEY_VARIABLE} must not contain ',' or '='.")
    return key or None


def _check_ports_free(agents: Sequence[ProjectAgent]) -> None:
    for agent in agents:
        with socket.socket() as probe:
            # Uvicorn binds with SO_REUSEADDR too, so a port only held by
            # recently closed connections counts as free, as it will be.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((_HOST, agent.port))
            except OSError:
                raise DevError(
                    f"Port {agent.port} for agent '{agent.name}' is already in use."
                ) from None


def _write_registry(directory: Path, agents: Sequence[ProjectAgent]) -> Path:
    registry = directory / REGISTRY_FILE
    registry.parent.mkdir(exist_ok=True)
    # Keeps the generated folder out of the user's repository.
    (registry.parent / ".gitignore").write_text("*\n")
    entries = [
        {"name": agent.name, "url": _base_url(agent) + AGENT_CARD_WELL_KNOWN_PATH}
        for agent in agents
    ]
    registry.write_text(yaml.safe_dump(entries, sort_keys=False))
    return registry


def _start(agent: ProjectAgent, environment: dict[str, str], cwd: Path) -> _Child:
    command = [sys.executable, "-m", "agentframe", "run", agent.app]
    command += ["--host", _HOST, "--port", str(agent.port)]
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # A terminal's Ctrl+C reaches only this command, which then signals
        # each agent once; a second signal would make Uvicorn skip the grace.
        start_new_session=True,
    )
    assert process.stdout is not None
    output = threading.Thread(
        target=_copy_output, args=(agent.name, process.stdout), daemon=True
    )
    output.start()
    return _Child(agent, process, output)


_print_lock = threading.Lock()


def _print(line: str) -> None:
    with _print_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _copy_output(name: str, stream: IO[bytes]) -> None:
    for raw_line in stream:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        _print(f"[{name}] {line}")


def _wait_until_ready(child: _Child, timeout: float, stop: threading.Event) -> None:
    name = child.agent.name
    deadline = time.monotonic() + timeout
    with httpx.Client(timeout=1) as http:
        while not stop.is_set():
            exit_code = child.process.poll()
            if exit_code is not None:
                raise DevError(
                    f"Agent '{name}' exited with code {exit_code} before it was "
                    "ready; see its log above."
                )
            try:
                response = http.get(_base_url(child.agent) + READY_PATH)
                if response.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                raise DevError(f"Agent '{name}' was not ready within {timeout:g} s.")
            stop.wait(_POLL_SECONDS)


def _announce_ready(agents: Sequence[ProjectAgent]) -> None:
    for agent in agents:
        _print(f"agentframe dev: {agent.name} is ready at {_base_url(agent)}")
    _print("agentframe dev: press Ctrl+C to stop all agents.")


def _wait_for_stop(children: Sequence[_Child], stop: threading.Event) -> None:
    while not stop.wait(_POLL_SECONDS):
        for child in children:
            exit_code = child.process.poll()
            if exit_code is not None:
                raise DevError(
                    f"Agent '{child.agent.name}' exited with code {exit_code}; "
                    "stopped the others."
                )


def _stop_all(children: Sequence[_Child]) -> None:
    for child in children:
        if child.process.poll() is None:
            child.process.terminate()
    deadline = time.monotonic() + _SHUTDOWN_WAIT_SECONDS
    for child in children:
        try:
            child.process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.process.kill()
            child.process.wait()
        # The output ends when the process does; this prints its last lines.
        child.output.join(timeout=1)


@contextmanager
def _stop_on_signals(stop: threading.Event) -> Iterator[None]:
    """Set ``stop`` on SIGINT or SIGTERM, instead of raising, while in use."""

    def handle(_number: int, _frame: FrameType | None) -> None:
        stop.set()

    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.signal(number, handle) for number in signals}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _base_url(agent: ProjectAgent) -> str:
    return f"http://{_HOST}:{agent.port}"
