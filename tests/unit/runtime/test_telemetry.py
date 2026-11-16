"""OpenTelemetry spans and metrics for local runs (``Runtime(tracing=...)``).

Each test gives the runtime its own SDK providers with in-memory exporters,
as an application would, so nothing touches OpenTelemetry's global state.
"""

import sys
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind, StatusCode

from agentframe import (
    Agent,
    AgentApp,
    AllowAnonymous,
    DefinitionError,
    FinishReason,
    HostConfig,
    InputValidationError,
    ModelCapabilities,
    ModelResponse,
    RunContext,
    Runtime,
    ToolCall,
    TracingConfig,
    Usage,
    agent,
    skill,
    tool,
)
from agentframe.runtime.correlation import TraceContext
from agentframe.runtime.telemetry import Telemetry
from agentframe.testing import ScriptedModel

SECRET = "secret-content-4b1d"
TOOL_MODEL = ModelCapabilities(context_window=128_000, tools=True)
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


@tool(description="Look up an order's status.")
def lookup_order(order_id: str) -> str:
    return f"{order_id} shipped {SECRET}"


@agent(name="Orders", version="1.0.0")
class Orders(Agent):
    """Answers questions about orders."""

    instructions = f"Answer questions about orders. {SECRET}"

    @skill("ask", tags=["orders"])
    async def answer(self, question: str, ctx: RunContext) -> str:
        """Answer with the lookup tool."""
        return await ctx.generate(question, tools=[lookup_order])

    @skill("fail", tags=["orders"])
    async def fail(self, ctx: RunContext) -> str:
        """Raise an exception whose message holds content."""
        raise ValueError(SECRET)


@dataclass
class Recorded:
    """What one runtime's providers recorded."""

    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader

    def finished(self) -> dict[str, ReadableSpan]:
        return {span.name: span for span in self.spans.get_finished_spans()}

    def points(self, name: str) -> list[Any]:
        data = self.metrics.get_metrics_data()
        assert data is not None
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


@pytest.fixture
def recorded() -> Recorded:
    return Recorded(InMemorySpanExporter(), InMemoryMetricReader())


@pytest.fixture
def tracing(recorded: Recorded) -> TracingConfig:
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(recorded.spans))
    return TracingConfig(
        tracer_provider=tracer_provider,
        meter_provider=MeterProvider(metric_readers=[recorded.metrics]),
    )


def answer(text: str) -> ModelResponse:
    usage = Usage(input_tokens=40, output_tokens=10, total_tokens=50, estimated=False)
    return ModelResponse(text=text, usage=usage, finish_reason=FinishReason.COMPLETED)


def orders_with_one_lookup() -> Orders:
    lookup = ToolCall(id="call_1", name="lookup_order", arguments='{"order_id": "A1"}')
    model = ScriptedModel(
        [lookup, answer(f"A1 has shipped. {SECRET}")], capabilities=TOOL_MODEL
    )
    return Orders(model=model, tools=[lookup_order])


async def ask(runtime: Runtime) -> None:
    await runtime.run(
        orders_with_one_lookup(),
        skill="ask",
        arguments={"question": f"Where is A1? {SECRET}"},
    )


def all_attribute_values(recorded: Recorded) -> Iterator[object]:
    for span in recorded.spans.get_finished_spans():
        yield span.name
        yield from (span.attributes or {}).values()
        for event in span.events:
            yield event.name
            yield from (event.attributes or {}).values()
    data = recorded.metrics.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    yield from (point.attributes or {}).values()


# Spans


