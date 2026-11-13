# Local runtime

[Reference index and status conventions](README.md). Decided calls are design
contracts. As of M1, `Runtime(services=...)`, `invoke`, `run`, `RunResult`,
`Usage`, `FinishReason` and the `AgentFrameError`, `DefinitionError`,
`InputValidationError`, `OutputValidationError`, `PermissionDeniedError` and
`LimitExceededError` classes are implemented from source. M2 adds
`Runtime(authorization=...)`, `PolicyUnavailableError` and `CancelledError`. M3 #33
adds the `FinishReason` model values and the `ModelError` family; M3 #36 adds
`Runtime.validate_model`, token/model-call accounting in `RunResult` and the
`TokenLimitExceededError`, `ModelCallLimitExceededError` and
`TimeoutLimitExceededError` subtypes. M5 #61 adds `ToolCallLimitExceededError`
and real `RunResult.tool_calls` counts. M7 #77 adds `Runtime(policy=...)`; M8+ #84 adds `RemoteAgentError` and #87 adds
`Runtime(directory=...)`, `Runtime.validate_remotes` and `ClarificationNeededError`;
#92 adds `Runtime(remotes=...)` and #93 its `OnBehalfOf` entries.
The remaining parameters and errors
arrive in later milestones. Managed local invocation is
not an A2A wire operation.

## `Runtime`

**Purpose / decision:** own skill registration and the common validated,
authorized, bounded execution pipeline without requiring model inference.

**Proposed constructor shape:**

