# Local runtime

[Reference index and status conventions](README.md). Decided calls are design
contracts, not implemented APIs. Managed local invocation is not an A2A wire operation.

## `Runtime`

**Purpose / decision:** own skill registration and the common validated,
authorized, bounded execution pipeline without requiring model inference.

**Proposed constructor shape:**

```text
Runtime(
    *, authorization: AuthorizationPolicy = ...,
    policy: ExecutionPolicy = ..., tracing: TracingConfig = ..., services: Services = ...,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `authorization` | `AuthorizationPolicy` implementation | Needed for protected operations; constructor omission allowed in unprotected local examples | Enforces mandatory skill permissions plus application resource rules. Missing required integration fails closed. |
| `policy` | `ExecutionPolicy` | Optional supplementary checks; exact default open | Input/model/action/output checks cannot waive authorization. Configured required checks cannot be silently skipped. |
| `tracing` | `TracingConfig` | Optional; tracing disabled unless configured | Application owns provider/exporters/sampling; accounting and invocation IDs do not depend on export. |
| `services` | `Services` | Explicit when skills need registered dependencies | Resolve `Inject[T]` parameters; missing registration is a startup `DefinitionError`. Same service container contract as `AgentApp`. |

```python
from agentframe import Runtime


runtime = Runtime()
```

This unprotected local construction requires no identity provider, model, or
telemetry integration. It does not declare any remote access policy.

**Behavior / failures:** no inference on construction. Validate definitions and
configuration; create invocation-scoped context, enforce policy/limits, validate
outputs, and surface the [decided error hierarchy](#agentframeerror).
Definition and missing-service validation happens at startup/preflight, before
executing skills; exact explicit local startup/lifecycle methods remain open.
Do not infer extra public registration/close methods from this description.
First release owns bounded registered-tool calls/results and generation
continuations as well as typed structured output; it returns complete validated
results. It has no built-in human approval pause/resume or automatic multi-agent
orchestration. Future [streaming/AG-UI](streaming.md) is a separate surface.

**Related:** [`Runtime.invoke`](#runtimeinvoke), [`AgentApp`](hosting.md#agentapp),
[runtime design](../base-classes.md#agent-authoring-and-execution).

## `Runtime.invoke`

**Purpose / decision:** explicitly select a registered local skill by ID and
return its validated value through the managed execution pipeline.

**Decided value-return call; exact generic/container typing remains illustrative:**

```python
async def invoke(
    self, agent: Agent, *, skill: str,
    arguments: Mapping[str, object], principal: Principal = ...,
) -> object: ...
```

`Mapping` is from `collections.abc`. The return is the selected skill's declared
value (for example `str` or `int`), not a result envelope;
`object` above does not settle generic typing or concrete accepted containers.
The value-return path is decided. Use [`run`](#runtimerun) for a `RunResult`
containing execution metadata; both paths use the same pipeline.

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required | Valid definition with registered declared/inherited skills. |
| `skill` | `str` | Required | Stable registered skill ID, not arbitrary method lookup or implicit model routing. |
| `arguments` | String-keyed mapping of business values | Explicit mapping in current examples; empty/default syntax open | Validated against selected skill. No `self`, `ctx`, `Inject[T]` parameters, principal, credentials or execution-policy overrides. |
| `principal` | Trusted `Principal` | Required trusted context for protected work; omission allowed for unprotected local examples | Supplied by trusted application code, never deserialized from untrusted arguments. Absent representation open. |

Supply the [`Echo`](agents.md#agent) instance defined in the agent reference.
`Echo` is an author-defined class, not an AgentFrame export.

```python
from agentframe import Agent, Runtime


async def echo_text(echo: Agent) -> str:
    return await Runtime().invoke(
        echo, skill="echo", arguments={"text": "Hello"},
    )
