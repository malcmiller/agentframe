# Execution configuration

[Reference index and status conventions](README.md). Defaults and precedence
identified as decided below are approved design contracts, not implemented
enforcement. Example overrides are not the library defaults.

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

**Behavior / failures:** resolve library -> agent -> skill -> per-run, always
within host ceilings. Skills use `@skill(limits=RunLimits(...))`; exact per-run
options syntax and partial-field composition remain open. Shared instructions combine with
trusted generation-specific instructions without replacing shared constraints.
Reject invalid settings or explicit requests exceeding policy; never silently
clamp them or allow overrides to weaken security/host ceilings.

**Related:** [`ModelSettings`](models.md#modelsettings), [`HostConfig`](hosting.md#hostconfig),
[defaults versus ceilings](../limits.md#defaults-versus-ceilings).

## `RunLimits`

**Purpose / decision:** typed resource bounds for each assembled model input and
the full invocation. Used separately for agent defaults and host ceilings.

**Decided defaults** (`max_output_tokens` stays canonical on `ModelSettings`,
not duplicated on `RunLimits`):

```text
RunLimits(
    *, max_input_tokens: int = ..., max_total_tokens: int = 100000,
    max_model_calls: int = 10, max_tool_calls: int = 20,
    timeout_seconds: ... = 120,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `max_input_tokens` | `int`; omitted-value representation open | Defaults to model-reported context size | Bounds assembled input, including instructions/history/user content/tool schemas/results; actual input plus reserved output must also fit context. Unknown capacity is not unlimited; explicit overflow fails, never truncates. |
| `max_total_tokens` | `int` | `100000`, configurable | Aggregate input/output across a run, retries and continuations, without double-counting overlapping usage. |
| `max_model_calls` | `int` | `10`, configurable | Dispatched inference attempts including retries, not just successful completions. |
| `max_tool_calls` | `int` | `20`, configurable | Bounds managed tool executions/continuations; detailed attempted/denied call counting remains open. |
| `timeout_seconds` | Numeric seconds; accepted numeric type open | `120`, configurable | Wall-clock invocation deadline including model/tool work; retries never reset it. |

```python
from agentframe import RunLimits


limits = RunLimits(
    max_input_tokens=6_000,
    max_total_tokens=12_000,
    max_model_calls=4,
    timeout_seconds=30,
)
```

**Behavior / failures:** validate finite settings; exact numeric ranges, zero
semantics, and partial-field composition remain open. M1 implements a frozen
`RunLimits(timeout_seconds: float = 120.0)` only; the other fields arrive with
model and tool milestones. A `timeout_seconds` that is not greater than zero
raises `DefinitionError`, and exceeding it raises `LimitExceededError`. Model-free skills still
have deadlines but consume no model tokens. Concurrency/request-size controls
are separate host requirements, not invented fields on this type.

Preflight assembled input and actual context capacity; atomically reserve
input/output allowances and reconcile available usage. Unknown usage is not
zero. Strict accounting must reject work without a sufficiently reliable bound;
strict-mode names/capabilities remain open. Model-backed routing must be
budgeted before selection and pass remaining allowance to the skill. Retries,
continuations, and explicit child work cannot reset or double-spend budgets.
First-release typed structured generation and bounded registered-tool loops
use these allowances. Resolve library -> agent -> skill -> per-run; none
may exceed host ceilings. `@skill(limits=RunLimits(...))` is decided; exact
per-run keyword/container and partial-field merge mechanics remain open.
Host admission/bytes/shutdown defaults live on `HostConfig`, not this type.

Invalid/excessive requests, input overflow, exhausted budgets, unavailable
strict accounting, deadline, cancellation, and provider errors are distinct
failures in the [decided hierarchy](runtime.md#agentframeerror); token, model-call,
tool-call and timeout subtype names remain open. Never silently truncate, discard
history, disable validation, or switch models to fit a limit. Accounting works
without tracing and is not an unqualified financial cap or free-cancellation
guarantee.
Future [AG-UI streaming](streaming.md) must preserve these limits during
buffering/backpressure and distinguish disconnect/cancellation. Safe local
execution/usage metadata uses `Runtime.run` / `RunResult`, independent of tracing.
M8+ additionally propagates remaining deadlines and token ceilings with default
maximum 5 remote hops; exact depth configuration carrier is still open, not a
new guessed `RunLimits` field. See [cross-agent budgets](../limits.md#cross-agent-budgets-and-depth-m8).

**Related:** [`ModelSettings.max_output_tokens`](models.md#modelsettings),
[`HostConfig.limits_ceiling`](hosting.md#hostconfig),
[accounting rationale](../limits.md#accounting-and-enforcement).

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
[span boundaries](../tracing.md#span-boundaries),
[safe metadata](../tracing.md#safe-metadata).
