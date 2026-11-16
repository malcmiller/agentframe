# Tracing and correlation

Tracing is optional infrastructure, not an agent decorator or subclass
requirement. The OpenTelemetry integration is implemented in 0.5.0
([#248](https://github.com/malcmiller/agentframe/issues/248)) behind the `otel`
extra (`opentelemetry-api` only); the application owns the SDK, sampling,
processors, exporters and collector credentials. Invocation metadata,
accounting and enforcement remain independent of it. The
[tracing guide](../guides/tracing.md) lists every span, attribute and metric.

## Configuration (implemented)

```python
from agentframe import Runtime, TracingConfig

runtime = Runtime(
    tracing=TracingConfig(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
    )
)
```

`tracer_provider` and `meter_provider` are application-created OpenTelemetry
providers; `None` (the default) uses OpenTelemetry's global providers.
AgentFrame never replaces global provider configuration or starts an
exporter. Tracing is disabled unless configured. A missing `otel` extra
raises `DefinitionError` when tracing is requested. The proposed `enabled`
and `capture_content` fields were dropped: passing a `TracingConfig` turns
tracing on, and content is never captured (see [safe metadata](#safe-metadata)).
`AgentApp(tracing=...)` builds its runtime with it; it cannot be combined
with `runtime=`.

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
Local invocations have no synthetic HTTP parent.

Implemented spans (#248): a run is `invoke_agent {agent}` (`SERVER` for an
A2A request), its selected skill `skill {id}`, each logical model call
`chat {model}` (`CLIENT`, GenAI semantic conventions; retries are `retry`
events on it, so a logical call is counted once), each tool call
`execute_tool {name}`, each `Remote.ask` / `call` `invoke_agent {remote}`
(`CLIENT`) and each plan step `plan_step {id}`. Routing model calls are
children of the run span, before the skill span. Authentication and
admission happen before a run exists and have no spans; refused requests
are counted by the `agentframe.admission.rejections` metric. Metrics also
count runs, errors by class, run and model-call durations and known token
usage (`gen_ai.client.token.usage`). Model-free skills
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
downstream trace field mappings remain open. M8+ remote bindings propagate
approved trace/correlation independently of exporters, alongside the decided
deadline/token/depth extension controls. Do not record OBO assertions, exchanged
tokens, dev API keys or raw remote questions. Charge negotiated downstream usage
once; extension-unaware peer usage remains unknown, not zero.

| Record by default when permitted | Do not capture by default |
| --- | --- |
| Invocation ID, agent identity/version, stable skill ID, interface | User text, instructions, generated responses |
| Approved model/deployment identifiers and operation name | Credentials, authorization headers, endpoint query secrets |
| Durations, safe status/error categories, cancellation, exhausted limits | Raw provider exceptions, stack locals, tool arguments/results |
| Available usage with source/completeness and effective token limits | Private reasoning, conversation history, arbitrary caller metadata |

Identifiers and deployment names can also be sensitive; support application
attribute filtering and avoid unbounded/high-cardinality payloads. Metrics should
not use invocation IDs as labels. Provider errors need redaction before recording.

Implemented (#248): spans and metrics record names, IDs, kinds, counts,
durations, finish reasons and error class names (`error.type`), never
exception messages, content, URLs, headers or credentials. Unknown skill IDs
from callers never name spans or label metrics; invocation IDs are span
attributes, never metric attributes. There is no content-capture option.

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
open. For ordinary A2A, task metadata carries the invocation ID and a safe usage snapshot (token counts, estimated flag, model and tool call counts) under `invocation_id` and `usage`
(M3 #36), never `RunResult` internals or provider details.
The negotiated M8+ extension separately reports actual remote usage for budget
reconciliation, not arbitrary local internals. Dev combined logs are tagged by
agent and invocation ID while preserving redaction/content-capture defaults.
A correlation reference does not grant trace-backend access.

### Implemented cross-agent correlation (M8 #91)

Without any tracing backend, every hosted run's log records carry `agent`,
`invocation_id`, `root_invocation_id` and `parent_invocation_id` through
`logging` `extra`; the library sets no log format, and `agentframe run
--log-format json` writes them as [JSON fields](../api/hosting.md#json-log-records)
(#271). `Remote` sends
`agentframe.root_invocation_id` (the request's first run) and
`agentframe.parent_invocation_id` (the calling run) as
[caller context](../api/hosting.md#structured-skill-call-wire-format), only to
peers whose card lists those fields, so an older AgentFrame peer never sees
unknown metadata. A root request uses its own invocation ID as root. Receivers
accept only lowercase UUID strings and use them only for logs: invalid values
are ignored (the run becomes a new root), never echoed, trusted or turned into
errors, and ordinary task metadata stays the invocation ID and usage. The root
is not exposed to skills.

A request's single valid version-00 W3C `traceparent` header, and its
`tracestate` when valid (at most 512 characters and 32 list members), is
forwarded unchanged on every outbound remote request of the run, to any A2A
peer. Invalid values are dropped; a later trace-context version is dropped
because it cannot be validated. Without tracing, AgentFrame never invents
trace IDs or creates spans.

With `TracingConfig` (#248), a hosted run's span continues that validated
context as its parent, and each outbound request instead carries a
`traceparent` (and the span's `tracestate`) naming the remote call's span as
parent, so one trace spans every agent that honors W3C trace context. Baggage
is never read or forwarded. Trace context still never authenticates.
None of these values authenticate, authorize or grant task access.

Plans (M10 [#173](https://github.com/malcmiller/agentframe/issues/173)) log
`Plan created`, `Plan step started` and `Plan step completed` with the same
correlation fields plus the step ID, entry name, status, error class name,
remote task ID, duration and total tokens, never content. No wire field identifies a step:
each peer run sees the orchestrator's run as its parent, and the remote task
ID ties a step to the peer's logs. See
[plan logs and usage](../api/orchestration.md#plan-logs-and-usage).

Authentication failures should be diagnosable without disclosing skill details.
Telemetry export failure must not become an agent success/failure substitute:
report exporter health through the observability infrastructure, keep execution
failures explicit, and never rely on successful export to enforce limits.

See [limits](../guides/limits.md) for independent accounting and
[MCP skill configuration](mcp-skills.md) for shared server composition.
