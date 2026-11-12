# Tracing and correlation

These are proposed runtime APIs, not implemented telemetry. Tracing is optional
infrastructure, not an agent decorator or subclass requirement. Use an optional
OpenTelemetry integration; the application owns sampling, processors, exporters,
and collector credentials.
Tracing integration is a separate follow-up after M7; invocation metadata,
accounting and enforcement remain independent of it in the first usable release.

## Proposed configuration

```python
from agentframe import Runtime, TracingConfig

runtime = Runtime(
    tracing=TracingConfig(
        enabled=True,
        tracer_provider=tracer_provider,
        capture_content=False,
    )
)
```

`tracer_provider` is an application-created OpenTelemetry provider. AgentFrame
must not replace global provider configuration or silently start an exporter.
Tracing is disabled unless configured; content capture is off by default.
Exporter setup and lifecycle remain application-owned. A missing optional
integration must produce an explicit configuration error if tracing is requested.

Tracing must not be required for local invocation, token accounting, or budget
enforcement. Every invocation receives a library-generated invocation ID even
when no trace is sampled; do not fabricate a trace ID when none exists.

## Span boundaries

An illustrative remote trace:

```text
server request: A2A or MCP
|-- authentication / authorization
|-- skill selection                       # A2A routing, when needed
|   `-- model request                     # Only for an explicit model router
`-- skill invocation: summarize
    |-- model request
    |-- tool execution                    # When used
    `-- model request
```

Selection precedes invocation, rather than being a child of a skill that has
not yet been selected. An activated structured A2A request or MCP tool call
already names a skill and needs no routing-model span or inferred usage.
Validation, authorization, accounting and invocation correlation still apply.
Local invocations have no synthetic HTTP parent. Model-free skills
still have invocation spans but no model spans.

One logical model operation may have multiple attempts: default two retries
with backoff for transient provider rate-limit/timeout/server failures only,
one retry layer with provider SDK retries disabled. Represent retries
explicitly and report their cost without double-counting a logical parent
and its child attempts. Use relevant OpenTelemetry semantic conventions when
implemented, with pinned versions rather than an invented competing standard.

For background A2A tasks, retain invocation correlation and appropriate span
links/parentage after the initial HTTP request ends. Do not keep an unrelated
request span open indefinitely or assume task completion equals HTTP completion.

## Safe metadata

Next-core-release [registry/directory discovery](discovery.md) can record bounded
source-read, card-fetch, cache and refresh operations with invocation correlation.
Do not fabricate model spans for model-free discovery or double-count downstream
usage. Keep caller/tenant/card variants isolated in caches and telemetry; log safe
status/freshness categories, not credentials, protected cards, secret-bearing
URLs or private query/connection metadata. Tracing must remain optional for
correct refresh, authorization and deadline enforcement. Exact attributes and
downstream propagation contracts remain open.

| Record by default when permitted | Do not capture by default |
| --- | --- |
| Invocation ID, agent identity/version, stable skill ID, interface | User text, instructions, generated responses |
| Approved model/deployment identifiers and operation name | Credentials, authorization headers, endpoint query secrets |
| Durations, safe status/error categories, cancellation, exhausted limits | Raw provider exceptions, stack locals, tool arguments/results |
| Available usage with source/completeness and effective token limits | Private reasoning, conversation history, arbitrary caller metadata |

Identifiers and deployment names can also be sensitive; support application
attribute filtering and avoid unbounded/high-cardinality payloads. Metrics should
not use invocation IDs as labels. Provider errors need redaction before recording.

If `capture_content=True` is later supported, it requires explicit application
consent, bounded payload sizes, field filtering/redaction, and retention/access
policies. It never authorizes logging credentials or private model reasoning.
Do not treat opt-in content capture as a security bypass.

## Propagation and transport boundaries

Future [AG-UI streaming](streaming.md) is a frontend protocol event stream, not
an OTel trace stream. Do not pipe spans, raw provider/tool payloads or private
reasoning to clients. Keep native thread/run/message/tool IDs distinct from
server-generated invocation IDs and optional trace IDs; none is caller identity.
`Runtime.invoke` returns the validated value; `Runtime.run` returns `RunResult`
with value, invocation ID, token usage (complete/estimated; unknown never zero),
model/tool call counts, duration and finish reason even when tracing is off.
Errors carry the invocation ID and a safe message. Exact usage marker fields,
duration unit and failed-run usage access remain open.
First-release complete validated results do not depend on a
connected frontend, and future stream buffering/disconnect cannot disable
accounting or required checks.

- For HTTP A2A and MCP, accept valid W3C `traceparent`/`tracestate` according to
  application trust policy. A public ingress may start a fresh trace or link
  incoming context rather than trusting remote sampling decisions.
- Trace headers, invocation IDs, context IDs, and MCP session IDs are not
  authentication or authorization. Generate the canonical invocation ID locally.
- Filter baggage and forwarded metadata; never accept an arbitrary exporter URL
  or observability configuration from a caller.
- Propagate only approved context to downstream clients. Do not assume every
  model provider accepts tracing headers.
- MCP stdio has no HTTP headers. Use SDK-supported context or local correlation;
  do not invent standard protocol metadata without documenting it.

Export traces through the configured backend. Do not invent a REST response
envelope for standard A2A or MCP. Return correlation only through documented
transport-appropriate metadata or headers, if supported; exact mappings remain
open. For A2A, only the invocation ID goes in response metadata, never internal
`RunResult` counters/provider details; its exact metadata key remains open.
A correlation reference does not grant trace-backend access.

Authentication failures should be diagnosable without disclosing skill details.
Telemetry export failure must not become an agent success/failure substitute:
report exporter health through the observability infrastructure, keep execution
failures explicit, and never rely on successful export to enforce limits.

See [limits](limits.md) for independent accounting and
[MCP skill configuration](mcp-skills.md) for shared server composition.