```

**Behavior / failures:** resolve the ID, validate arguments, authorize declared
permissions and resource rules, create managed context, apply limits/checks,
execute bounded tools/continuations when explicitly registered, validate/check
the complete output, and return the validated value. Unknown IDs,
undecorated helpers, injected-context attempts, invalid input/output, denial,
policy outage, limit exhaustion, deadline, cancellation, and provider failures
use the decided typed errors below. Refused/incomplete
generation, invalid structured results and action denials must not appear as
successful values.

Direct `await echo.echo(...)` is an ordinary Python call and bypasses this
pipeline. Per-skill limits use `@skill(limits=RunLimits(...))`; the per-run
options keyword/container remains open and separate from business arguments.
Precedence is library -> agent -> skill -> per-run; allowances cannot exceed host ceilings,
weaken security, or extend a parent deadline.

**Related:** [`Principal`](authorization.md#principal),
[`RunContext`](agents.md#runcontext), [`RunLimits`](configuration.md#runlimits),
[local invocation guide](../agent-examples.md#invoke-through-the-local-runtime).

## `Runtime.run`

**Purpose / decision:** execute the same managed invocation as `invoke`, returning
the validated value plus local execution metadata.

**Decided call shape** (generic result typing remains open):

```text
await runtime.run(agent, skill=..., arguments=..., principal=...) -> RunResult
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent`, `skill`, `arguments` | Same as `invoke` | Required as above | Same validation, injection, authorization and limits; no implicit routing. |
| `principal` | Trusted `Principal` | Needed for protected local work | Never supplied as business data. |

```python
from agentframe import Agent, RunResult, Runtime


async def inspect_run(runtime: Runtime, echo: Agent) -> RunResult:
    return await runtime.run(echo, skill="echo", arguments={"text": "Hello"})
```

**Behavior / failures:** metadata works with tracing off. Failures raise the
decided errors, not a success-shaped `RunResult`; error carries `invocation_id`.
Exact failed-run usage access/retention remains open.

**Related:** [`Runtime.invoke`](#runtimeinvoke), [`RunResult`](#runresult).

## `RunResult`

**Purpose / decision:** complete local result and operational metadata, independent
of OpenTelemetry. Not the remote A2A response envelope.

**Signature status:** fields below are decided; constructor, generic typing,
usage type/marker names, duration unit/type and finish-reason values remain open.

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `value` | Selected skill's validated return type | Present on successful run | Same value as `invoke`. |
| `invocation_id` | Correlation identifier; exact type open | Every run | Library-generated, independent of trace IDs; never authentication. |
| `usage` | Normalized input/output/total token usage | Present; completeness/estimate representation open | Mark complete or estimated; unknown is not zero or a fabricated estimate. Preserve unavailable/partial usage explicitly; no hard billing guarantee. |
| `model_calls` | Count | Present | Dispatched inference attempts including retries. |
| `tool_calls` | Count | Present | Managed tool executions; exact attempted/denied-call breakdown remains open. |
| `duration` | Elapsed duration; unit/type open | Present | Run duration, not a provider-only time. |
| `finish_reason` | AgentFrame-normalized finish reason; values open | Present | Completion information, not raw provider data or private reasoning. |

```python
from agentframe import RunResult


def validated_value(result: RunResult) -> object:
    return result.value
```

**Behavior / failures:** remote A2A returns only the invocation ID in response
metadata, not `RunResult`, counters, provider internals or stack traces. Exact
metadata key/placement remains open and must use native extension/metadata rules.
Model-free runs have zero model usage/calls, not guessed inference.

**Related:** [`Runtime.run`](#runtimerun), [accounting](../limits.md#accounting-and-enforcement).

## `AgentFrameError`

**Purpose / decision:** base for public safe failures, distinct from raw provider
exceptions. All errors below inherit the shared correlation/message contract.

**Signature status:** class name and inherited fields decided; constructor and
startup correlation allocation details remain open.

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `invocation_id` | Correlation identifier; exact type open | Every error | Available even on failure; startup/pre-invocation identifier allocation is still to specify. |
| Safe message | Text; accessor/constructor spelling open | Every error | No secrets, provider payloads or stack traces. |

```python
from agentframe import AgentFrameError


def error_correlation(error: AgentFrameError) -> object:
    return error.invocation_id
