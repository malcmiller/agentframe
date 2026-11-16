"""OpenTelemetry trace context across agents, over real localhost A2A.

The orchestrator is an ``AgentApp`` with ``tracing=``; the peer is the bare
official-SDK server from ``tests/support/a2a_peer.py``, which records the
headers it receives. Spans go to an in-memory exporter.
"""

import asyncio
from typing import Any, Literal

import pytest
from a2a.client import A2AClientError
from a2a.types import TaskState
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from a2a_client import call_skill, connect
from a2a_peer import PeerExecutor, a2a_peer
from agentframe import (
    Agent,
    AgentApp,
    AgentDirectory,
    AgentQuery,
    AgentReference,
    AgentRegistry,
    AllowAnonymous,
    HostConfig,
    Plan,
    PlanStep,
    Remote,
    RunContext,
    Runtime,
    TracingConfig,
    agent,
    skill,
)
from local_server import AppFactory, ServeLocally

type Peer = Remote[Literal["peer"]]

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
CALLER_SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{CALLER_SPAN_ID}-01"


class Listed(AgentRegistry):
    """Lists the agents served for one test by name."""

    def __init__(self, **urls: str) -> None:
        self.urls = urls

    async def get(self, agent_id: str) -> AgentReference:
        return AgentReference(agent_id, self._card(self.urls[agent_id]))

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return [AgentReference(n, self._card(u)) for n, u in self.urls.items()]

    @staticmethod
    def _card(url: str) -> str:
        return f"{url}/.well-known/agent-card.json"


@agent(name="Relay", description="Asks a peer.", version="0.1.0")
class Relay(Agent):
    """Asks the ``peer`` agent and returns its answer."""

    @skill(id="relay", description="Pass text on.", tags=["relay"])
    async def relay(self, text: str, peer: Peer, ctx: RunContext) -> str:
        return await peer.ask(text)


@agent(name="Planner", description="Runs reviewed plans.", version="0.1.0")
class Planner(Agent):
    """Runs the plan it is given over every registered agent's skills."""

    @skill(id="execute", description="Run a reviewed plan.", tags=["plans"])
    async def execute(self, plan: Plan, ctx: RunContext) -> str:
        result = await ctx.run_plan(plan, await ctx.remote_skills(all_registered=True))
        return str(result.value)


@agent(name="Echo", description="Echoes text.", version="0.1.0")
class Echo(Agent):
    """A specialist whose one skill a plan can call."""

    @skill(id="echo", description="Echo text.", tags=["echo"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        return text


@agent(name="Waiter", description="Waits to be released.", version="0.1.0")
class Waiter(Agent):
    """Holds its run open until the test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @skill(id="wait", description="Wait to be released.", tags=["test"])
    async def wait(self, ctx: RunContext) -> str:
        self.started.set()
        await self.release.wait()
        return "done"


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def metrics() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def tracing(
    spans: InMemorySpanExporter, metrics: InMemoryMetricReader
) -> TracingConfig:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    return TracingConfig(
        tracer_provider=tracer_provider,
        meter_provider=MeterProvider(metric_readers=[metrics]),
    )


def anonymous(public_url: str) -> HostConfig:
    return HostConfig(public_url=public_url, auth=AllowAnonymous())


def by_name(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


async def test_a_hosted_run_continues_the_caller_trace_and_passes_it_on(
    serve: ServeLocally, spans: InMemorySpanExporter, tracing: TracingConfig
) -> None:
    peer = PeerExecutor()

    def relay_app(directory: AgentDirectory) -> AppFactory:
        def make_app(public_url: str) -> AgentApp:
            return AgentApp(
                agent=Relay(),
                host=anonymous(public_url),
                directory=directory,
                tracing=tracing,
            )

        return make_app

    async with (
        serve(a2a_peer(peer)) as peer_url,
        AgentDirectory(sources=[Listed(peer=peer_url)]) as directory,
        serve(relay_app(directory)) as url,
        await connect(url) as client,
    ):
        task = await call_skill(
            client, "relay", {"text": "tides"}, headers={"traceparent": TRACEPARENT}
        )

    assert task.status.state == TaskState.TASK_STATE_COMPLETED
    finished = by_name(spans)
    run = finished["invoke_agent Relay"]
    remote = finished["invoke_agent peer"]
    assert run.kind == SpanKind.SERVER
    assert run.parent is not None
    assert format(run.parent.span_id, "016x") == CALLER_SPAN_ID
    assert format(run.context.trace_id, "032x") == TRACE_ID
    assert remote.kind == SpanKind.CLIENT
    [request] = peer.requests
    _, trace_id, parent_id, _ = request.headers["traceparent"].split("-")
    assert trace_id == TRACE_ID
    assert parent_id == format(remote.context.span_id, "016x")


async def test_each_plan_step_is_a_span(
    serve: ServeLocally, spans: InMemorySpanExporter, tracing: TracingConfig
) -> None:
    def echo_app(public_url: str) -> AgentApp:
        return AgentApp(agent=Echo(), host=anonymous(public_url))

    plan = Plan(
        steps=[PlanStep(id="say", use="echo.echo", arguments={"text": "hi"})],
        result="say",
    )
    async with (
        serve(echo_app) as echo_url,
        AgentDirectory(sources=[Listed(echo=echo_url)]) as directory,
    ):
        result = await Runtime(directory=directory, tracing=tracing).run(
            Planner(), skill="execute", arguments={"plan": plan.model_dump()}
        )

    assert result.value == "hi"
    step = by_name(spans)["plan_step say"]
    assert step.attributes is not None
    assert step.attributes["agentframe.plan.use"] == "echo.echo"
    assert step.attributes["agentframe.plan.step.status"] == "completed"


async def test_requests_refused_while_shutting_down_are_counted(
    serve: ServeLocally, metrics: InMemoryMetricReader, tracing: TracingConfig
) -> None:
    apps: list[AgentApp] = []

    def make_app(public_url: str) -> AgentApp:
        app = AgentApp(agent=Echo(), host=anonymous(public_url), tracing=tracing)
        apps.append(app)
        return app

    async with serve(make_app) as url, await connect(url) as client:
        apps[0].begin_shutdown()
        with pytest.raises(A2AClientError):
            await call_skill(client, "echo", {"text": "hi"})

    [point] = rejections(metrics)
    assert point.value == 1
    assert point.attributes["agentframe.rejection.reason"] == "shutting_down"


async def test_requests_refused_while_busy_are_counted(
    serve: ServeLocally, metrics: InMemoryMetricReader, tracing: TracingConfig
) -> None:
    waiter = Waiter()

    def make_app(public_url: str) -> AgentApp:
        host = HostConfig(
            public_url=public_url, auth=AllowAnonymous(), max_concurrent_runs=1
        )
        return AgentApp(agent=waiter, host=host, tracing=tracing)

    async with serve(make_app) as url, await connect(url) as client:
        running = asyncio.create_task(call_skill(client, "wait", {}))
        await waiter.started.wait()
        with pytest.raises(A2AClientError):
            await call_skill(client, "wait", {})
        waiter.release.set()
        await running

    [point] = rejections(metrics)
    assert point.value == 1
    assert point.attributes["agentframe.rejection.reason"] == "busy"


def rejections(metrics: InMemoryMetricReader) -> list[Any]:
    """Return the admission-rejection counter's data points."""
    data = metrics.get_metrics_data()
    assert data is not None
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "agentframe.admission.rejections"
        for point in metric.data.data_points
    ]