```text
Runtime(
    *, authorization: AuthorizationPolicy = ...,
    tool_authorization: ToolAuthorizationPolicy = ...,
    policy: ExecutionPolicy = ..., tracing: TracingConfig = ..., services: Services = ...,
    directory: AgentDirectory = ..., remotes: Mapping[str, Service] = ...,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `authorization` | `AuthorizationPolicy` implementation | Needed for protected operations; constructor omission allowed in unprotected local examples | Enforces mandatory skill permissions plus application resource rules. Missing required integration fails closed. |
| `tool_authorization` | [`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy) implementation | Optional; default `None` checks only tools' `requires_permissions` | Checks every model-requested tool call with its validated arguments before it runs. Not required for side-effecting tools, but strongly advised for them. Hosted apps pass it through `AgentApp(runtime=...)`. |
| `policy` | [`ExecutionPolicy`](authorization.md#executionpolicy) | Optional; default `None` runs no content checks | Input, model-request, tool and output checks (M7 #77, #78). They run after authorization and cannot waive it; every configured check runs and fails closed. Hosted apps pass it as `AgentApp(policy=...)` or through `runtime=`. |
| `tracing` | `TracingConfig` | Optional; tracing disabled unless configured | Application owns provider/exporters/sampling; accounting and invocation IDs do not depend on export. |
| `services` | `Services` | Explicit when skills need registered dependencies | Resolve `Inject[T]` parameters; missing registration is a startup `DefinitionError`. Same service container contract as `AgentApp`. |
| `directory` | [`AgentDirectory`](discovery.md#agentdirectory) | Optional; required when a skill takes a [`Remote`](discovery.md#remoteliteralname) parameter (M8+ #87) | Where `Remote[Literal["name"]]` parameters find their agents; each run binds them at its start, within its deadline. The application opens and closes it. |
| `remotes` | `Mapping[str, `[`Service`](authorization.md#service)` \| `[`OnBehalfOf`](authorization.md#onbehalfof)`]` | Optional; default no credentials (M8+ #92, #93) | How to authenticate to each remote agent, by directory name. A remote without an entry is called without credentials, which only works when its card declares no security. Any other value raises `DefinitionError`. Local runs have no signed-in user, so an `OnBehalfOf` remote fails them with `PermissionDeniedError`. |

```python
from agentframe import Runtime


runtime = Runtime()
```

This unprotected local construction requires no identity provider, model, or
telemetry integration. It does not declare any remote access policy. M2
implements `Runtime(*, services: Services | None = None, authorization:
AuthorizationPolicy | None = None)`; omitting `authorization` means no resource
checks. M5 #62 adds `tool_authorization: ToolAuthorizationPolicy | None = None`
and M7 #77 adds `policy: ExecutionPolicy | None = None`. M8+ #87 adds
`directory: AgentDirectory | None = None` and #92 adds
`remotes: Mapping[str, Service | OnBehalfOf] | None = None`.

**Behavior / failures:** no inference on construction. Validate definitions and
configuration; create invocation-scoped context, enforce policy/limits, validate
outputs, and surface the [decided error hierarchy](#agentframeerror).
Definition and missing-service validation happens at startup/preflight, before
executing skills. In M1, definitions validate at class creation and each run
checks its skill's service registrations before any skill code or scoped factory
runs. M2 adds [`Runtime.validate`](#runtimevalidate), which `AgentApp` calls
before serving; other lifecycle methods remain open.
Do not infer extra public registration/close methods from this description.
First release owns bounded registered-tool calls/results and generation
continuations as well as typed structured output; it returns complete validated
results. It has no built-in human approval pause/resume or automatic multi-agent
orchestration. Future [streaming/AG-UI](streaming.md) is a separate surface.

**Related:** [`Runtime.invoke`](#runtimeinvoke), [`AgentApp`](hosting.md#agentapp),
[runtime design](../design/base-classes.md#agent-authoring-and-execution).

## `Runtime.validate`

**Purpose / decision:** check that an agent can run on this runtime before
accepting any work. [`AgentApp`](hosting.md#agentapp) calls it on construction.

**Signature status:** implemented in M2 (#3) as
`Runtime.validate(agent: Agent) -> None`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required | Instance of an `@agent`-decorated class. |
| `limits_ceiling` | `RunLimits \| None` | Optional; `None` caps nothing | A host ceiling that caps the resolved limits, including the capabilities-read timeout. `AgentApp` passes `HostConfig.limits_ceiling`; local callers normally omit it. |

```python
from agentframe import Agent, Runtime, Services


def check(agent: Agent, services: Services) -> None:
    Runtime(services=services).validate(agent)
```

**Behavior / failures:** raises `DefinitionError` when the agent's class is not
decorated with `@agent`, any skill's `Inject[T]` parameter has no registered
service, or a skill takes a `Remote` parameter and the runtime has no
directory. It runs no skill code, creates no scoped services and does no I/O.

**Related:** [`Runtime`](#runtime), [`Services`](agents.md#services).

## `Runtime.validate_model`

**Purpose / decision:** check, before serving any work, that every skill of a
model-backed agent knows its input token limit, so an unknown context size is
never guessed. [`AgentApp`](hosting.md#agentapp) calls it at server startup.

**Signature status:** implemented in M3 (#36) as
`async Runtime.validate_model(agent: Agent, *, limits_ceiling: RunLimits | None = None) -> None`;
`limits_ceiling` added in M7 (#71).

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required | Instance of an `@agent`-decorated class. |
| `limits_ceiling` | `RunLimits \| None` | Optional; `None` caps nothing | A host ceiling that caps the resolved limits, including the capabilities-read timeout. `AgentApp` passes `HostConfig.limits_ceiling`; local callers normally omit it. |

```python
from agentframe import Agent, Runtime


async def check_model(agent: Agent) -> None:
    await Runtime().validate_model(agent)
```

**Behavior / failures:** agents without a model pass. Otherwise it reads
`agent.model.capabilities()` (which may do I/O, so it is bounded by the agent's
timeout) and, for each skill, resolves the effective `RunLimits` (library →
agent → skill), capped by `limits_ceiling` when given. A skill whose `max_input_tokens` is unset while the model
reports no context window raises `DefinitionError` naming the skill. Local runs
that skip this check fail the same way before their first model call.
`ProviderError` propagates when capabilities cannot be read; reading them past
the agent's timeout raises a transient `ProviderError`.

**Related:** [`RunLimits`](configuration.md#runlimits),
[`ModelCapabilities`](models.md#modelcapabilities).

## `Runtime.validate_remotes`

**Purpose / decision:** check, before serving any work, that every
[`Remote`](discovery.md#remoteliteralname) parameter's agent can be called.
[`AgentApp`](hosting.md#agentapp) calls it at server startup.

**Signature status:** implemented in M8+ (#87) as
`async Runtime.validate_remotes(agent: Agent) -> None`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required | Instance of an `@agent`-decorated class. |

```python
from agentframe import Agent, AgentDirectory, Runtime


async def check_remotes(agent: Agent, directory: AgentDirectory) -> None:
    async with directory:
        await Runtime(directory=directory).validate_remotes(agent)
```

**Behavior / failures:** looks every distinct name up in the directory,
fetching its card. Agents without `Remote` parameters pass without I/O.
Raises `DefinitionError` when a skill takes a `Remote` parameter and there is
no directory, the directory is not open, no source lists a name, a card
requires credentials that the name's `remotes=` entry cannot provide (see
[`Service`](authorization.md#service)), or a remote with credentials uses a
directory client that follows redirects; raises
[`RemoteAgentError`](#remoteagenterror) when a card cannot be fetched or is
invalid. Local runs that skip it fail the same way when the skill runs.

**Related:** [`Remote`](discovery.md#remoteliteralname),
[`AgentDirectory`](discovery.md#agentdirectory).

## `Runtime.invoke`

**Purpose / decision:** explicitly select a registered local skill by ID and
return its validated value through the managed execution pipeline.

**Decided value-return call** (implemented in M1; generic typing remains open):

```python
async def invoke(
    self, agent: Agent, *, skill: str,
    arguments: Mapping[str, Any] | None = None, principal: Principal | None = None,
    options: RunOptions | None = None,
) -> Any: ...
```

`Mapping` is from `collections.abc`. The return is the selected skill's declared
value (for example `str` or `int`), not a result envelope;
`Any` above does not settle future generic typing.
The value-return path is decided. Use [`run`](#runtimerun) for a `RunResult`
containing execution metadata; both paths use the same pipeline.

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required | Valid definition with registered declared/inherited skills. |
| `skill` | `str` | Required | Stable registered skill ID, not arbitrary method lookup or implicit model routing. |
| `arguments` | String-keyed mapping of business values | Optional; `None` (default) means no arguments | Validated against selected skill. No `self`, `ctx`, `Inject[T]` parameters, principal, credentials or execution-policy overrides: those names count as unknown arguments. |
| `principal` | Trusted `Principal \| None` | Default `None`; required for skills with `requires_permissions` | Supplied by trusted application code, never deserialized from untrusted arguments. `None` means no identity. |
| `options` | [`RunOptions \| None`](configuration.md#runoptions) | Default `None` (no per-run overrides; M3 #42) | Limits and model settings for this run only, kept apart from business arguments. Each field set overrides the skill's, agent's and library's value ([precedence](configuration.md#precedence)). Hosted callers cannot supply them. |

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

M1 runs these steps in order: create the invocation ID; resolve the skill
(unknown -> `InputValidationError`); check service registrations
(`DefinitionError`); check all-of `requires_permissions` against the verified caller
(`PermissionDeniedError`); validate arguments (`InputValidationError`); start
the run's deadline; ask the configured
[`AuthorizationPolicy`](authorization.md#authorizationpolicy) within that
deadline (deny -> `PermissionDeniedError`, policy exception or deadline expiry
-> `PolicyUnavailableError`); resolve scoped services and call the skill
within the remaining time (`LimitExceededError`);
validate the output (`OutputValidationError`). Effective limits and model
settings are merged field by field from library defaults, agent, skill and
`options` ([precedence](configuration.md#precedence)). Exceptions raised
by the skill itself propagate unchanged; a `TimeoutError` the skill raises
itself is not converted into a limit failure.

Direct `await echo.echo(...)` is an ordinary Python call and bypasses this
pipeline. Per-skill settings use `@skill(limits=..., model_settings=...)`;
per-run settings use `options=RunOptions(...)`, separate from business
arguments. Precedence is library → agent → skill → per-run; allowances must not
exceed host ceilings (M7 #71), weaken security, or extend a parent deadline.

**Related:** [`Principal`](authorization.md#principal),
[`RunContext`](agents.md#runcontext), [`RunLimits`](configuration.md#runlimits),
[local invocation guide](../guides/agent-examples.md#invoke-through-the-local-runtime).

## `Runtime.run`

**Purpose / decision:** execute the same managed invocation as `invoke`, returning
the validated value plus local execution metadata.

**Decided call shape** (implemented in M1; returns `RunResult[Any]`, generic
result typing remains open):

```text
await runtime.run(
    agent, skill=..., arguments=..., principal=..., options=...
) -> RunResult
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent`, `skill`, `arguments` | Same as `invoke` | Required as above | Same validation, injection, authorization and limits; no implicit routing. |
| `principal` | Trusted `Principal` | Needed for protected local work | Never supplied as business data. |
| `options` | `RunOptions \| None` | Default `None` | Same as [`invoke`](#runtimeinvoke): per-run limits and model settings, merged field by field. |

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

**Signature status:** fields below are decided and implemented in M1 as a
frozen generic dataclass `RunResult[T]`.

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `value` | `T`, the selected skill's validated return type | Present on successful run | Same value as `invoke`. |
| `invocation_id` | `str` (UUID 4) | Every run | Library-generated, independent of trace IDs; never authentication. |
| `usage` | `Usage` | Present | Normalized token usage; see below. |
| `model_calls` | `int` | Present | Dispatched inference attempts including retries; `0` for model-free runs. |
| `tool_calls` | `int` | Present (M5 #61) | Every tool call the model asked for, including ones that did not run because of an unknown tool name or invalid arguments. `0` for model-free and tool-free runs. |
| `duration` | `datetime.timedelta` | Present | Run duration, not a provider-only time. |
| `finish_reason` | `FinishReason` (`StrEnum`) | Present | Completion information, not raw provider data or private reasoning. Values: `COMPLETED`, `LENGTH` (output token limit reached), `REFUSED`, `CONTENT_FILTER` (decided, M3 #33); `TOOL_CALLS` (M5 #59) describes a model call that asks for tools and is never a run's finish reason; more only when a later milestone needs them. Model-free runs report `COMPLETED`. |

`Usage(input_tokens, output_tokens, total_tokens, estimated)` is a frozen
dataclass. Each count is `int | None`, where `None` means unknown, never zero;
`estimated: bool` marks estimates. Model-free runs report
`Usage(0, 0, 0, estimated=False)`. Since M3 #36 `usage` and `model_calls` add
up every model call of the run, including failed ones. Counts the provider did
not report for a returned response are filled with a character-based estimate
(four characters per token) and `estimated=True`; a failed or cancelled call
with no reported usage makes the counts unknown (`None`). A successful run's
`finish_reason` is `COMPLETED`: other finish reasons fail the run. The same `Usage` and `FinishReason` describe
a single model call in [`ModelResponse`](models.md#modelresponse). Provider completeness details and no hard
billing guarantee remain as described in [accounting](../guides/limits.md#accounting-and-enforcement).

```python
from agentframe import RunResult


def validated_value(result: RunResult) -> object:
    return result.value
```

**Behavior / failures:** ordinary remote A2A does not return `RunResult`,
provider internals or stack traces. Task metadata carries the invocation ID and,
since M3 #36, a safe usage snapshot (token counts, `estimated` and
`model_calls`); see [result and failure mapping](hosting.md#result-and-failure-mapping).
The negotiated M8+ extension separately reports actual usage for cross-hop
accounting, not full local execution results or provider payloads.
Model-free runs have zero model usage/calls, not guessed inference.

**Related:** [`Runtime.run`](#runtimerun), [accounting](../guides/limits.md#accounting-and-enforcement).

## `AgentFrameError`

**Purpose / decision:** base for public safe failures, distinct from raw provider
exceptions. All errors below inherit the shared correlation/message contract.

**Signature status:** decided and implemented in M1:
`AgentFrameError(message: str, *, invocation_id: str | None = None)`. Host
startup correlation allocation details remain open.

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `invocation_id` | `str \| None` | Set on every error raised during a run | The run's UUID. `None` only for definition errors raised while a class is being defined. |
| `message` | `str` | Every error | Safe text: no secrets, submitted values, provider payloads or stack traces. Validation errors list field locations and problems only, and are raised without a chained Pydantic exception. |

```python
from agentframe import AgentFrameError


def error_correlation(error: AgentFrameError) -> object:
    return error.invocation_id
```

**Behavior / failures:** A2A failures map to standard failed/rejected task states
with a safe message; preserve protocol-level errors and input-required
clarification rather than converting every response to a failed task. The
per-class mapping is in [result and failure mapping](hosting.md#result-and-failure-mapping)
(M2 #7). Log diagnostics with redaction, never send
raw provider exceptions, stack traces or secrets to callers.

**Related:** [specific error classes](#definitionerror), [safe diagnostics](../design/tracing.md#safe-metadata).

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
M1 raises it at class definition for invalid `@agent` / `@skill` usage, for
invalid `RunLimits`, for duplicate `Services` registrations, from `definition()`
on undecorated classes, and before running a skill whose `Inject[T]`
registration is missing.
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

**Behavior / failures:** no skill effects on invalid input. M1 raises it for an
unknown skill id and for unknown, missing or invalid arguments.
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
**Signature status:** implemented `AgentFrameError` subtype (M2 story #4), raised
by authenticators before any run exists, so `invocation_id` is `None`. The host
maps it to HTTP 401.

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

**Behavior / failures:** no protected effects on denial. M1 raises it when a
skill declares `requires_permissions` and the principal is `None` or lacks any
listed permission; the message names the missing permissions. Since M2 it is
also raised when the `AuthorizationPolicy` denies a run, with a generic message
that does not reveal whether the requested resource exists. Since M5 #62 it is
raised when the model calls a tool the caller lacks a `requires_permissions`
entry for, or the [`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy)
denies a call; the denial fails the run and is never sent to the model.
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

**Behavior / failures:** fail closed. Since M2, `Runtime` raises it when the
[`AuthorizationPolicy`](authorization.md#authorizationpolicy) raises or the run's
deadline expires while the policy is deciding; a deadline that expires in the
skill body remains a `LimitExceededError`. Since M5 #62 the same holds for the
[`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy) checking
a tool call. The message never includes the
policy's own error; the original exception is chained for server-side diagnostics.
**Related:** [`AuthorizationPolicy`](authorization.md#authorizationpolicy), [`ExecutionPolicy`](authorization.md#executionpolicy), [`AgentFrameError`](#agentframeerror).

## `ModelError`

**Purpose / decision:** base for model capability, refusal, incomplete-output and provider errors.
**Signature status:** implemented (M3 #33) `AgentFrameError` subtype with the
constructor `(message, *, usage=None, invocation_id=None)`, shared by its
subtypes except `ProviderError`. Adapters raise them without an invocation ID;
the runtime attaches the run's ID.

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | No provider exception passthrough. |
| `usage` | `Usage \| None` | `None` | Tokens the failed call consumed, when the adapter knows them (M3 #36). The run's accounting settles to it instead of dropping it. |

```python
from agentframe import ModelError


def model_failure(error: ModelError) -> object:
    return error.invocation_id
```

**Behavior / failures:** only transient provider failures are retry candidates.
**Related:** [`Model`](models.md#model), [`AgentFrameError`](#agentframeerror).

## `UnsupportedCapabilityError`

**Purpose / decision:** requested model capability is unsupported.
**Signature status:** implemented `ModelError` subtype (M3 #33).

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
**Signature status:** implemented `ModelError` subtype (M3 #33).

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

**Purpose / decision:** the model's output was cut short or empty. Catching it
is the explicit opt-in for partial text; there is no flag (M4 #54).
**Signature status:** implemented `ModelError` subtype (M3 #33);
`partial_text` implemented (M4 #54).

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID, safe message and optional `usage` | Required | Partial content is not complete validated output. |
| `partial_text` | `str \| None` | Default `None` | Text received before a **text** generation stopped at its output token limit. `None` for structured output, empty output and other incomplete reasons. |

```python
from agentframe import Agent, IncompleteOutputError, RunContext, agent, skill


@agent(version="1.0.0")
class Drafter(Agent):
    """Drafts text, accepting an answer cut short at the token limit."""

    @skill("draft", tags=["text"])
    async def draft(self, topic: str, ctx: RunContext) -> str:
        """Draft text about a topic."""
        try:
            return await ctx.generate(topic)
        except IncompleteOutputError as error:
            if error.partial_text is None:
                raise
            return error.partial_text + " [truncated]"
```

**Behavior / failures:** `ctx.generate` never returns partial text; an uncaught
error fails the run (hosted: task `FAILED`). A skill that catches it and
returns normally completes with `RunResult.finish_reason` `COMPLETED`; its
return value is still validated, and the truncated call's usage still counts.
The safe message, hosted failure messages, task metadata and logs never
contain `partial_text`. Structured output is never returned partially parsed.
**Related:** [`ModelResponse`](models.md#modelresponse), [`ModelError`](#modelerror).

## `ProviderError`

**Purpose / decision:** safely translated provider failure.
**Signature status:** implemented `ModelError` subtype (M3 #33). Per-provider
transient classification belongs to each adapter.

```text
ProviderError(message, *, transient, retry_after=None, usage=None, invocation_id=None)
```

| Fields | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| Inherited error contract | Invocation ID and safe message | Required | Redact diagnostics; no provider stack/payload for callers. |
| `transient` | `bool` | Required keyword | `True` when retrying may succeed (rate limit, timeout, server error). Only transient errors are retried. |
| `retry_after` | `float \| None` | `None` | Seconds the provider asked callers to wait, from its hint. |
| `usage` | `Usage \| None` | `None` | Tokens the failed call consumed, when reported, so accounting never treats a failed call as free. |

```python
from agentframe import ProviderError


def provider_failure(error: ProviderError) -> object:
    return error.transient, error.retry_after
```

**Behavior / failures:** transient rate-limit, timeout and server failures alone
may retry with backoff, default two retries (three attempts); SDK retries disabled.
Configured by `RunLimits.max_model_retries`; the wait honors `retry_after`, else
exponential backoff with jitter, never past the deadline. Every attempt consumes
the same token/model-call allowance and deadline; never replay a tool side effect.
**Related:** [retry rules](../guides/limits.md#retries-and-duplicate-requests), [`ModelError`](#modelerror).

## `LimitExceededError`

**Purpose / decision:** base for token, model-call, tool-call and timeout limit failures.
**Signature status:** implemented base; the token, model-call and timeout
subtypes below are implemented (M3 #36) and the tool-call subtype
`ToolCallLimitExceededError` (M5 #61).
`except LimitExceededError` catches all of them.

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

## `TokenLimitExceededError`

**Purpose / decision:** a model call does not fit the run's token limits.
**Signature status:** implemented (M3 #36) `LimitExceededError` subtype with the
inherited constructor.

```python
from agentframe import TokenLimitExceededError


def too_large(error: TokenLimitExceededError) -> str:
    return error.message
```

**Behavior / failures:** raised before the call is sent when the estimated
input exceeds the input limit, input plus `max_output_tokens` exceeds the
model's context window, or the call's reservation (input estimate +
`max_output_tokens`) does not fit what is left of `max_total_tokens`. Input is
never truncated and the output cap is never lowered to fit.
**Related:** [accounting](../guides/limits.md#implemented-token-accounting-m3).

## `ModelCallLimitExceededError`

**Purpose / decision:** a model call would exceed `RunLimits.max_model_calls`.
**Signature status:** implemented (M3 #36) `LimitExceededError` subtype with the
inherited constructor.

```python
from agentframe import ModelCallLimitExceededError


def too_many(error: ModelCallLimitExceededError) -> str:
    return error.message
```

**Behavior / failures:** raised before the call is sent. Failed calls count
toward the limit; a call refused with `UnsupportedCapabilityError` before it
was sent does not.
**Related:** [`RunLimits`](configuration.md#runlimits).

## `ToolCallLimitExceededError`

**Purpose / decision:** a tool call would exceed `RunLimits.max_tool_calls`.
**Signature status:** implemented (M5 #61) `LimitExceededError` subtype with the
inherited constructor.

```python
from agentframe import ToolCallLimitExceededError


def too_many_tools(error: ToolCallLimitExceededError) -> str:
    return error.message
```

**Behavior / failures:** raised before the extra tool call runs. Every call a
model asks for counts, including ones naming an unknown tool or with invalid
arguments.
**Related:** [`RunLimits`](configuration.md#runlimits),
[tool calling](agents.md#implemented-tool-calling-m5-61).

## `TimeoutLimitExceededError`

**Purpose / decision:** the run's `RunLimits.timeout_seconds` expired.
**Signature status:** implemented (M3 #36) `LimitExceededError` subtype with the
inherited constructor; the timeouts M1–M3 raised as the base class now raise
this subtype.

```python
from agentframe import TimeoutLimitExceededError


def too_slow(error: TimeoutLimitExceededError) -> str:
    return error.message
```

**Behavior / failures:** raised when the skill, a model call, a tool call or
free-text routing runs past the run's deadline; scoped-service cleanup still runs.
**Related:** [`RunLimits`](configuration.md#runlimits).

## `CancelledError`

**Purpose / decision:** AgentFrame cancellation failure, distinct from
`asyncio.CancelledError`.
**Signature status:** implemented in M2 (#7) as a direct `AgentFrameError`
subclass with the inherited constructor. Over A2A it ends the task in
`CANCELED` ([mapping](hosting.md#result-and-failure-mapping)): the host
reports an A2A `CancelTask` with it, while the running skill itself stops
through ordinary `asyncio` cancellation. `Runtime.run` does not raise it.
Decided (M7 #79): local runs never convert `asyncio.CancelledError`; cancelling
the task running `Runtime.run` re-raises it unchanged, even during a policy
check, after per-run services are cleaned up. AgentFrame `CancelledError` only
reports a cancelled hosted task (`CancelTask` or shutdown), and later a
cancelled remote call (M8+).

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
cancellation retains protocol semantics.
**Related:** [`AgentFrameError`](#agentframeerror), [cancellation](../guides/limits.md#accounting-and-enforcement).

M8+ #87 also adds [`ClarificationNeededError`](discovery.md#clarificationneedederror),
an `AgentFrameError` carrying a remote input-required question with the remote
task and context IDs; no automatic back-and-forth. The remote task stays
input-required; it is not reported as a failed remote task.

## `RemoteAgentError`

**Purpose / decision:** a failure caused by another agent rather than by local
configuration: an unreachable agent, an invalid or refused Agent Card, or a
remote task that did not complete ([`Remote.ask`](discovery.md#remoteask), #87).
**Signature status:** implemented in M8+ #84 as a direct `AgentFrameError`
subclass:
`RemoteAgentError(message: str, *, task_id: str | None = None, task_state: str | None = None, invocation_id: str | None = None)`.

| Field | Type | Required/default status | Meaning |
| --- | --- | --- | --- |
| `task_id` | `str \| None` | Default `None` | The remote A2A task's ID, when the failure concerns one. |
| `task_state` | `str \| None` | Default `None` | The remote task's final state, when known. |
| Inherited error contract | Invocation ID and safe message | Required | The message names the agent, never the remote response body. |

```python
from agentframe import RemoteAgentError


def remote_task(error: RemoteAgentError) -> str | None:
    return error.task_id
```

**Behavior / failures:** configuration mistakes (an unknown agent name, the same
name in two sources, an unreadable source) stay `DefinitionError`.
A remote task's own status text is never put in the message.
`ClarificationNeededError` stays a separate error.
**Related:** [`AgentDirectory`](discovery.md#agentdirectory),
[`AgentFrameError`](#agentframeerror).

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

**Related:** [extension responsibilities](../design/base-classes.md#authoring-and-extension-decisions),
[A2A task-store boundary](hosting.md#a2aconfig).
