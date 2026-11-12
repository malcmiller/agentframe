# Execution limits and token accounting

This document records approved defaults and design contracts, not implemented
enforcement. Limits are typed configuration; `@skill(limits=RunLimits(...))`
sets skill-specific overrides. Every entry point uses the same accounting
and execution policy, whether called locally, through A2A, or through MCP.

## Separate the scopes

| Proposed setting | Scope | Meaning |
| --- | --- | --- |
| `ModelSettings.max_output_tokens` | One model request | Default **4096**; canonical per-request output allowance, not duplicated in `RunLimits`. Reasoning-token inclusion is provider-specific. |
| `RunLimits.max_input_tokens` | Each model request | Defaults to model-reported context size; bounds assembled input including instructions, history, user content, tool schemas/results. |
| `RunLimits.max_total_tokens` | One skill invocation | Default **100000** aggregate input/output tokens across requests and continuations, without double-counting subdivisions. |
| `RunLimits.max_model_calls` | One skill invocation | Default **10** dispatched attempts including retries, not just successes. |
| `RunLimits.max_tool_calls` | One skill invocation | Default **20** managed tool calls; detailed attempted/denied call counting remains open. |
| `RunLimits.timeout_seconds` | One skill invocation | Default **120** seconds including model/tool work and backoff; retries never reset it. |

Model context capacity belongs to `ModelCapabilities`, not a caller-controlled
promise. Input plus the reserved output allowance must fit the actual supported
context, with provider-specific reasoning and output rules accounted for.
Unknown capacity or unsupported settings must not be silently treated as unlimited.
The context-derived input default does not reserve the entire context for input:
input plus output reservation must still fit capacity. Explicit tighter input
limits and host ceilings apply; a model-free run needs no context-size query.

Model-free skills still have deadlines and resource constraints but do not
consume model tokens. Concurrency and request-size limits are separate host
requirements; a token budget is not a substitute for either.

First-release bounded registered-tool execution and generation continuations
share these allowances; typed structured output also must fit the actual
deployment's capability/context and validate completely. Explicit finite
tool-call controls use the defaults above. `HostConfig` separately defaults to
32 concurrent runs (excess immediately rejected with a standard busy error),
1 MiB request, 4 MiB response, and 30-second shutdown grace; all configurable.
Shutdown stops accepting, drains, then cancels remaining runs. `/health` is
liveness; `/ready` means startup checks passed and not shutting down.
Exact byte measurement, busy-error binding and probe response schemas remain open.

## Proposed configuration

The `WritingAssistant` definition is in [agent examples](agent-examples.md).
`model` is an application-supplied dependency. Numbers are illustrative, not
universal defaults.

```python
from agentframe import AgentConfig, ModelSettings, RunLimits

writer = WritingAssistant(
    model=model,
    config=AgentConfig(
        model_settings=ModelSettings(max_output_tokens=800),
        limits=RunLimits(
            max_input_tokens=6_000,
            max_total_tokens=12_000,
            max_model_calls=4,
            timeout_seconds=30,
        ),
    ),
)
```

The decided defaults above are configurable. Skills can use
`@skill(..., limits=RunLimits(timeout_seconds=30, max_tool_calls=4))`;
exact per-run options syntax and partial-field composition remain open.
Do not pass limits through skill business arguments.

## Defaults versus ceilings

Precedence is **library defaults -> agent -> skill -> per-run**. All layers
stay within host ceilings. A host-enforced ceiling is an independent constraint, not another
overridable default. Resolve effective allowance before executing work.

Reject invalid settings and requests that exceed policy; do not silently clamp
an explicit request to an unrelated value. Authorized callers may tighten
allowances but cannot disable limits, extend a parent deadline, or exceed host
ceilings. Keep execution options separate from skill arguments and authenticate
the caller before accepting any remote override.

```python
from agentframe import EntraIDAuth, HostConfig, RunLimits

host = HostConfig(
    public_url="https://writer.example.com",
    auth=EntraIDAuth(tenant_id=tenant_id, audience=audience),
    limits_ceiling=RunLimits(
        max_input_tokens=8_000,
        max_total_tokens=16_000,
        max_model_calls=6,
        timeout_seconds=45,
    ),
)
```

`tenant_id` and `audience` are trusted application configuration; the URL does not configure
TLS or binding. The ceiling fields are illustrative proposals. The host must
validate configured agent allowances against its ceilings before accepting work.

