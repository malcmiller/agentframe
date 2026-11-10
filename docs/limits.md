# Execution limits and token accounting

This document describes proposed APIs, not implemented enforcement. Limits are
typed configuration, not decorators. Every entry point uses the same accounting
and execution policy, whether called locally, through A2A, or through MCP.

## Separate the scopes

| Proposed setting | Scope | Meaning |
| --- | --- | --- |
| `ModelSettings.max_output_tokens` | One model request | Generated-token allowance translated to a supported provider parameter. Reasoning-token inclusion depends on the model/API and must be documented. |
| `RunLimits.max_input_tokens` | Each model request | Bounds the assembled input, including instructions, history, user content, tool schemas, and tool results. |
| `RunLimits.max_total_tokens` | One skill invocation | Aggregate input and output budget across model requests and tool-loop continuations, without counting overlapping usage categories twice. |
| `RunLimits.max_model_calls` | One skill invocation | Maximum dispatched inference attempts, including retries, not merely successful completions. |
| `RunLimits.timeout_seconds` | One skill invocation | Wall-clock deadline that includes model and tool work; retries do not reset it. |

Model context capacity belongs to `ModelCapabilities`, not a caller-controlled
promise. Input plus the reserved output allowance must fit the actual supported
context, with provider-specific reasoning and output rules accounted for.
Unknown capacity or unsupported settings must not be silently treated as unlimited.

Model-free skills still have deadlines and resource constraints but do not
consume model tokens. Concurrency and request-size limits are separate host
requirements; a token budget is not a substitute for either.

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

Defaults must be finite and validated, but concrete release defaults remain an
implementation decision. Skills inherit agent configuration; final syntax for
trusted per-skill or per-run overrides remains open. Do not invent a limit
decorator or add provider-specific parameters to the skill's business arguments.

## Defaults versus ceilings

Library defaults are refined by class/instance configuration and permitted run
options. A host-enforced ceiling is an independent constraint, not another
overridable default. Resolve effective allowance before executing work.

Reject invalid settings and requests that exceed policy; do not silently clamp
an explicit request to an unrelated value. Authorized callers may tighten
allowances but cannot disable limits, extend a parent deadline, or exceed host
ceilings. Keep execution options separate from skill arguments and authenticate
the caller before accepting any remote override.

```python
from agentframe import HostConfig, RunLimits

host = HostConfig(
    public_url="https://writer.example.com",
    authentication=authentication,
    limits_ceiling=RunLimits(
        max_input_tokens=8_000,
        max_total_tokens=16_000,
        max_model_calls=6,
        timeout_seconds=45,
    ),
)
```

`authentication` is application-supplied; the advertised URL does not configure
TLS or binding. The ceiling fields are illustrative proposals. The host must
validate configured agent allowances against its ceilings before accepting work.

For A2A model-backed routing, establish a request-wide budget and deadline
before routing. The selected skill inherits the remaining allowance. Routing,
retries, tool-loop continuations, and any explicitly composed child work must
not reset budgets or be counted twice. Independent remote services need their
own enforcement; local limits do not establish distributed billing guarantees.

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
cancellation, and provider errors. Exact public exception names remain open.

Cancellation propagates where supported; it cannot guarantee that already
dispatched inference stops or that charges are prevented. Enforcement must work
with tracing disabled or sampled out. See [tracing](tracing.md).
