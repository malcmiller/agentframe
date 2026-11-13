# Execution configuration

[Reference index and status conventions](README.md). Defaults and precedence
identified as decided below are approved design contracts; field-by-field
[precedence](#precedence), `RunOptions`, `RunLimits` and host ceilings
([`HostConfig.limits_ceiling`](hosting.md#hostconfig), M7 #71) are implemented.
Example overrides are not the library defaults.

## `AgentConfig`

**Purpose / decision:** validated agent defaults for instructions and execution,
separate from non-overridable host policy.

**Proposed fields already used in examples:**

```text
AgentConfig(*, model_settings: ModelSettings = ..., limits: RunLimits = ...)
```

| Parameter/concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `model_settings` | `ModelSettings` | Optional refinement; output default 4096 | Shared per-request generation settings; must fit capabilities, host ceilings and remaining allowance. Other settings remain open. |
| `limits` | `RunLimits` | Optional refinement of the decided defaults below | Agent invocation/input budgets. Host ceilings are independent constraints, not another default layer. |
| Instructions | Static text or trusted typed callable; field/callable signature open | Optional authoring configuration | Existing examples use class `instructions`; typed configuration is intended without a finalized constructor keyword. |

```python
from agentframe import AgentConfig, ModelSettings, RunLimits


config = AgentConfig(
    model_settings=ModelSettings(max_output_tokens=800),
    limits=RunLimits(
        max_input_tokens=6_000,
        max_total_tokens=12_000,
        max_model_calls=4,
        timeout_seconds=30,
    ),
)
```

Supply as `config=` when constructing an author-defined [`Agent`](agents.md#agent)
subclass.

**Behavior / failures:** resolve library → agent → skill → per-run, always
within host ceilings, merged field by field (see [precedence](#precedence)).
Skills use `@skill(limits=..., model_settings=...)`; local runs use
[`RunOptions`](#runoptions). Shared instructions combine with
trusted generation-specific instructions without replacing shared constraints.
Reject invalid settings or explicit requests exceeding policy; never silently
clamp them or allow overrides to weaken security/host ceilings.

**Related:** [`ModelSettings`](models.md#modelsettings), [`HostConfig`](hosting.md#hostconfig),
[defaults versus ceilings](../guides/limits.md#defaults-versus-ceilings).

## `RunLimits`

**Purpose / decision:** typed resource bounds for each assembled model input and
the full invocation. Used separately for agent defaults and host ceilings.

**Decided defaults** (`max_output_tokens` stays canonical on `ModelSettings`,
not duplicated on `RunLimits`):

```text
RunLimits(
    *, max_input_tokens: int = ..., max_total_tokens: int = 100000,
    max_model_calls: int = 10, max_model_retries: int = 2,
    max_tool_calls: int = 20, max_remote_hops: int = 5,
    timeout_seconds: ... = 120,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `max_input_tokens` | `int \| None` (implemented M3 #36) | Unset (`None`) means the model-reported context window; unknown window plus unset limit is a `DefinitionError` | Bounds assembled input, including instructions/history/user content/tool schemas/results; actual input plus reserved output must also fit context. Unknown capacity is not unlimited; explicit overflow fails, never truncates. |
| `max_total_tokens` | `int \| None` (implemented M3 #36) | `100000`, configurable | Aggregate input/output across a run, retries and continuations, without double-counting overlapping usage. |
| `max_model_calls` | `int \| None` (implemented M3 #36) | `10`, configurable | Dispatched inference attempts including retries, not just successful completions. |
| `max_model_retries` | `int \| None` (implemented M3 #37) | `2`, configurable; `0` disables retries; negative is a `DefinitionError` | Retries of one model call after a transient `ProviderError`. Each retry counts toward `max_model_calls` and the token budget; see [retries](../guides/limits.md#retries-and-duplicate-requests). |
| `max_tool_calls` | `int \| None` (implemented M5 #61) | `20`, configurable | Tool calls a run may make. Every model-requested call counts, including unknown-tool and invalid-argument calls that did not run; the call over the limit raises `ToolCallLimitExceededError` before it runs. |
| `max_remote_hops` | `int \| None` (implemented M8 #89) | `5`, configurable | How deep a chain of calls between agents may go. A run at this depth cannot call another agent (`Remote` raises base `LimitExceededError` before sending), and an A2A request arriving deeper is `REJECTED` before routing or the skill, using the agent's limits capped by the host ceiling. Depth is cooperative: it stops accidental loops, not abuse. |
| `timeout_seconds` | Numeric seconds; accepted numeric type open | `120`, configurable | Wall-clock invocation deadline including model/tool work; retries never reset it. A hosted run's timeout is also capped by the caller's remaining time when it sends `agentframe.deadline_ms` (M8 #89). |

```python
from agentframe import RunLimits


limits = RunLimits(
    max_input_tokens=6_000,
    max_total_tokens=12_000,
    max_model_calls=4,
    timeout_seconds=30,
)
```

**Behavior / failures:** implemented so far: a frozen, keyword-only
`RunLimits(*, timeout_seconds: float | None = None, max_total_tokens: int | None
= None, max_model_calls: int | None = None, max_input_tokens: int | None =
None, max_model_retries: int | None = None, max_tool_calls: int | None =
None, max_remote_hops: int | None = None)`. Every field defaults to
`None`, meaning "unset", and is inherited field by field (see
[precedence](#precedence)); the library defaults are `DEFAULT_LIMITS =
RunLimits(timeout_seconds=120.0, max_total_tokens=100_000, max_model_calls=10,
max_model_retries=2, max_tool_calls=20, max_remote_hops=5)`,
with `max_input_tokens` unset so it falls back to the model's context window. A
field that is set but not greater than zero raises `DefinitionError`. Exceeding
a limit raises a `LimitExceededError` subclass naming it:
`TimeoutLimitExceededError`, `TokenLimitExceededError`,
`ModelCallLimitExceededError` or `ToolCallLimitExceededError`. Model-free skills still have deadlines but
consume no model tokens and need no context window. Concurrency/request-size
controls are separate host requirements, not invented fields on this type.

**Implemented accounting (M3 #36):** before each model call the runtime reads
`Model.capabilities()`, estimates the assembled input and checks it against the
input limit and, with the full `max_output_tokens`, against the context window.
It then atomically reserves input estimate + `max_output_tokens` against what is
left of `max_total_tokens` and counts the call against `max_model_calls`;
concurrent calls in one run cannot both take the same tokens. If the
reservation does not fit, the call is not sent and `TokenLimitExceededError` is
raised; the output cap is never lowered to make it fit. After the call the
reservation is replaced by actual usage; a failed call whose usage is unknown
keeps its reservation. When the model reports no context window and no level
sets `max_input_tokens`, `AgentApp` fails startup (`Runtime.validate_model`) and
a local run fails with `DefinitionError` before its first model call. See
[limits](../guides/limits.md#implemented-token-accounting-m3) for the estimate.

Unknown usage is not zero. Strict accounting must reject work without a sufficiently reliable bound;
strict-mode names/capabilities remain open. Model-backed routing must be
budgeted before selection and pass remaining allowance to the skill. Retries,
continuations, and explicit child work cannot reset or double-spend budgets.
First-release typed structured generation and bounded registered-tool loops
use these allowances. Resolve library → agent → skill → per-run; none
may exceed host ceilings. `@skill(limits=RunLimits(...))` and per-run
[`RunOptions`](#runoptions) are implemented (M3 #42) and merged field by field.
Host admission/bytes/shutdown defaults live on `HostConfig`, not this type.

Invalid/excessive requests, input overflow, exhausted budgets, unavailable
strict accounting, deadline, cancellation, and provider errors are distinct
failures in the [decided hierarchy](runtime.md#agentframeerror); the token,
model-call, timeout and tool-call (`ToolCallLimitExceededError`, M5 #61)
subtypes are implemented. Never silently truncate, discard
history, disable validation, or switch models to fit a limit. Accounting works
without tracing and is not an unqualified financial cap or free-cancellation
guarantee.
Future [AG-UI streaming](streaming.md) must preserve these limits during
buffering/backpressure and distinguish disconnect/cancellation. Safe local
execution/usage metadata uses `Runtime.run` / `RunResult`, independent of tracing.
M8+ propagates remaining deadlines and depth (implemented #89, with
`max_remote_hops` above) and later token ceilings (#90). See
[cross-agent budgets](../guides/limits.md#cross-agent-budgets-and-depth-m8).

**Related:** [`ModelSettings.max_output_tokens`](models.md#modelsettings),
[`HostConfig.limits_ceiling`](hosting.md#hostconfig),
[accounting rationale](../guides/limits.md#accounting-and-enforcement).

## Precedence

**Decision (2026-11-12, implemented M3 #42):** limits and model settings combine
**field by field** in this order, lowest to highest:

1. Library defaults: `DEFAULT_LIMITS` (`timeout_seconds=120`) and
   `DEFAULT_MODEL_SETTINGS` (`max_output_tokens=4096`).
2. Agent: class attributes `limits = RunLimits(...)` and
   `model_settings = ModelSettings(...)`.
3. Skill: `@skill(limits=RunLimits(...), model_settings=ModelSettings(...))`.
4. Per run: `runtime.run(..., options=RunOptions(...))` or the same `options=`
   on `invoke`.

Only fields a level explicitly sets override; a field left `None` (unset) falls
through to the next lower level. Overriding one value, such as a skill's
`timeout_seconds`, therefore never resets the agent's other limits or settings.
The merged values are the run's effective settings: they set the run deadline,
are stored on the run's generation state and are sent, fully resolved, in
every [`ModelRequest`](models.md#modelrequest).

```python
from agentframe import Agent, ModelSettings, RunContext, RunLimits, agent, skill


@agent(version="1.0.0")
class Editor(Agent):
    """Edits text."""

    limits = RunLimits(timeout_seconds=30)
    model_settings = ModelSettings(max_output_tokens=512)

    @skill("quick", limits=RunLimits(timeout_seconds=5), tags=["text"])
    async def quick(self, text: str, ctx: RunContext) -> str:
        """Edit text quickly."""
        # Effective: timeout_seconds=5 (skill), max_output_tokens=512 (agent).
        return await ctx.generate(text)
```

Hosted A2A requests use library, agent and skill settings only; callers cannot
send per-run options. On a host with
[`HostConfig(limits_ceiling=...)`](hosting.md#hostconfig) (M7 #71), the ceiling
then caps the merged limits as an independent constraint, not another default
layer: each field it sets caps that limit, and unset fields cap nothing. A
library default above the ceiling is lowered to it, while an agent or skill
limit explicitly set above it fails `AgentApp` construction with
`DefinitionError` naming the skill and field. Free-text routing and startup
model checks use the capped agent-level limits. A `max_total_tokens` ceiling
also bounds `ModelSettings.max_output_tokens`: a call whose output cap does not
fit the capped budget fails with `TokenLimitExceededError`, never silently
shrunk. Local `Runtime` runs take no ceiling; their options come from trusted
code.

**Related:** [`RunOptions`](#runoptions), [`RunLimits`](#runlimits),
[`ModelSettings`](models.md#modelsettings),
[defaults versus ceilings](../guides/limits.md#defaults-versus-ceilings).

## `RunOptions`

**Purpose / decision:** per-run limits and model settings for one local run,
kept apart from the skill's business arguments.

**Signature (implemented, M3 #42):** frozen, keyword-only dataclass.

```text
RunOptions(*, limits: RunLimits | None = None, model_settings: ModelSettings | None = None)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `limits` | `RunLimits \| None` | Optional; `None` sets nothing | Highest-precedence limits. Only fields it sets override the skill's, agent's and library's values. |
| `model_settings` | `ModelSettings \| None` | Optional; `None` sets nothing | Highest-precedence model settings, merged field by field like `limits`. |

```python
from agentframe import Agent, ModelSettings, RunOptions, Runtime


async def short_summary(runtime: Runtime, writer: Agent) -> str:
    options = RunOptions(model_settings=ModelSettings(max_output_tokens=200))
    return await runtime.invoke(
        writer, skill="summarize", arguments={"text": "..."}, options=options
    )
```

`writer` is an application-supplied agent with a `summarize` skill.

**Behavior / failures:** options apply to one run only and never change the
agent, skill or other runs. Invalid values fail when the `RunLimits` or
`ModelSettings` is constructed (`DefinitionError`). Hosted requests never
accept per-run options from callers; authenticated remote overrides remain
later work, and host ceilings cap hosted runs ([precedence](#precedence)).

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[`Runtime.run`](runtime.md#runtimerun), [precedence](#precedence).

## `TracingConfig`

**Purpose / decision:** optional OpenTelemetry integration configured on
`Runtime`, independent of accounting/enforcement.

**Proposed constructor shape:**

```text
TracingConfig(
    *, enabled: bool = ..., tracer_provider: ... = ...,
    capture_content: bool = False,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `enabled` | `bool` | Tracing disabled unless configured; field default open | Explicitly opt into runtime tracing; no implicit exporter setup. |
| `tracer_provider` | Application-owned OpenTelemetry provider; exact accepted interface open | Supplied in enabled example; omission behavior open | Application controls provider, processors, exporters, credentials, sampling, and lifecycle. Do not replace global configuration. |
| `capture_content` | `bool` | `False` by default | Content capture, if implemented, needs explicit consent, bounded sizes, redaction/filtering, retention/access policies; never captures credentials/private reasoning. |

```python
from opentelemetry.trace import TracerProvider
from agentframe import Runtime, TracingConfig


def traced_runtime(tracer_provider: TracerProvider) -> Runtime:
    return Runtime(
        tracing=TracingConfig(
            enabled=True,
            tracer_provider=tracer_provider,
            capture_content=False,
        )
    )
```

The provider is an application-created dependency from optional OpenTelemetry
packages; this example neither creates an exporter nor chooses sampling.

**Behavior / failures:** requested tracing with missing optional integration
fails configuration explicitly. Operational metadata is redacted; incoming
trace/session/context IDs are not identity or authorization. Every invocation
has a library-generated invocation ID even when tracing is disabled/sampled
out; do not fabricate trace IDs. Export failures are reported through
observability infrastructure, not disguised as agent results or used to
disable enforcement. Exact span attributes, correlation transport mappings,
and integration versions remain open.

**Related:** [`Runtime`](runtime.md#runtime),
[span boundaries](../design/tracing.md#span-boundaries),
[safe metadata](../design/tracing.md#safe-metadata).
