# Hosting

[Reference index and status conventions](README.md). These are design contracts,
not implemented servers or compatibility claims. Approved auth/protection
fields/defaults below are decided; unspecified composition details stay open. ASGI
binding/TLS and external infrastructure remain application responsibilities.

## `AgentApp`

**Purpose / decision:** compose an agent and common runtime with native A2A
hosting and optional MCP, rather than require a server subclass. Use one server,
one shared runtime and an endpoint per caller audience, not per skill: A2A for
agent callers, MCP for LLM apps/tools, and later AG-UI for frontends.

**Constructor status:** `services=`, M8+ `directory=` and later `ag_ui=` are
decided; the surrounding composition
shape and unspecified omission details remain illustrative.

```text
AgentApp(
    *, agent: Agent, host: HostConfig, runtime: Runtime = ...,
    a2a: A2AConfig = ..., mcp: MCPConfig = ..., router: SkillRouter = ...,
    services: Services = ...,
    directory: AgentDirectory = ..., ag_ui: AGUIConfig = ...,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `agent` | `Agent` instance | Required in current shape | Valid registered skills and identity; no per-method A2A RPC endpoints. |
| `host` | `HostConfig` | Explicit in current examples; omission contract open | Advertised origin, authentication integration, independent resource ceilings. |
| `runtime` | `Runtime` | Optional in examples; default construction details open | Shared managed validation, authorization, checks, limits, accounting and tracing for all enabled entry points. |
| `a2a` | `A2AConfig` | A2A enabled by default; exact object default open | Selected native binding and implemented features only. |
| `mcp` | `MCPConfig` | MCP disabled by default; exact object default open | Explicit optional allowlisted skill publication. |
| `router` | `SkillRouter` implementation | Configured for free-text A2A selection/extraction | Custom router in M2 or built-in opt-in model router in M6. Activated structured selection bypasses it, not runtime controls. No finalized extraction/clarification interface. |
| `services` | `Services` | Register dependencies used by `Inject[T]` | Startup validates required services; app owns/closes singletons, AgentFrame creates/closes per-run scoped instances. Shared with runtime; conflicting app/runtime container handling remains open. |
| `directory` | `AgentDirectory` | M8+ when remote bindings need discovery | Resolve `Remote["name"]`; validate configured names at startup. Sources/cache lifecycle follows ownership, not automatic transfer. |
| `ag_ui` | `AGUIConfig` | Later explicit opt-in, off unless enabled | `AGUIConfig(enabled=True)` serves `/ag-ui` with the same auth/permissions/limits. |

Supply an instance of [`Echo`](agents.md#agent) for this single-skill
composition. `auth` is an explicit `Authenticator`;
`authorization` supplies applicable permission/resource policy.

```python
from agentframe import (
    Agent, AgentApp, Authenticator, AuthorizationPolicy, HostConfig, Runtime,
)


def build_app(
    echo: Agent, auth: Authenticator, authorization: AuthorizationPolicy,
) -> AgentApp:
    return AgentApp(
        agent=echo,
        runtime=Runtime(authorization=authorization),
        host=HostConfig(
            public_url="https://echo.example.com",
            auth=auth,
        ),
    )