```

**Behavior / failures:** A2A failures map to standard failed/rejected task states
with a safe message; preserve protocol-level errors and input-required
clarification rather than converting every response to a failed task.
Exact per-class mappings remain open. Log diagnostics with redaction, never send
raw provider exceptions, stack traces or secrets to callers.

**Related:** [specific error classes](#definitionerror), [safe diagnostics](../tracing.md#safe-metadata).

## `DefinitionError`

**Purpose / decision:** startup definition/configuration errors, including missing services.
**Signature status:** decided `AgentFrameError` subtype; constructor open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Fail before serving/executing invalid definitions. |

```python
from agentframe import DefinitionError


def definition_failure(error: DefinitionError) -> object:
    return error.invocation_id
```

**Behavior / failures:** do not defer missing registrations until a request.
**Related:** [`Services`](agents.md#services), [`AgentFrameError`](#agentframeerror).

## `InputValidationError`

**Purpose / decision:** invalid business input or caller injection of managed parameters.
**Signature status:** decided `AgentFrameError` subtype; detailed error fields open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | No unredacted input/secret echo. |

```python
from agentframe import InputValidationError


def invalid_input(error: InputValidationError) -> object:
    return error.invocation_id
```

**Behavior / failures:** no skill effects on invalid input.
**Related:** [`Runtime.invoke`](#runtimeinvoke), [`AgentFrameError`](#agentframeerror).

## `OutputValidationError`

**Purpose / decision:** invalid generated or skill-return data against its typed contract.
**Signature status:** decided `AgentFrameError` subtype; detailed fields open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Never release invalid output as success. |

```python
from agentframe import OutputValidationError


def invalid_output(error: OutputValidationError) -> object:
    return error.invocation_id
```

**Behavior / failures:** validation cannot undo prior actions; authorize before effects.
**Related:** [`RunContext.generate`](agents.md#runcontextgenerate), [`AgentFrameError`](#agentframeerror).

## `AuthenticationError`

**Purpose / decision:** missing/invalid required caller authentication.
**Signature status:** decided `AgentFrameError` subtype; transport details open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Never disclose credentials or token details. |

```python
from agentframe import AuthenticationError


def authentication_failure(error: AuthenticationError) -> object:
    return error.invocation_id
```

**Behavior / failures:** fail protected work, never elevate an absent identity.
**Related:** [`Authenticator`](authorization.md#authenticator), [`AgentFrameError`](#agentframeerror).

## `PermissionDeniedError`

**Purpose / decision:** verified caller lacks required grants or resource/action permission.
**Signature status:** decided `AgentFrameError` subtype; detailed fields open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Avoid exposing private resource existence. |

```python
from agentframe import PermissionDeniedError


def denied(error: PermissionDeniedError) -> object:
    return error.invocation_id
```

**Behavior / failures:** no protected effects on denial.
**Related:** [`AuthorizationPolicy`](authorization.md#authorizationpolicy), [`AgentFrameError`](#agentframeerror).

## `PolicyUnavailableError`

**Purpose / decision:** a required policy integration is unavailable or times out.
**Signature status:** decided `AgentFrameError` subtype; detailed fields open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Outage is not permission. |

```python
from agentframe import PolicyUnavailableError


def policy_failure(error: PolicyUnavailableError) -> object:
    return error.invocation_id
```

**Behavior / failures:** fail closed; an overall run timeout remains a limit failure.
**Related:** [`ExecutionPolicy`](authorization.md#executionpolicy), [`AgentFrameError`](#agentframeerror).

## `ModelError`

**Purpose / decision:** base for model capability, refusal, incomplete-output and provider errors.
**Signature status:** decided `AgentFrameError` subtype; constructor open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | No provider exception passthrough. |

```python
from agentframe import ModelError


def model_failure(error: ModelError) -> object:
    return error.invocation_id
```

**Behavior / failures:** only transient provider failures are retry candidates.
**Related:** [`Model`](models.md#model), [`AgentFrameError`](#agentframeerror).

## `UnsupportedCapabilityError`

**Purpose / decision:** requested model capability is unsupported.
**Signature status:** decided `ModelError` subtype; constructor open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Reject before sending, not text fallback. |

```python
from agentframe import UnsupportedCapabilityError


def unsupported(error: UnsupportedCapabilityError) -> object:
    return error.invocation_id
