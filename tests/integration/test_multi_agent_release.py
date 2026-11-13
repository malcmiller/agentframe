"""Multi-agent release acceptance: independently deployed agents over real A2A.

Each agent runs as its own ``agentframe run`` process on localhost, wired
through the environment variables ``dev_settings()`` reads, with a
development API key per caller. The planner is ``examples/multi_agent``'s;
the research agent is the example's with a stand-in model that reports 300
tokens per brief (``tests/support/release_targets.py``). The official A2A
client calls them, as any A2A caller would.

Identity across agents is service mode here: the planner calls with its own
API key (``Service(APIKeyCredential)``). Delegated Entra on-behalf-of calls
are covered by the opt-in ``entra`` test in ``test_on_behalf_of.py``.
"""

import asyncio
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
import pytest
import yaml
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import Task, TaskState

from a2a_client import call_skill, connect
from a2a_peer import PeerExecutor, a2a_peer
from examples.multi_agent.planner import Plan, Source
from local_server import ServeLocally
from release_targets import BRIEF_TOKENS

ROOT = Path(__file__).parent.parent.parent
SUPPORT = ROOT / "tests" / "support"
CARD_PATH = "/.well-known/agent-card.json"
CLIENT_KEY = "client-key"
PLANNER_KEY = "planner-key"
RELAY_KEY = "relay-key"
STARTUP_SECONDS = 30
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TOPICS = ["tides", "weather", "waves"]


@dataclass
class AgentProcess:
    """One agent served by ``agentframe run``, logging to a file."""

    url: str
    process: subprocess.Popen[bytes]
    log: Path

    def started_runs(self) -> list[tuple[str, str]]:
        """Return each ``Run started`` line's invocation and root invocation IDs."""
        pattern = r"Run started \(invocation_id=(\S+), root_invocation_id=(\S+)\)"
        return re.findall(pattern, self.log.read_text())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_until_ready(agent: AgentProcess) -> None:
    deadline = time.monotonic() + STARTUP_SECONDS
    with httpx.Client(base_url=agent.url) as http:
        while agent.process.poll() is None and time.monotonic() < deadline:
            with contextlib.suppress(httpx.TransportError):
                if http.get("/ready").status_code == 200:
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
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("AGENTFRAME_")
    }
    environment |= {f"AGENTFRAME_{name}": value for name, value in settings.items()}
    environment |= {
        "AGENTFRAME_DEV": "1",
        "AGENTFRAME_PUBLIC_URL": url,
        "PYTHONPATH": os.pathsep.join([str(ROOT), str(SUPPORT)]),
        "PYTHONUNBUFFERED": "1",
    }
    log = directory / f"{app.replace(':', '.')}.{port}.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "agentframe", "run", app, "--port", str(port)],
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


@dataclass
class Agents:
    research: AgentProcess
    planner: AgentProcess


@pytest.fixture(scope="module")
def agents(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Agents]:
    """The research agent and the planner, each in its own process.

    The planner finds ``research-agent`` by name in its registry and calls it
    with its own key; the research agent verifies that key as ``planner``.
    """
    logs = tmp_path_factory.mktemp("logs")
    with agent_process(
        "release_targets:create_research_app",
        logs,
        DEV_API_KEYS=f"planner={PLANNER_KEY}",
    ) as research:
        wait_until_ready(research)
        with agent_process(
            "examples.multi_agent.planner:create_app",
            logs,
            AGENTS=f"research-agent={research.url}{CARD_PATH}",
            DEV_API_KEYS=f"developer={CLIENT_KEY}",
            DEV_REMOTE_KEYS=f"research-agent={PLANNER_KEY}",
        ) as planner:
            wait_until_ready(planner)
            yield Agents(research, planner)


async def plan(planner: AgentProcess, **metadata: object) -> Task:
    """Ask the planner to plan a beach day, researching three topics at once."""
    client = await connect(planner.url, CLIENT_KEY)
    try:
        return await call_skill(
            client,
            "plan",
            {"goal": "A beach day", "topics": TOPICS},
            metadata=metadata,
        )
    finally:
        await client.close()


def plan_result(task: Task) -> Plan:
    assert task.status.state == TaskState.TASK_STATE_COMPLETED, task.status
    [data] = get_data_parts(task.artifacts[0].parts)
    return Plan.model_validate(data["value"])


# Discovery, structured calls and free-text asks across processes