```

Supply `EntraIDAuth`, `APIKeyAuth`, explicit `AllowAnonymous`, or a custom
authenticator; no remote host starts without a choice.

**Behavior / failures:** composing the ASGI application does not start a server.
Generate a native card from identity/skills plus actual supported host features;
do not expose instructions, secrets, private tools, or imaginary capabilities.
Reject invalid definitions/configuration and unsupported required adapters.
Messages, tasks, artifacts, cancellation, and safe failures follow the selected
official protocol/SDK version. Stream disconnection is not automatically task
cancellation; in-memory task storage is ephemeral. First release returns
complete validated results and must not advertise A2A streaming. Protocol tasks
do not establish built-in human approval pause/resume. Ordinary A2A callers receive only
the invocation ID in response metadata, not internal `RunResult` fields.
Safe failures map to failed/rejected task states as applicable; protocol failures
and input-required clarification retain native semantics.

Duplicate A2A requests from the same verified caller with the same `messageId`
within the default 10-minute window return the original task instead of
re-running. Default storage is in-memory; a pluggable store supports multiple
instances. This is not exactly-once execution. Window/store configuration
syntax, atomicity details, conflicting-payload handling and message-only result
interaction remain open; never claim process restarts preserve memory.

The proposed HTTP deployment uses `/.well-known/agent-card.json`, `/a2a`, and
optional MCP Streamable HTTP at `/mcp`; binding/version choices still need
verification. No extra public `serve()`, card-generation, or lifecycle method
signature is specified here.

For next-core-release [discovery](discovery.md#agentdirectory), host lifespan may
manage a library-created or explicitly transferred directory. Merely injecting
a directory does not transfer ownership. The decided constructor keyword is
`directory=`; no new lifecycle method is finalized. Directory refresh/removal is not A2A task
cancellation or agent deployment.
Future [AG-UI streaming](streaming.md) is a separate optional frontend adapter;
`ag_ui=AGUIConfig(enabled=True)` and `/ag-ui` are decided, not implemented
conformance. Disconnect cancels the AG-UI run with no first-version resume;
this does not redefine A2A stream-disconnect task semantics.

**Related:** [`HostConfig`](#hostconfig), [`SkillRouter`](#skillrouter),
[combined server example](../mcp-skills.md#proposed-server-composition).

## `agentframe run`

**Purpose / decision:** explicitly start one agent through the native host in
M2. Approved command: `agentframe run myapp.research:ResearchAgent`.
The command is not implemented or installed in this repository.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Agent target | `module:Class` string | Required | Application module and agent class. Loading/factory details and host/config flags remain open. |
| Host configuration | Explicit trusted application configuration; carrier open | Required remotely | Auth choice, limits, advertised address and readiness rules still apply. No implicit anonymous startup. |

```shell
agentframe run myapp.research:ResearchAgent
```

**Behavior / failures:** startup reports invalid import, agent definition,
configuration or missing auth. Importing agents never starts serving; shutdown
uses the host grace period, not abrupt unbounded child work.

**Related:** [single-agent development](../development.md#one-agent-m2),
[`HostConfig`](#hostconfig).

## `agentframe dev`

**Purpose / decision:** in M8+, start all named agents from `agentframe.yaml`
on localhost with generated development discovery/auth and one combined log.
Approved command: `agentframe dev`; not implemented tooling.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agentframe.yaml` | YAML named-agent/module-path/port list | Default config file | Detailed keys, loading/bootstrap and overrides open; not the registry file schema. |
| Local registry | Generated YAML name/URL references | Automatic for named discovery | Path/conflict handling open; never overwrite user configuration silently. |
| Development auth | Managed API-key service identity | Automatic between dev agents | No fabricated OBO identity; key storage/rotation/cleanup open. Never put keys in registry or logs. |
| Combined log | Agent and invocation ID tags | Decided output | Preserve redaction/content-capture defaults; not durable workflow state. |

```shell
agentframe dev
```

**Behavior / failures:** report invalid config, port conflicts and partial
startup; bound cleanup of owned processes/resources. Development keys are not
production defaults. Container packaging is one container per agent; Kubernetes/
Foundry templates later, not infrastructure deployment management.

