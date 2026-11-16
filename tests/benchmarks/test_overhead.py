"""Overhead benchmarks: what AgentFrame adds around a model call.

Opt-in: these run only with ``AGENTFRAME_BENCHMARK=1`` and are skipped by the
default ``uv run pytest``::

    AGENTFRAME_BENCHMARK=1 uv run pytest tests/benchmarks -m benchmark -s

Every model is a ``ScriptedModel``, so no provider is called and the numbers
are AgentFrame's own cost: validation, limits, accounting, hosting and the
A2A protocol. They measure:

- a local run: ``Runtime.run`` of a model-backed skill, minus the bare model
  call;
- a hosted run: one blocking skill call from the official A2A SDK client to
  an ``AgentApp`` over localhost HTTP;
- an A2A hop: a local orchestrator run whose skill makes one ``Remote.call``
  to that hosted agent, minus a local run;
- throughput at the admission limit (``HostConfig.max_concurrent_runs``),
  then with twice as many concurrent callers as the limit admits. Here the
  scripted model takes a fixed 20 ms per reply, so runs overlap as they would
  with a real model.

Each result is printed and, when ``AGENTFRAME_BENCHMARK_REPORT`` names a file
(CI uses ``$GITHUB_STEP_SUMMARY``), appended to it as a Markdown table row.
Each test fails when its number passes a ceiling set well above the recorded
baseline (docs/guides/performance.md), so only a clear regression fails it.
Client and server share one event loop and one CPU here, so treat the numbers
as a relative baseline, not as a server's capacity.
"""

import asyncio
import os
import statistics
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Literal

import httpx
import pytest
from a2a.client import A2AClientError, Client
from a2a.types import TaskState

from a2a_client import call_skill, connect
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AllowAnonymous,
    FileAgentRegistry,
    HostConfig,
    Message,
    ModelRequest,
    ModelResponse,
    ModelSettings,
    Remote,
    RunContext,
    Runtime,
    agent,
    skill,
)
from agentframe.testing import ScriptedModel
from local_server import ServeLocally

if os.environ.get("AGENTFRAME_BENCHMARK") != "1":
    pytest.skip("set AGENTFRAME_BENCHMARK=1 to run", allow_module_level=True)

pytestmark = pytest.mark.benchmark

# Ceilings: several times the recorded baseline, so a slower CI runner passes
# and only a clear regression fails. Milliseconds unless named otherwise.
LOCAL_RUN_OVERHEAD_CEILING_MS = 1.0
HOSTED_RUN_CEILING_MS = 50.0
HOP_OVERHEAD_CEILING_MS = 400.0
MIN_RUNS_PER_SECOND = 50.0

WARMUP = 20
LOCAL_RUNS = 2_000
HOSTED_RUNS = 300
HOPS = 20
ADMISSION_LIMIT = 8
THROUGHPUT_REQUESTS = 800
MODEL_LATENCY_SECONDS = 0.02

type Specialist = Remote[Literal["specialist"]]