async def test_a_run_records_run_skill_model_and_tool_spans(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    await ask(Runtime(tracing=tracing))

    spans = recorded.finished()
    assert spans.keys() == {
        "invoke_agent Orders",
        "skill ask",
        "chat",
        "execute_tool lookup_order",
    }
    run = spans["invoke_agent Orders"]
    skill_span = spans["skill ask"]
    tool_span = spans["execute_tool lookup_order"]
    assert run.parent is None
    assert skill_span.parent is not None
    assert skill_span.parent.span_id == run.context.span_id
    assert tool_span.parent is not None
    assert tool_span.parent.span_id == skill_span.context.span_id
    assert run.attributes is not None
    assert run.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert run.attributes["gen_ai.agent.name"] == "Orders"
    assert run.attributes["agentframe.skill"] == "ask"
    assert tool_span.attributes == {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "lookup_order",
        "gen_ai.tool.call.id": "call_1",
    }


async def test_each_model_call_is_a_genai_client_span(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    await ask(Runtime(tracing=tracing))

    chats = [s for s in recorded.spans.get_finished_spans() if s.name == "chat"]
    assert len(chats) == 2
    final = chats[1]
    assert final.kind == SpanKind.CLIENT
    assert final.attributes is not None
    assert final.attributes["gen_ai.operation.name"] == "chat"
    # ScriptedModel names no provider.
    assert final.attributes["gen_ai.provider.name"] == "_OTHER"
    assert final.attributes["gen_ai.usage.input_tokens"] == 40
    assert final.attributes["gen_ai.usage.output_tokens"] == 10
    assert final.attributes["gen_ai.response.finish_reasons"] == ("completed",)


async def test_no_content_is_recorded(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    await ask(Runtime(tracing=tracing))
    with pytest.raises(ValueError, match=SECRET):
        await Runtime(tracing=tracing).run(orders_with_one_lookup(), skill="fail")

    for value in all_attribute_values(recorded):
        assert SECRET not in str(value)


async def test_a_failed_run_records_its_error_class_not_its_message(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    with pytest.raises(ValueError, match=SECRET):
        await Runtime(tracing=tracing).run(orders_with_one_lookup(), skill="fail")

    run = recorded.finished()["invoke_agent Orders"]
    assert run.status.status_code == StatusCode.ERROR
    assert run.status.description is None
    assert run.attributes is not None
    assert run.attributes["error.type"] == "ValueError"
    assert run.events == ()


async def test_an_unknown_skill_id_never_names_a_span(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    with pytest.raises(InputValidationError):
        await Runtime(tracing=tracing).run(orders_with_one_lookup(), skill=SECRET)

    assert recorded.finished().keys() == {"invoke_agent Orders", "skill"}


async def test_an_unknown_tool_name_never_names_a_span(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    made_up = ToolCall(id="call_1", name=SECRET, arguments="{}")
    model = ScriptedModel([made_up, answer("Sorry.")], capabilities=TOOL_MODEL)
    orders = Orders(model=model, tools=[lookup_order])

    await Runtime(tracing=tracing).run(orders, skill="ask", arguments={"question": "?"})

    tool_span = recorded.finished()["execute_tool"]
    assert tool_span.attributes == {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.call.id": "call_1",
    }


async def test_a_run_continues_a_given_trace_context(tracing: TracingConfig) -> None:
    telemetry = Telemetry(tracing)
    parent = TraceContext(TRACEPARENT)

    with telemetry.span("child", parent=parent):
        headers = telemetry.outbound_headers(parent)

    trace_id, span_id = headers["traceparent"].split("-")[1:3]
    assert trace_id == "0af7651916cd43dd8448eb211c80319c"
    # The peer's parent is this span, not the caller's.
    assert span_id != "b7ad6b7169203331"


def test_without_tracing_outbound_requests_carry_the_incoming_context() -> None:
    parent = TraceContext(TRACEPARENT)

    assert Telemetry(None).outbound_headers(parent) == {"traceparent": TRACEPARENT}
    assert Telemetry(None).outbound_headers(None) == {}


# Metrics


async def test_runs_and_their_duration_are_counted_by_skill(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    await ask(Runtime(tracing=tracing))

    [runs] = recorded.points("agentframe.runs")
    assert runs.value == 1
    assert dict(runs.attributes) == {
        "gen_ai.agent.name": "Orders",
        "agentframe.skill": "ask",
    }
    [duration] = recorded.points("agentframe.run.duration")
    assert duration.count == 1
    assert recorded.points("agentframe.run.errors") == []


async def test_failed_runs_are_counted_by_error_class(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    with pytest.raises(ValueError, match=SECRET):
        await Runtime(tracing=tracing).run(orders_with_one_lookup(), skill="fail")

    [errors] = recorded.points("agentframe.run.errors")
    assert errors.value == 1
    assert errors.attributes["error.type"] == "ValueError"


async def test_model_calls_record_token_usage_and_duration(
    recorded: Recorded, tracing: TracingConfig
) -> None:
    await ask(Runtime(tracing=tracing))

    # The first, tool-calling reply has unknown usage, so only the second counts.
    tokens = {
        point.attributes["gen_ai.token.type"]: point.sum
        for point in recorded.points("gen_ai.client.token.usage")
    }
    assert tokens == {"input": 40, "output": 10}
    [duration] = recorded.points("gen_ai.client.operation.duration")
    assert duration.count == 2


async def test_accounting_is_the_same_with_tracing_on_or_off(
    tracing: TracingConfig,
) -> None:
    arguments = {"question": "Where is A1?"}
    traced = await Runtime(tracing=tracing).run(
        orders_with_one_lookup(), skill="ask", arguments=arguments
    )
    untraced = await Runtime().run(
        orders_with_one_lookup(), skill="ask", arguments=arguments
    )

    assert traced.usage == untraced.usage
    assert (traced.model_calls, traced.tool_calls) == (
        untraced.model_calls,
        untraced.tool_calls,
    )


# Configuration


def test_tracing_without_the_otel_extra_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry makes the import fail, as when the package is absent.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)

    with pytest.raises(DefinitionError, match=r"agentframe-py\[otel\]"):
        Runtime(tracing=TracingConfig())


def test_tracing_is_given_to_the_app_or_its_runtime_not_both(
    tracing: TracingConfig,
) -> None:
    with pytest.raises(DefinitionError, match="tracing"):
        AgentApp(
            agent=orders_with_one_lookup(),
            host=HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous()),
            runtime=Runtime(),
            tracing=tracing,
        )