**Related:** [configuration and packaging examples](../development.md),
[`AgentDirectory`](discovery.md#agentdirectory).

## `HostConfig`

**Purpose / decision:** separate actual deployment/security constraints from
agent defaults and adapter-specific settings.

**Constructor status:** `auth`, `role_permissions` and protection fields/defaults
below are decided. Existing URL/ceiling composition is illustrative where not specified.

```text
HostConfig(
    *, public_url: str, auth: Authenticator, role_permissions = ...,
    limits_ceiling: RunLimits = ...,
    max_concurrent_runs: int = 32, max_request_bytes: int = 1048576,
    max_response_bytes: int = 4194304, shutdown_grace_seconds: ... = 30,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `public_url` | `str` | Explicit in current shape | Advertised origin/endpoint configuration, not server binding or TLS setup. Do not derive it from untrusted forwarded headers. |
| `auth` | `Authenticator` | Required for remote hosting; no default | Built-in Entra/API key/explicit anonymous or custom; omitted choice refuses startup. |
| `role_permissions` | Role-to-permission mapping | Optional application mapping; omitted-container shape open | Maps verified roles, e.g. `{"Editor": ["documents:write"]}`; never trusts roles from skill arguments. Permissions are the union over the caller's verified roles; unknown roles grant nothing. Entra scope details remain open. |
| `limits_ceiling` | `RunLimits` | Independent host constraint; omission/default values open | Non-overridable ceilings, not agent defaults. Validate configured agent allowances before accepting work. |
| `max_concurrent_runs` | `int` | `32`, configurable | Excess is rejected immediately with a standard busy error, not indefinitely queued; exact binding error mapping open. |
| `max_request_bytes` | `int` | `1048576` (1 MiB), configurable | Bound ingress before work; protocol/encoding measurement details open. |
| `max_response_bytes` | `int` | `4194304` (4 MiB), configurable | Bound outgoing content; no silent truncation into successful output. Exact error mapping open. |
| `shutdown_grace_seconds` | Numeric seconds; exact type open | `30`, configurable | Stop accepting, drain up to grace, then cancel remaining runs and clean up scoped services. |

```python
from agentframe import Authenticator, HostConfig, RunLimits


def host_config(auth: Authenticator) -> HostConfig:
    return HostConfig(
        public_url="https://writer.example.com",
        auth=auth,
        limits_ceiling=RunLimits(
            max_input_tokens=8_000,
            max_total_tokens=16_000,
            max_model_calls=6,
            timeout_seconds=45,
        ),
    )
```

The application selects an authenticator. Explicit `limits_ceiling` values
above are example ceilings, not new library defaults; host protection defaults
are the decided values in the table.

**Behavior / failures:** require intentional authentication and HTTPS outside
local development. A card's security declaration is not enforcement.
Host security/ceilings cannot be weakened by agent/run options; reject invalid
or excessive explicit requests instead of silently clamping. Missing identity/
required integration fails protected work. The decided admission, byte and
shutdown fields must be enforced independently
of token limits. `/health` is liveness; `/ready` reports startup checks passed
and not shutting down. Startup includes auth/definition/service validation.
Probe status/body schema and byte-accounting/error details remain open.

**Related:** [`Principal`](authorization.md#principal),
[`AgentConfig`](configuration.md#agentconfig),
[host ceilings](../limits.md#defaults-versus-ceilings).

## `A2AConfig`

**Purpose / decision:** configure the first-class A2A adapter, using official
protocol/SDK types and actual supported endpoint behavior. Accept preferred
structured selection plus JSON arguments through a declared/activated A2A 1.0
extension, and free-text requests through custom or opt-in model routing.

**Proposed known constructor fields:**

```text
A2AConfig(*, enabled: bool = ..., path: str = ...)
```

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `enabled` | `bool` | A2A enabled by default on `AgentApp`; field default open | Controls the adapter, not caller authorization. |
| `path` | `str` | `/a2a` in examples; concrete default open | Selected A2A binding endpoint, not a route per skill. |
| Binding, task-store integration, supported capabilities | Exact fields/types open | Select/pin and verify before release | Reuse SDK interfaces; first-release complete results only. Do not advertise streaming/push/task features the host cannot execute. |
| Structured requests | A2A extension declaration/activation, data-part arguments; exact fields open | Preferred request style; no new constructor keyword selected | Card declares support; caller selects a skill under that extension, bypassing routing but not authorization/typed validation/limits/accounting/tracing. |
| Prompt clarification | Native A2A task/status message; exact contract open | Required for ambiguous/missing prompt input | Return `TASK_STATE_INPUT_REQUIRED` with an ask-back, not a guessed invocation or successful final result. |
| Duplicate requests | Same caller + A2A `messageId`; store/window fields open | 10-minute window, in-memory default; configurable/pluggable | Return original task instead of re-running; multi-instance store support, no exactly-once claim or task-access bypass. |
| Response correlation/errors | Native response metadata/task states; exact mappings open | Only invocation ID exposed from run internals | Safe failed/rejected states as appropriate; no `RunResult`, raw provider exception, stack trace or secrets. |
| Long-running tasks | Native A2A task store; constructor setting open | In-memory default, persistent store pluggable | Task persistence is not Python skill checkpointing, human approval resume or exactly-once execution. |

```python
from agentframe import A2AConfig


a2a = A2AConfig(enabled=True, path="/a2a")
```

**Behavior / failures:** structured selection is deterministic with no routing
model call/cost; the selected skill can still run inference. Unknown skill IDs
or invalid arguments fail explicitly, without falling back to prompt routing.
Extension-unaware callers use free text; ambiguous/missing prompt input asks
back using the native input-required task state, never the first registered
skill or a guess. Native [extension/part/task mechanisms](../mcp-skills.md#a2a-request-styles-and-extension-boundary)
are verified against A2A 1.0, but extension URI/name/schema, selector placement,
support advertisement details and clarification/follow-up API remain open.
Typed business-input/output mappings and task lifecycle still need
version-specific evidence, including typed structured outputs. Re-authorize protected task/resource access.
Invalid/unsupported configuration and protocol failures must be explicit and
safe; no copied schemas or custom response envelope.
For M8+ peers the negotiated AgentFrame extension also carries deadline,
token ceiling/actual usage and max-5-hop depth accounting. Ordinary callers
still receive no internal result metadata beyond invocation correlation.
Remote client input-required raises `ClarificationNeededError`; the remote task
remains input-required, not automatically failed or conversed with.

**Related:** [`SkillRouter`](#skillrouter),
[native SDK types](#native-a2a-and-mcp-types),
[A2A routing guide](../agent-examples.md#a2a-hosting-and-selection).

## `MCPConfig`

**Purpose / decision:** optional native MCP publication of explicitly
allowlisted registered skills, separate from internal model-callable `@tool`s.

**Proposed constructor shape:**

```text
MCPConfig(*, enabled: bool = ..., path: str = ..., exposed_skills: list[str] = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `enabled` | `bool` | MCP disabled by default on `AgentApp`; field default open | Explicitly opt into the optional adapter/dependency. |
| `path` | `str` | `/mcp` in HTTP examples; default open | Native Streamable HTTP endpoint, not per-skill REST routes. Stdio, if added, is a separate transport. |
| `exposed_skills` | `list[str]` in examples | Empty by default; opt-in per skill | Only registered stable IDs. No wildcard, auto-exposure, or authorization grant. Exact default construction syntax remains proposed. |

```python
from agentframe import MCPConfig


mcp = MCPConfig(
    enabled=True, path="/mcp", exposed_skills=["summarize", "rewrite"],
)
```

Use with an agent registering those IDs, such as the guide's
[`WritingAssistant`](../agent-examples.md#with-a-model).

**Behavior / failures:** initial mapping uses stable skill IDs as native MCP
tool names and excludes context, `Inject[T]` services, credentials and execution internals from
schemas. Reject unknown IDs, naming collisions, unsupported schemas, missing
optional dependencies, and context/policy argument injection; never silently
skip/rename. MCP named selection bypasses A2A routing, not validation,
authorization, limits, checks, or tracing. Apply discovery policy and enforce it
again at invocation; hidden names cannot remain callable through that policy.

Use SDK-native initialization, version negotiation, tool results, and errors.
Exact structured-result mapping (including the design's possible scalar
`{"value": ...}` wrapper), authentication hooks, and async-task support remain
open/version-dependent, not settled wire fields or features. Sharing an A2A
host does not prove MCP authorization conformance. Custom REST remains deferred.
Tool-like deterministic/typed/quick/single-shot skills are good MCP candidates;
judgment, multi-turn, clarification or long-running skills usually stay A2A-only.
This is guidance, not an enforced restriction on model-backed MCP tools.

**Related:** [`@tool`](decorators.md#tool),
[schema mapping](../mcp-skills.md#schema-and-call-mapping),
[shared security](../mcp-skills.md#shared-execution-and-security).

## `SkillRouter`

**Purpose / decision:** select an eligible skill and extract arguments for
free-text A2A requests; no author-written mandatory handler.
Support custom routers and a built-in opt-in model-based router.
Activated structured requests select deterministically and do not call the router.

**Signature status:** injected interface intended; constructor, method names,
request/decision types, and clarification contract remain open.

| Input/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Free-text request | Native A2A message/content; exact interface open | Needed for prompt selection and validated argument extraction | Descriptive skills alone are not dispatch. Unknown/invalid decisions fail; ambiguity/missing input asks back via `TASK_STATE_INPUT_REQUIRED`. Clarification schema/follow-up handling remain open. |
| Eligible candidates and request budget | Exact interface open | Required constraints for applicable selection | Restrict candidates by caller policy; budget model routing before invocation and propagate remaining allowance. |

```python
from agentframe import SkillRouter


def describe_router(router: SkillRouter) -> str:
    return type(router).__name__
```

Supply a custom or explicitly configured built-in model implementation as
`AgentApp(router=...)`. Built-in model routing is a capability decision; its
public class/factory and any `route()` signature remain open.

**Behavior / failures:** deterministic or explicitly model-backed; injecting
a model into an agent does not enable model routing, tools, or multi-skill
orchestration. Ambiguous/missing prompt input produces an input-required task
and question, never a guess. Unknown skills/invalid extracted arguments remain
explicit failures unless a supported missing-input clarification applies.
Authorize admission before sensitive routing, apply outbound model checks,
then re-check selected skill/resource authorization. A router cannot grant
permissions. Preferred structured skill selection uses the separately declared
and activated A2A extension; local `skill=` is not a standard A2A wire field.
Structured failure must not silently downgrade into routing or re-inference.

**Related:** [`Runtime.invoke`](runtime.md#runtimeinvoke),
[routing design](../annotations.md#skill-registration-versus-request-routing).

## Built-in model-based router (planned implementation)

**Purpose / decision:** first-release library-supplied opt-in selection for
free-text multi-skill A2A, alongside custom `SkillRouter` implementations.
M6 follows M3/M4; injecting a model into an agent never activates it.
Structured extension requests bypass this router and its model-selection cost.

**Signature status:** public implementation name, constructor/factory, selected
model configuration, selection/argument-extraction decisions and clarification
interface remain open. No new class export or constructor fields are settled.

| Concern | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| Model routing opt-in | Explicit router configuration; syntax open | Disabled without explicit selection | Does not follow from `Agent(model=...)`; custom routers remain supported. |
| Model and eligible skills | Normalized model dependency and authorized candidate contracts; open | Needed for model selection | Apply outbound checks and request-wide budget before inference; cannot invent skills or grant permission. |
| Selected skill/business arguments | Typed decision representation; open | Must be validated before invocation | Unknown/invalid decisions fail explicitly; ambiguity/missing arguments asks back through `TASK_STATE_INPUT_REQUIRED`. Exact clarification contract remains open. |

This example accepts an **already explicitly configured library model router**
through the proposed interface. It does not invent its constructor or make the
agent's model double as router configuration.

```python
from agentframe import (
    Agent, AgentApp, Authenticator, AuthorizationPolicy, HostConfig, Runtime, SkillRouter,
)


def build_routed_app(
    agent: Agent, model_router: SkillRouter,
    development_auth: Authenticator,
    development_authorization_policy: AuthorizationPolicy,
) -> AgentApp:
    return AgentApp(
        agent=agent,
        router=model_router,
        runtime=Runtime(authorization=development_authorization_policy),
        host=HostConfig(
            public_url="http://127.0.0.1:8000",
            auth=development_auth,
        ),
    )
```

The application chooses a real authenticator and supplies resource policy.
`APIKeyAuth` is suitable for a development example; `AllowAnonymous()` would
be a deliberate access choice, not an omitted configuration.

**Behavior / failures:** authorize admission/restrict candidates, budget/check
the selection model, validate extraction and re-check skill/resource policy
before execution. Exhaustion, refusal, invalid decisions, ambiguity and policy
failures use the decided typed errors; exact protocol/clarification details
remain open. Never enable
automatic multi-agent orchestration, implicit tools or caller authority.
Input-required clarification is not completed output, human approval workflow
or durable execution resumption.

**Related:** [`SkillRouter`](#skillrouter),
[routing and extraction](../annotations.md#skill-registration-versus-request-routing),
[remaining execution contracts](../base-classes.md#decided-execution-contracts-and-remaining-details).

## Native A2A and MCP types

Reuse official A2A `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and
`Artifact`, task stores, and request-handling components where possible.
Reuse native MCP tool results and SDK protocol handling. These are **not new
AgentFrame classes** or separately maintained wire schemas.

A2A protocol 1.0 and the official `a2a-sdk` are selected (1.3.0 observed on
2026-11-11; specification release v1.0.1). Exact SDK pins, native imports and
transport binding must be proven at implementation; no compatibility testing
has occurred. MCP version selection is separate. Constructor signatures/imports
are deliberately not copied as stable AgentFrame APIs. See
[resolved platform/API choices](../initial-outline.md#resolved-m0-platform-and-api-decisions).
No fabricated
MCP/A2A payload fields, REST envelopes, or unverified capabilities are part of
this reference.

**Related:** [native type boundary](../base-classes.md#native-a2a-types),
[delivery milestones](../initial-outline.md#delivery-milestones).