@agent(version="1.0.0")
class Answerer(Agent):
    """Answers with one model call."""

    @skill("answer", tags=["benchmark"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer a question with the model."""
        return await ctx.generate(question)


@agent(version="1.0.0")
class Relay(Agent):
    """Passes a question to the specialist agent over A2A."""

    @skill("relay", tags=["benchmark"])
    async def relay(
        self, question: str, specialist: Specialist, ctx: RunContext
    ) -> str:
        """Ask the specialist's ``answer`` skill."""
        answer: str = await specialist.call("answer", question=question)
        return answer


class SlowScriptedModel(ScriptedModel):
    """A ``ScriptedModel`` that takes ``MODEL_LATENCY_SECONDS`` to reply.

    The latency stands in for a real model's, so concurrent runs overlap and
    fill the host's admission slots.
    """

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await asyncio.sleep(MODEL_LATENCY_SECONDS)
        return await super().generate(request)


def answerer(runs: int) -> Answerer:
    """Return an ``Answerer`` whose model has a reply for ``runs`` calls."""
    return Answerer(model=ScriptedModel(["ok"] * runs))


def anonymous_app(agent: Agent, public_url: str, *, limit: int = 32) -> AgentApp:
    """Serve ``agent`` anonymously; for a localhost benchmark only."""
    host = HostConfig(
        public_url=public_url, auth=AllowAnonymous(), max_concurrent_runs=limit
    )
    return AgentApp(agent=agent, host=host)


async def timings_ms(call: Callable[[], Awaitable[object]], count: int) -> list[float]:
    """Await ``call`` ``WARMUP`` times untimed, then ``count`` times timed."""
    for _ in range(WARMUP):
        await call()
    timings = []
    for _ in range(count):
        started = time.perf_counter()
        await call()
        timings.append((time.perf_counter() - started) * 1000)
    return timings


def p95(timings: list[float]) -> float:
    """Return the 95th percentile."""
    return statistics.quantiles(timings, n=20)[18]


def report(name: str, value: float, unit: str, detail: str) -> None:
    """Print one result and append it to ``AGENTFRAME_BENCHMARK_REPORT``."""
    print(f"\n{name}: {value:.3f} {unit} ({detail})")
    path = os.environ.get("AGENTFRAME_BENCHMARK_REPORT")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as output:
        output.write(f"- **{name}:** {value:.3f} {unit} ({detail})\n")


@pytest.fixture
async def hosted(serve: ServeLocally) -> AsyncIterator[str]:
    """Serve an ``Answerer`` over localhost HTTP and yield its base URL."""
    runs = 2 * WARMUP + HOSTED_RUNS + HOPS
    async with serve(lambda url: anonymous_app(answerer(runs), url)) as url:
        yield url


async def test_local_run_overhead() -> None:
    model = ScriptedModel(["ok"] * (WARMUP + LOCAL_RUNS))
    request = ModelRequest(
        messages=(Message(role="user", content="hi"),),
        settings=ModelSettings(max_output_tokens=100),
    )
    bare = await timings_ms(lambda: model.generate(request), LOCAL_RUNS)
    runtime = Runtime()
    agent = answerer(WARMUP + LOCAL_RUNS)

    runs = await timings_ms(
        lambda: runtime.run(agent, skill="answer", arguments={"question": "hi"}),
        LOCAL_RUNS,
    )

    overhead = statistics.median(runs) - statistics.median(bare)
    report(
        "Local run overhead",
        overhead,
        "ms",
        f"median of {LOCAL_RUNS}; run p95 {p95(runs):.3f} ms",
    )
    assert overhead < LOCAL_RUN_OVERHEAD_CEILING_MS


async def test_hosted_run_latency(hosted: str) -> None:
    async with await connect(hosted) as client:

        async def call() -> None:
            task = await call_skill(client, "answer", {"question": "hi"})
            assert task.status.state == TaskState.TASK_STATE_COMPLETED

        runs = await timings_ms(call, HOSTED_RUNS)

    latency = statistics.median(runs)
    report(
        "Hosted run latency",
        latency,
        "ms",
        f"median of {HOSTED_RUNS}; p95 {p95(runs):.3f} ms",
    )
    assert latency < HOSTED_RUN_CEILING_MS


async def test_a2a_hop_overhead(hosted: str, tmp_path: Path) -> None:
    agents = tmp_path / "agents.yaml"
    card_url = f"{hosted}/.well-known/agent-card.json"
    agents.write_text(f"- name: specialist\n  url: {card_url}\n")
    local = answerer(WARMUP + HOPS)
    async with AgentDirectory(sources=[FileAgentRegistry(agents)]) as directory:
        runtime = Runtime(directory=directory)
        local_runs = await timings_ms(
            lambda: runtime.run(local, skill="answer", arguments={"question": "hi"}),
            HOPS,
        )

        hops = await timings_ms(
            lambda: runtime.run(Relay(), skill="relay", arguments={"question": "hi"}),
            HOPS,
        )

    overhead = statistics.median(hops) - statistics.median(local_runs)
    report(
        "A2A hop overhead",
        overhead,
        "ms",
        f"median of {HOPS}; hop p95 {p95(hops):.3f} ms",
    )
    assert overhead < HOP_OVERHEAD_CEILING_MS


def is_busy(error: A2AClientError) -> bool:
    """Whether the host refused the request because it was full (HTTP 503)."""
    cause = error.__cause__
    return (
        isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 503
    )


async def send_concurrently(
    client: Client, callers: int, requests: int
) -> tuple[float, int, int]:
    """Send ``requests`` skill calls from ``callers`` concurrent callers.

    Returns:
        The seconds taken, the completed runs and the busy refusals.
    """
    remaining = requests
    completed = refused = 0

    async def caller() -> None:
        nonlocal remaining, completed, refused
        while remaining > 0:
            remaining -= 1
            try:
                task = await call_skill(client, "answer", {"question": "hi"})
            except A2AClientError as error:
                if not is_busy(error):
                    raise
                refused += 1
                continue
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            completed += 1

    started = time.perf_counter()
    async with asyncio.TaskGroup() as group:
        for _ in range(callers):
            group.create_task(caller())
    return time.perf_counter() - started, completed, refused


async def test_throughput_at_the_admission_limit(serve: ServeLocally) -> None:
    agent = Answerer(
        model=SlowScriptedModel(["ok"] * (WARMUP + 2 * THROUGHPUT_REQUESTS))
    )

    def app(url: str) -> AgentApp:
        return anonymous_app(agent, url, limit=ADMISSION_LIMIT)

    async with serve(app) as url, await connect(url) as client:
        await send_concurrently(client, 1, WARMUP)
        seconds, completed, refused = await send_concurrently(
            client, ADMISSION_LIMIT, THROUGHPUT_REQUESTS
        )
        overload = await send_concurrently(
            client, 2 * ADMISSION_LIMIT, THROUGHPUT_REQUESTS
        )

    runs_per_second = completed / seconds
    report(
        "Throughput at the admission limit",
        runs_per_second,
        "runs/s",
        f"{ADMISSION_LIMIT} callers, limit {ADMISSION_LIMIT}; {refused} refused",
    )
    overload_seconds, overload_completed, overload_refused = overload
    report(
        "Throughput at twice the limit",
        overload_completed / overload_seconds,
        "completed runs/s",
        f"{2 * ADMISSION_LIMIT} callers, limit {ADMISSION_LIMIT}; "
        f"{overload_refused} of {THROUGHPUT_REQUESTS} refused as busy",
    )
    assert refused == 0
    assert runs_per_second > MIN_RUNS_PER_SECOND
