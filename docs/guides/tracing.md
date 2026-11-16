# Tracing and metrics

AgentFrame can record OpenTelemetry spans and metrics for every run: the run,
its skill, each model call, each tool call, each remote call and each plan
step. Tracing is off unless you turn it on, and it records names, IDs, counts,
durations and error class names only, never content.

Accounting, limits and the `RunResult` work the same with tracing on or off.
See the [design](../design/tracing.md) for the rationale and the
[`TracingConfig` reference](../api/configuration.md#tracingconfig) for the
constructor.

## Turning tracing on

Install the `otel` extra, which adds the OpenTelemetry API only:

```shell
pip install "agentframe-py[otel]"
```

Your application owns the OpenTelemetry SDK: providers, processors,
exporters, sampling and resource attributes. Install the SDK and an exporter
yourself (for example `opentelemetry-sdk` and
`opentelemetry-exporter-otlp`), then pass `tracing=TracingConfig(...)` to
`Runtime` or `AgentApp`:

```python
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from agentframe import AgentApp, HostConfig, TracingConfig


def create_app(agent, host: HostConfig) -> AgentApp:
    tracer_provider = TracerProvider()  # Add your span processor and exporter.
    meter_provider = MeterProvider()  # Add your metric reader.
    return AgentApp(
        agent=agent,
        host=host,
        tracing=TracingConfig(
            tracer_provider=tracer_provider, meter_provider=meter_provider
        ),
    )
```

`TracingConfig()` with no providers uses OpenTelemetry's global providers,
which your application or `opentelemetry-instrument` sets. AgentFrame never
sets global providers or starts an exporter. Asking for tracing without the
`otel` extra raises `DefinitionError`. Give `tracing=` to the `AgentApp` or to
its `Runtime`, not both.

## Spans

| Span | Kind | When | Attributes |
| --- | --- | --- | --- |
| `invoke_agent {agent}` | `SERVER` for an A2A request, otherwise `INTERNAL` | One per run | `gen_ai.operation.name`, `gen_ai.agent.name`, `agentframe.invocation_id`, `agentframe.skill` once known, `error.type` on failure |
| `skill {skill}` | `INTERNAL` | The selected skill running | `gen_ai.agent.name`, `agentframe.skill`, `agentframe.invocation_id` |
| `chat {model}` | `CLIENT` | One logical model call, retries included (each retry is a `retry` event) | `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.request.max_tokens`, `gen_ai.usage.input_tokens` / `output_tokens` when known, `gen_ai.response.finish_reasons` |
| `execute_tool {tool}` | `INTERNAL` | One tool call | `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id` |
| `invoke_agent {remote}` | `CLIENT` | One `Remote.ask` or `call`, including plan steps and remote tools | `gen_ai.operation.name`, `gen_ai.agent.name` (the remote's configured name), `agentframe.remote.skill` for `call`, usage tokens when the peer reports them |
| `plan_step {step}` | `INTERNAL` | One plan step | `agentframe.plan.step`, `agentframe.plan.use`, `agentframe.plan.step.status` |

Model spans follow the OpenTelemetry
[GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/),
so GenAI-aware dashboards show them. `OllamaModel` names its provider
`ollama` and `FoundryModel` `azure.ai.openai`; a custom model may set
`provider_name` and `model_name` attributes, otherwise its provider is
`_OTHER`. Routing's model calls appear under the run span, before the skill
span. A failed span has status `ERROR` and `error.type`, the exception's class
name; the exception's message is never recorded, since it may hold content.

## Trace context across agents

With tracing on, a hosted run continues a valid incoming W3C `traceparent`
(and `tracestate`) as its parent span, and every outbound A2A request carries
a new `traceparent` naming the remote call's span as its parent. The whole
request, across AgentFrame agents and any other A2A agent that honors trace
context, is one trace. Baggage is never read or sent.

Without tracing, AgentFrame keeps its earlier behavior: a valid incoming trace
context is passed on unchanged to every peer, and no spans are created.
Trace context is never authentication or authorization.

## Metrics

| Metric | Type | Unit | Attributes |
| --- | --- | --- | --- |
| `agentframe.runs` | Counter | `{run}` | `gen_ai.agent.name`, `agentframe.skill`, `error.type` on failure |
| `agentframe.run.errors` | Counter | `{run}` | As above; counts failed runs by error class |
| `agentframe.run.duration` | Histogram | `s` | As above |
| `agentframe.admission.rejections` | Counter | `{request}` | `gen_ai.agent.name`, `agentframe.rejection.reason` (`busy` or `shutting_down`) |
| `gen_ai.client.token.usage` | Histogram | `{token}` | Model attributes, `gen_ai.token.type` (`input` or `output`); only known counts |
| `gen_ai.client.operation.duration` | Histogram | `s` | Model attributes, `error.type` on failure |

Invocation IDs are never metric attributes. An unknown skill ID sent by a
caller never names a span or labels a metric.

## What is never recorded

Messages, questions, answers, instructions, skill arguments and results, tool
arguments and results, remote URLs, credentials, headers, exception messages
and private reasoning are never recorded. There is no content-capture option.
Agent, skill, tool, remote, model and plan step names and IDs are recorded;
keep secrets out of them.