For A2A model-backed routing, establish a request-wide budget and deadline
before routing. The selected skill inherits the remaining allowance. Routing,
retries, tool-loop continuations, and any explicitly composed child work must
not reset budgets or be counted twice. Independent remote services need their
own enforcement; local limits do not establish distributed billing guarantees.
Structured A2A selection through the activated extension bypasses routing/model
selection, not request budgets or accounting. The selected skill's inference
and tools still consume their allowances; do not call it a free invocation.

Next-core-release [directory discovery](discovery.md) must also fit remaining
deadlines and host resource bounds: source reads, bounded card HTTP fetches,
refresh/enrichment fan-out and downstream calls do not reset allowance.
Discovery itself is model-free; explicit model-assisted selection consumes
model budget. Remote card metadata cannot impose a hard remote token/billing
cap. Exact cross-service allowance/deadline APIs remain open.

## Accounting and enforcement

1. Validate input, caller policy, effective settings, deadline, and remaining
   inference-attempt allowance before dispatch.
2. Count the complete assembled model input using a supported tokenizer/counting
   API where available. Mark estimates as estimates.
3. Check context capacity and reserve input plus the permitted output allowance
   against the remaining budget. Concurrent requests share an atomic reservation
   ledger; they cannot each spend the same remaining allowance.
4. Dispatch with the supported output limit, remaining deadline, and a single
   coordinated retry policy.
5. Reconcile reservations with provider usage when available. Stop scheduling
   more work when the budget is exhausted or cannot be safely established.

Total-token accounting is not automatically a hard financial cap. Providers
may omit final usage after failures, cancellation, or interrupted streams.
Unknown usage remains unknown; do not release an unresolved reservation as if
the call were free. A strict enforcement mode must reject calls when it cannot
establish a sufficiently reliable bound. Exact mode names and capability
requirements remain to be designed.

Reported actual usage, local estimates, and unresolved reservations are distinct
accounting states. Cached/reasoning token breakdowns are subdivisions when the
provider defines them that way, not automatically additive token totals.
Pricing and currency budgets are outside this initial proposal.

Never silently truncate user input, discard history, switch models, or disable
structured validation to fit a limit. Failures must distinguish input too large,
budget exhaustion, unavailable strict accounting, deadline expiration,
cancellation and provider errors. Use the
[decided error hierarchy](api/runtime.md#agentframeerror): `LimitExceededError`
has token/model-call/tool-call/timeout subtypes whose names remain open;
AgentFrame `CancelledError` is distinct from `asyncio.CancelledError`.

Cancellation propagates where supported; it cannot guarantee that already
dispatched inference stops or that charges are prevented. Enforcement must work
with tracing disabled or sampled out. See [tracing](tracing.md).

First release returns complete validated results by default; token-limit
truncation raises `IncompleteOutputError` unless a skill explicitly opts into
partial results (syntax open, validation still mandatory).
`Runtime.invoke` returns the value; `Runtime.run` returns `RunResult` with
value, invocation ID, usage, model/tool call counts, duration and finish reason,
even with tracing off. Usage marks completeness/estimation and never turns
unknown into zero. Exact usage marker fields/failed-run retrieval remain open.
Future [AG-UI streaming](streaming.md) must bound buffering,
slow-consumer/backpressure and write/shutdown time within execution deadlines.
Disconnect is not automatically cancellation; replay/reconnect cannot silently
retry completed actions or reset budgets. Protocol usage events do not replace
the accounting ledger or establish remote billing guarantees.

## Retries and duplicate requests

Only transient model provider failures (rate limit, provider timeout, server
errors) retry, with backoff; the default is **2 retries** after the initial
attempt, configurable. Use a single runtime retry layer and disable provider
SDK retries. Each dispatched attempt consumes the same model-call/token budget
and remaining deadline; no retry after exhaustion. Unsupported capabilities,
refusal, validation or policy denials are not transient provider failures.
Exact configuration keyword, backoff schedule and transient-code mapping remain open.

Tools declare `@tool(side_effects=True)` or `@tool(idempotent=True)`.
Side-effecting tools are **never automatically retried**; an idempotence
declaration is not permission to replay or an exactly-once guarantee.
Additional tool retry rules and flag-combination semantics remain open.

For A2A, **same verified caller + same `messageId` within 10 minutes** returns
the original task instead of re-running. The window is configurable; storage
defaults to memory, with a pluggable store for multi-instance hosts. This is
explicitly not exactly-once: process loss, expiry and distributed side effects
still need application handling. Store/window API, atomic claims, changed
payloads under the same ID and direct-message-only interactions remain open.
Deduplication does not waive current task-access authorization or publish
another caller's results. It is not a general retry/resume workflow.