```

**Behavior / failures:** no inference for unsupported requested features.
**Related:** [`ModelCapabilities`](models.md#modelcapabilities), [`ModelError`](#modelerror).

## `ModelRefusalError`

**Purpose / decision:** the model refused the requested generation.
**Signature status:** decided `ModelError` subtype; constructor open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Refusal is never fake generated text. |

```python
from agentframe import ModelRefusalError


def refused(error: ModelRefusalError) -> object:
    return error.invocation_id
```

**Behavior / failures:** do not retry refusal as a transient provider failure.
**Related:** [`RunContext.generate`](agents.md#runcontextgenerate), [`ModelError`](#modelerror).

## `IncompleteOutputError`

**Purpose / decision:** token-limit truncation without explicit permission for partial results.
**Signature status:** decided `ModelError` subtype; partial opt-in syntax open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Partial content is not complete validated output. |

```python
from agentframe import IncompleteOutputError


def incomplete(error: IncompleteOutputError) -> object:
    return error.invocation_id
```

**Behavior / failures:** no partial-result success by default; opt-in cannot waive output validation.
**Related:** [`ModelResponse`](models.md#modelresponse), [`ModelError`](#modelerror).

## `ProviderError`

**Purpose / decision:** safely translated provider failure.
**Signature status:** decided `ModelError` subtype; transient classification details open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Redact diagnostics; no provider stack/payload for callers. |

```python
from agentframe import ProviderError


def provider_failure(error: ProviderError) -> object:
    return error.invocation_id
```

**Behavior / failures:** transient rate-limit, timeout and server failures alone
may retry with backoff, default two retries (three attempts); SDK retries disabled.
Retry configuration keyword and backoff schedule remain open. Attempts consume
the same token/model-call allowance and deadline; never replay a tool side effect.
**Related:** [retry rules](../limits.md#retries-and-duplicate-requests), [`ModelError`](#modelerror).

## `LimitExceededError`

**Purpose / decision:** base for token, model-call, tool-call and timeout limit failures.
**Signature status:** base name decided; those subtype names remain open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Explicitly distinguish exhausted resource categories. |

```python
from agentframe import LimitExceededError


def exhausted(error: LimitExceededError) -> object:
    return error.invocation_id
```

**Behavior / failures:** do not silently clamp/truncate or reset budgets.
**Related:** [`RunLimits`](configuration.md#runlimits), [`AgentFrameError`](#agentframeerror).

## `CancelledError`

**Purpose / decision:** AgentFrame cancellation failure, distinct from
`asyncio.CancelledError`.
**Signature status:** public name decided; inheritance beyond `AgentFrameError`
and cancellation translation details remain open.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Report cancellation without claiming rollback/free inference. |

```python
from agentframe import CancelledError as AgentFrameCancelledError


def cancelled(error: AgentFrameCancelledError) -> object:
    return error.invocation_id
```

**Behavior / failures:** preserve cancellation propagation and scoped cleanup;
do not swallow Python task cancellation or present it as success. Native A2A
cancellation retains protocol semantics; exact error/state translation is open.
**Related:** [`AgentFrameError`](#agentframeerror), [cancellation](../limits.md#accounting-and-enforcement).

## `StateStore` (deferred)

**Purpose / direction:** an injected structural interface for isolated
conversation state, distinct from an A2A task store. It is not a prerequisite
for the first usable release (text, typed structured generation and bounded
tools with complete validated results).

**Signature status:** no constructor, methods, or field names have been proposed.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Conversation storage dependency | Future structural protocol | Deferred; state methods open | Service injection uses `Inject[T]` / `Services`; isolate state by trusted caller/conversation boundaries, not untrusted context/session IDs. |

```python
from agentframe import StateStore


def describe_store(store: StateStore) -> str:
    return type(store).__name__
```

This only illustrates the future dependency type, not a usable storage API.

**Behavior / failures:** no durability, persistence methods, or automatic state
attachment is promised. Never store mutable per-caller state on a shared agent.
Storage failure semantics and lifecycle must be specified before support.

**Related:** [extension responsibilities](../base-classes.md#authoring-and-extension-decisions),
[A2A task-store boundary](hosting.md#a2aconfig).