async def test_the_planner_fans_out_structured_calls_and_asks_to_the_research_agent(
    agents: Agents,
) -> None:
    result = plan_result(await plan(agents.planner))

    # directory.find found the research agent's card by its skill.
    assert result.researchers == ["Research"]
    # One call and one ask per topic, all at once, each with its own ceiling.
    assert [topic.topic for topic in result.topics] == TOPICS
    tides, weather, waves = result.topics
    assert tides.sources == [
        Source(title="Tide tables explained", url="https://example.org/tides"),
        Source(title="How the Moon moves the oceans", url="https://example.org/moon"),
    ]
    assert weather.sources == [
        Source(title="Reading a forecast", url="https://example.org/weather")
    ]
    assert waves.brief.startswith(
        "Brief: Brief me on waves for this goal: A beach day (for planner,"
    )
    assert len(agents.research.started_runs()) >= 2 * len(TOPICS)


# Identity, budget and deadline


async def test_the_research_agent_sees_the_planners_identity_and_deadline(
    agents: Agents,
) -> None:
    result = plan_result(
        await plan(agents.planner, **{"agentframe.deadline_ms": 30_000})
    )

    for topic in result.topics:
        # Verified from the planner's own API key: service identity.
        match = re.search(r"\(for (\S+), (\d+) s left\)$", topic.brief)
        assert match is not None, topic.brief
        caller, seconds_left = match.groups()
        assert caller == "planner"
        # The caller's 30 seconds, not the research agent's own 120.
        assert 0 < int(seconds_left) <= 30


async def test_the_research_agents_usage_is_charged_to_the_planner_once(
    agents: Agents,
) -> None:
    task = await plan(agents.planner)

    plan_result(task)
    usage = task.metadata["usage"]
    # Three briefs of 300 tokens each; finding sources uses no model.
    assert usage["total_tokens"] == len(TOPICS) * BRIEF_TOKENS
    assert usage["estimated"] is False
    # Only the planner's own model calls, of which it makes none.
    assert usage["model_calls"] == 0


# Correlation


async def test_both_processes_log_the_same_root_invocation_id(
    agents: Agents,
) -> None:
    task = await plan(agents.planner)

    plan_result(task)
    root = task.metadata["invocation_id"]
    assert (root, root) in agents.planner.started_runs()
    research_runs = [
        invocation
        for invocation, run_root in agents.research.started_runs()
        if run_root == root
    ]
    # One structured call and one ask per topic, each its own run.
    assert len(research_runs) == 2 * len(TOPICS)
    assert root not in research_runs


# Depth


async def test_a_recursive_configuration_fails_at_5_hops(
    agents: Agents, tmp_path: Path
) -> None:
    # The relay starts with `next` pointing at the research agent, since its
    # startup checks `next` can be reached, then is pointed at itself.
    registry = tmp_path / "agents.yaml"
    registry.write_text(
        yaml.safe_dump([{"name": "next", "url": agents.research.url + CARD_PATH}])
    )
    with agent_process(
        "release_targets:create_relay_app",
        tmp_path,
        DEV_REGISTRY=str(registry),
        DEV_API_KEYS=f"relay={RELAY_KEY},developer={CLIENT_KEY}",
        DEV_REMOTE_KEYS=f"next={RELAY_KEY}",
    ) as relay:
        wait_until_ready(relay)
        registry.write_text(
            yaml.safe_dump([{"name": "next", "url": relay.url + CARD_PATH}])
        )
        client = await connect(relay.url, CLIENT_KEY)
        task = await call_skill(client, "relay", {"text": "loop"})
        await client.close()
        log = relay.log.read_text()
        runs = relay.started_runs()

    assert task.status.state == TaskState.TASK_STATE_FAILED
    # The request itself, then hops 1 to 5; hop 6 is never sent.
    assert len(runs) == 6
    assert {root for _, root in runs} == {task.metadata["invocation_id"]}
    assert log.count("Maximum remote call depth of 5 exceeded") == 1


# Interoperability with a non-AgentFrame peer


async def test_a_plain_a2a_peer_receives_the_callers_traceparent_unchanged(
    agents: Agents, tmp_path: Path, serve: ServeLocally
) -> None:
    peer = PeerExecutor()
    async with serve(a2a_peer(peer)) as peer_url:
        registry = tmp_path / "agents.yaml"
        registry.write_text(
            yaml.safe_dump([{"name": "next", "url": peer_url + CARD_PATH}])
        )
        with agent_process(
            "release_targets:create_relay_app",
            tmp_path,
            DEV_REGISTRY=str(registry),
            DEV_API_KEYS=f"developer={CLIENT_KEY}",
            DEV_REMOTE_KEYS=f"next={RELAY_KEY}",
        ) as relay:
            # The relay's startup fetches the peer's card from this event loop.
            await asyncio.to_thread(wait_until_ready, relay)
            client = await connect(relay.url, CLIENT_KEY)
            task = await call_skill(
                client, "relay", {"text": "hello"}, headers={"traceparent": TRACEPARENT}
            )
            await client.close()

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    assert get_artifact_text(task.artifacts[0]) == "Echo: hello"
    [received] = peer.requests
    assert get_message_text(received.message) == "hello"
    assert received.headers["traceparent"] == TRACEPARENT
