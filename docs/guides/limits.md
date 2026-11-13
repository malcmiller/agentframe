# Execution limits and token accounting

This document records approved defaults and design contracts. Timeouts (M1)
token/model-call limits (M3 #36, see
[implemented token accounting](#implemented-token-accounting-m3)) and tool-call
limits (M5 #61, see [implemented tool-call limits](#implemented-tool-call-limits-m5))
are enforced; the rest is not implemented yet. Limits are typed configuration; `@skill(limits=RunLimits(...))`
sets skill-specific overrides. Every entry point uses the same accounting
and execution policy, whether called locally, through A2A, or through MCP.

## Separate the scopes

| Proposed setting | Scope | Meaning |
| --- | --- | --- |
| `ModelSettings.max_output_tokens` | One model request | Default **4096**; canonical per-request output allowance, not duplicated in `RunLimits`. Reasoning-token inclusion is provider-specific. |
| `RunLimits.max_input_tokens` | Each model request | Defaults to model-reported context size; bounds assembled input including instructions, history, user content, tool schemas/results. |
| `RunLimits.max_total_tokens` | One skill invocation | Default **100000** aggregate input/output tokens across requests and continuations, without double-counting subdivisions. |
| `RunLimits.max_model_calls` | One skill invocation | Default **10** dispatched attempts including retries, not just successes. |
| `RunLimits.max_model_retries` | Each model request | Default **2** retries of a transient provider failure; `0` disables retries. Each retry is a counted model call. |
| `RunLimits.max_tool_calls` | One skill invocation | Default **20** tool calls; every model-requested call counts, including unknown-tool and invalid-argument calls that did not run. |
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
32 concurrent runs (excess immediately rejected with HTTP 503 and
`Retry-After: 1`, implemented in M7 #72),
1 MiB request, 4 MiB response (both implemented in M7 #73), and 30-second
shutdown grace; all configurable. An oversized request body gets HTTP 413
before authentication and creates no task; a result or clarification question
over the response limit fails its task with `LimitExceededError` and is never
sent in part.
Shutdown stops accepting, drains, then cancels remaining runs, all within one
grace period measured from the shutdown signal (implemented in M7 #74).
`/health` is liveness; `/ready` means startup checks passed and not shutting
down. Probe bodies are minimal: `{"status": "ok"}`, `{"status": "ready"}` or
`{"status": "not ready"}`.

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
per-run settings use `options=RunOptions(...)`, and every level overrides only
the fields it sets ([precedence](../api/configuration.md#precedence)).
Do not pass limits through skill business arguments.

## Defaults versus ceilings

Precedence is **library defaults → agent → skill → per-run**. All layers
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
TLS or binding. The ceiling values are examples, not library defaults.

Implemented in M7 (#71): each field the ceiling sets caps that limit after the
library → agent → skill → per-run merge; unset fields cap nothing.

- A library default above the ceiling is lowered to it: with the example above,
  the 120-second default timeout becomes 45 seconds. Defaults are not explicit
  requests.
- An agent or skill limit explicitly set above the ceiling fails `AgentApp`
  construction with `DefinitionError` naming the skill and field, before any
  I/O or request.
- Hosted runs, the free-text routing deadline and budget, and startup model
  checks (`Runtime.validate_model`, `ModelRouter`) use the capped limits. A
  `max_input_tokens` ceiling caps the model's context window as the input
  limit, and stands in for a window the model does not report.
- A `max_total_tokens` ceiling also bounds `ModelSettings.max_output_tokens`:
  each call reserves its full output cap, so a call whose cap cannot fit the
  capped budget fails with `TokenLimitExceededError`; it is never silently
  shrunk.
- Local `Runtime` takes no ceiling: ceilings protect a host from remote
  callers, while local `RunOptions` come from trusted code.

For A2A model-backed routing, establish a request-wide budget and deadline
before routing. The selected skill inherits the remaining allowance.
Implemented in M6 (#67): a router's
[`RoutingContext.generate`](../api/hosting.md#routingcontextgenerate) calls use
the library defaults merged with the agent's limits and model settings (the
skill is not known yet), capped by any host ceiling (M7 #71), charged to the run's shared accounting. The selected
skill then runs with its own effective limits against the same accounting and
the same deadline start, so routing tokens and model calls count against its
`max_total_tokens` and `max_model_calls`. Routing,
retries, tool-loop continuations, and any explicitly composed child work must
not reset budgets or be counted twice. Independent remote services need their
own enforcement; local limits do not establish distributed billing guarantees.
Structured A2A selection through the activated extension bypasses routing/model
selection, not request budgets or accounting. The selected skill's inference
and tools still consume their allowances; do not call it a free invocation.

Next-core-release [directory discovery](../design/discovery.md) must also fit remaining
deadlines and host resource bounds: source reads, bounded card HTTP fetches,
refresh/enrichment fan-out and downstream calls do not reset allowance.
Discovery itself is model-free; explicit model-assisted selection consumes
model budget. Remote card metadata cannot impose a hard remote token/billing
cap. M8+ author calls and propagation defaults are decided below; exact extension
schema and reconciliation details remain open.

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
[decided error hierarchy](../api/runtime.md#agentframeerror): `LimitExceededError`
has the `TokenLimitExceededError`, `ModelCallLimitExceededError` and
`TimeoutLimitExceededError` subtypes (M3 #36) and `ToolCallLimitExceededError`
(M5 #61);
AgentFrame `CancelledError` is distinct from `asyncio.CancelledError`.

Cancellation propagates where supported; it cannot guarantee that already
dispatched inference stops or that charges are prevented. Enforcement must work
with tracing disabled or sampled out. See [tracing](../design/tracing.md).

First release returns complete validated results by default; token-limit
truncation raises `IncompleteOutputError`. A skill that prefers a partial
answer catches it and uses `error.partial_text`, the text received before the
limit (text generation only; always `None` for structured output). Whatever the
skill returns is still validated, the truncated call's usage still counts, and
the run finishes `COMPLETED`. Hosted failure messages and logs never include
the partial text (M4 #54).
`Runtime.invoke` returns the value; `Runtime.run` returns `RunResult` with
value, invocation ID, usage, model/tool call counts, duration and finish reason,
even with tracing off. Usage marks completeness/estimation and never turns
unknown into zero. Exact usage marker fields/failed-run retrieval remain open.
Future [AG-UI streaming](../design/streaming.md) must bound buffering,
slow-consumer/backpressure and write/shutdown time within execution deadlines.
AG-UI disconnect cancels the run; its first version has no reconnect/resume.
This does not redefine A2A task-disconnect semantics or retry completed actions.
Protocol usage events do not replace
the accounting ledger or establish remote billing guarantees.

## Implemented token accounting (M3)

M3 #36 implements the steps above for `ctx.generate`, per run:

1. **Resolve.** `max_total_tokens` (default 100000), `max_model_calls`
   (default 10) and `max_input_tokens` merge field by field like every other
   limit. `max_input_tokens` unset at every level means the model's reported
   context window. When the model reports none (for example `FoundryModel`
   without `context_window=`), `AgentApp` fails startup and a local run fails
   before its first model call, both with `DefinitionError`.
2. **Estimate.** The input estimate is the text length of all messages divided
   by **four characters per token**, rounded up. It is best effort, not a
   guaranteed bound: code and many languages use more tokens per character.
3. **Check.** The estimated input must not exceed the input limit, and input
   plus the full `max_output_tokens` must fit the context window when known.
   Otherwise `TokenLimitExceededError`; nothing is truncated.
4. **Reserve.** Input estimate + `max_output_tokens` is reserved against what
   is left of `max_total_tokens`, and the call is counted against
   `max_model_calls`, in one step with no `await`, so concurrent calls in one
   run (for example in an `asyncio.TaskGroup`) cannot both take the same
   tokens. A reservation that does not fit raises `TokenLimitExceededError`;
   the output cap is never lowered to make it fit. Too many calls raise
   `ModelCallLimitExceededError`. Either way the call is not sent.
5. **Settle.** After the call, its reservation is replaced by its actual total.
   Counts the provider did not report for a returned response are estimated
   (the output from its text plus any tool-call names and arguments) and marked `estimated=True`. A failed call
   settles to the usage its error carries (`ModelError.usage`, set by
   adapters); when that is unknown, its reservation stays counted against the
   budget and the run's reported counts become unknown (`None`), never zero.
   A call refused with `UnsupportedCapabilityError` was never sent, so its
   reservation and call count are released.

Actual usage can exceed a reservation when the estimate was low; the next
call then has less budget, so the total is a best-effort bound. `RunResult`
reports the run's `usage` and `model_calls`; hosted tasks report the same
values in a safe [usage snapshot](../api/hosting.md#result-and-failure-mapping).
Each [retry](#retries-and-duplicate-requests) is a new call with its own
reservation and call count.

## Implemented tool-call limits (M5)

M5 #61 runs the tools a model asks for inside `ctx.generate(..., tools=[...])`
under the same run limits:

- **Counting.** `max_tool_calls` (default 20) merges field by field like every
  other limit. Each call the model asks for is counted before it runs,
  including calls naming an unknown tool or with invalid arguments (which run
  nothing). The call over the limit raises `ToolCallLimitExceededError` and
  does not run. `RunResult.tool_calls` and the hosted usage snapshot report
  the count.
- **Continuations.** Sending tool results back is an ordinary model call: it
  is counted against `max_model_calls`, reserved and settled against
  `max_total_tokens`, and its input (including tool calls and results) is
  checked against the input limit and context window.
- **Deadline.** Tools run under the run's deadline. When it expires during a
  tool, the tool is cancelled and the run raises `TimeoutLimitExceededError`;
  cancelling the run cancels the tool. Scoped services are cleaned up either
  way.
- **No tool retries.** A tool call is never retried; a transient model retry
  re-sends the same request, which already holds earlier tool results.

## Cross-agent budgets and depth (M8+)

**Decided:** send the remaining deadline automatically. An AgentFrame receiver
uses the minimum of that deadline and its own timeout; the caller independently
enforces its local wait even with non-AgentFrame peers. Do not extend an ancestor
deadline, reset it on task polling/retries or promise remote cancellation stops billing.

`await research.call(..., max_total_tokens=20_000)` supplies a per-call ceiling;
the default is the caller's remaining token allowance. AgentFrame receivers can
only lower it and report actual usage back through the same AgentFrame A2A
extension. Charge returned usage to the caller once, without double-counting
local inference or nested-hop aggregates. Concurrent `asyncio.TaskGroup` calls
share atomic reservations; each cannot spend the whole remaining allowance.
Exact reservation allocation, descendant reconciliation and usage trust/markers
remain open. Strict accounting cannot pretend an unbounded peer is bounded.

Non-AgentFrame usage is **unknown, never zero**. Extension-unaware peers do not
promise token enforcement/reporting, though caller-side waiting remains bounded.
This negotiated accounting extension is a narrow exception to ordinary A2A
invocation-ID-only metadata; it never exposes internal `RunResult` or raw provider data.

Track remote call depth with a default maximum of **5 hops**; exceeding it fails
instead of continuing a recursion loop. The configuration carrier, root/hop
encoding and precise limit subtype name remain open. Deadline, token allowance/
reported usage and depth use the same structured-request extension, whose
URI/schema remain open; these are not invented standard A2A top-level fields.

## Retries and duplicate requests

Only transient model provider failures (rate limit, provider timeout, server
errors) retry, with backoff; the default is **2 retries** after the initial
attempt. Implemented in M3 #37:

- `RunLimits.max_model_retries` sets the retries per `ctx.generate` call
  (default `2`; `0` disables them), merged field by field like every other
  limit ([precedence](../api/configuration.md#precedence)).
- The runtime's generate path is the single retry layer. Adapters only
  classify failures as `ProviderError(transient=...)`; provider SDK retries
  are disabled.
- Before each retry the runtime waits the provider's `retry_after` hint when
  given. Otherwise it waits a random time between 0 and an exponential bound
  (0.5, 1, 2, 4, then at most 8 seconds) so failed runs do not retry in step.
- A retry whose wait would reach the run's deadline is not made; the provider
  error is raised at once. Waiting counts against `timeout_seconds`, which a
  retry never resets.
- Every attempt is a new model call: it reserves tokens, counts toward
  `max_model_calls`, and its usage is reported. A retry that no longer fits
  those limits fails with the limit error instead.
- Non-transient provider errors, unsupported capabilities, refusals and
  incomplete output are never retried.

Tools declare `@tool(side_effects=True)` or `@tool(idempotent=True)`; both
default to `False` and may both be `True` (M5 #58). Implemented in M5 #63:

- **No tool is ever retried automatically**, whatever its flags. A tool that
  raises fails the run at once; it is not run again and the model is not
  called again. The flags are declarations for readers and policies, not
  retry switches:

  | `side_effects` | `idempotent` | Automatic tool retry |
  | --- | --- | --- |
  | `False` | `False` | Never |
  | `True` | `False` | Never |
  | `False` | `True` | Never |
  | `True` | `True` | Never |

- **A model retry never replays a tool.** A retry re-sends the same model
  request, whose history already holds the results of the tools that ran.
  If the continuation after `cancel_order` fails transiently, only that model
  call is retried; the order is cancelled once. A retry that would exceed
  `max_model_calls` fails the run with `ModelCallLimitExceededError`, still
  without re-running the tool.
- **Repeated calls are not blocked.** When the model asks for the same
  side-effecting call again in a later turn, that is a new model decision: it
  is authorized again, counts toward `max_tool_calls` and runs. AgentFrame does
  not compare arguments across calls.

A tool that wants retries implements them itself, inside the function. For
exactly-once effects, make the backend operation idempotent: pass an
idempotency key the backend deduplicates on, derived from stable inputs such
as `ctx.invocation_id` plus the business arguments, so a repeated call within
one run is a no-op. A key derived only from the invocation ID still lets a new
run (a new user request) act again.

```python
from agentframe import Inject, RunContext, tool


class Payments:  # Application-defined; deduplicates on idempotency_key.
    async def refund(self, order_id: str, *, idempotency_key: str) -> None: ...


@tool(description="Refund an order.", side_effects=True, idempotent=True)
async def refund_order(
    order_id: str, payments: Inject[Payments], ctx: RunContext
) -> str:
    await payments.refund(
        order_id, idempotency_key=f"{ctx.invocation_id}:refund:{order_id}"
    )
    return "refunded"
```

Declare `idempotent=True` only when the tool really is (here, because the
backend deduplicates); the declaration itself guarantees nothing.

For A2A, **same verified caller + same `messageId` within 10 minutes** returns
the original task instead of re-running (implemented, M7 #75). Configure it
with [`AgentApp(a2a=A2AConfig(dedup_window_seconds=..., dedup_store=...))`](../api/hosting.md#a2aconfig):

- The original task comes back whatever its state; failed and cancelled tasks
  are not re-run. A repeat takes no run slot, so it gets its task even when the
  host is full. Repeated answers to input-required tasks are deduplicated too.
- Reusing a `messageId` for a different message is refused with JSON-RPC
  `InvalidParams`. A message refused before it ran (for example busy) runs
  when retried.
- Anonymous callers are not deduplicated: they share one namespace, so a
  guessable `messageId` would expose another anonymous caller's task.
- Storage defaults to memory, so claims are **lost on restart**; a
  [`MessageDedupStore`](../api/hosting.md#messagededupstore) with an atomic
  `put_if_absent` can be shared by several instances.
- This is **not exactly-once**: process loss, expiry, instances that cannot
  see each other's tasks and distributed side effects still need application
  handling, such as idempotent backends.

Deduplication does **not bypass task authorization**: the original task is
looked up as the caller, and another caller's same `messageId` gets its own
task, never the first caller's results. It is not a general retry/resume
workflow.
