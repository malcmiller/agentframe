# Base classes and interfaces

These are design contracts, not implemented or stable class signatures. Names
and defaults explicitly marked decided in the reference are approved. Start with
the smallest working surface; add supporting types when implementation needs
them, rather than scaffolding an entire platform in advance.

AgentFrame owns these abstractions independently of major agent frameworks.
Provider adapters use protocol/HTTP clients or vendor SDKs, not another agent
framework's agent, executor, or model abstraction.

## Authoring and extension decisions

Here, annotation-based includes decorators and Python type annotations.
Configuration and composition are intentional alternatives to inheritance.

| Piece | Recommended mechanism | Responsibility |
| --- | --- | --- |
| Identity | Annotation-based | `@agent(...)` supplies descriptive metadata, not executable overrides. |
| Skills | Annotation-based | Each `@skill(...)` method registers an implementation and advertised metadata without creating a per-method A2A RPC endpoint. |
| Behavior / entry point | Subclass-derived skill methods | An `Agent` subclass implements one or more async skills, not a required `handle()`. The runtime owns the protocol entry point. |
| Input/output contracts | Type annotations and optional data-model subclasses | Built-in types or Pydantic `BaseModel` subclasses validate data. |
| Instructions | Configuration or typed callable | Static instructions by default; trusted dynamic construction when needed. No required subclass. |
| Model dependency | Injected interface implementation | Supplied adapters or a custom `Model` protocol implementation. |
| Tools | Annotation-based with explicit registration | `@tool(...)` defines typed callables for first-release bounded execution/continuations; registration and action authorization remain separate. |
| Execution loop | Library default with a replaceable interface | First release owns bounded tool execution/results and generation continuations; advanced executors remain later. |
| Run context and services | Library-managed context, annotated injection | `RunContext` supplies generation/control signals; `Inject[T]` resolves registered `Services`. Do not subclass context to store application state. |
| Conversation state | Injected interface implementation | A future `StateStore` protocol isolates conversation state. It is not the A2A task store. |
| Knowledge / retrieval | Injected interface or annotated tool | Application-directed retrieval uses a dependency; model-directed retrieval uses an explicitly registered tool. |
| Execution policy | Typed configuration and policy interface | Limits and runtime checks enforce policy; metadata may declare requirements but cannot enforce them. |
| Authorization | Skill permission annotations and an injected policy | `requires_permissions` declares all-of grants; verified identity and resource policy govern execution. Roles remain application-owned. |
| Failure handling | Library defaults and exception subclasses | Typed failures and explicit protocol mappings; no required error decorator. |
| Observability | Configuration and injected hooks | Correlated operational logs and optional tracing without changing agent inheritance. |
| Agent Card | Generated from annotations and host configuration | Native SDK type with validated customization, not a required custom subclass. |
| A2A host | Configured composition | `AgentApp` assembles an agent, host configuration, and SDK components. |
| Discovery sources (next core release) | Small abstract base and supplied adapters | Extend read-only `AgentRegistry`; validated references and supported queries do not require registration writes. |
| Card discovery lifecycle (next core release) | Library-managed composition | `AgentDirectory` combines registry/resolver/cache/refresh and owned-resource lifecycle; no per-backend user HTTP loops. |
| Remote invocation (M8+) | `Remote["name"]` skill annotation | Structured `call`, plain-text `ask`, bounded task waiting; underlying client distinct from discovery. No initial helper/graph engine. |
| Streaming (later) | Optional protocol adapter | Planned AG-UI frontend events, separate from A2A/MCP/OTel; no first-release streaming or UI dependency. |

These are design responsibilities, not a requirement to implement every
extension in the first release. One `Agent` subclass is enough for the normal
authoring path. Pydantic schema subclasses are data definitions, not another
layer of agent inheritance.

## Agent authoring and execution

| Type | Definition |
| --- | --- |
| `Agent` | Base class for immutable authoring configuration and one or more valid decorated skill methods, including inherited skills. Supports model-free, model-backed, and mixed skills with no required abstract `handle()`. |
| `RunContext` | Per-invocation model access, `principal`, deadline, cancellation and correlation. `generate` supports text, `output=Model` and registered `tools=[...]`. |
| `Inject[T]` / `Services` | Annotated skill dependencies, excluded from caller/card/MCP schema. `add` registers app-owned singletons; `add_scoped` creates/closes per-run instances. Missing registrations fail startup. |
| `Principal` | Immutable trusted caller identity and verified grants/claims, supplied by authentication or trusted local application context, not skill arguments. Exact fields remain open. |
| `AuthorizationPolicy` | Injected policy for verified caller grants and resource-specific rules. Adds restrictions to mandatory skill requirements and fails closed on denial or unavailability. |
| `ExecutionPolicy` | Optional configured input, outbound-model, tool/action, and output checks. Does not replace authorization or guarantee injection prevention. |
| `Runtime` | `invoke` returns a validated value; `run` returns `RunResult` with local execution metadata, even without tracing. `services=` supports local DI; no inference required. |
| `RunResult` | Value, invocation ID, token usage (complete/estimated; unknown never zero), model/tool calls, duration and finish reason. Ordinary A2A receives ID-only execution metadata; M8+ negotiated extension reports accounting usage, not internal results. |
| `SkillRouter` | Custom-router interface (implemented in M2: `route(conversation, skills) -> SkillCall \| Clarification`) plus a built-in opt-in model-based implementation (M6) for free-text A2A selection/extraction. Structured extension requests bypass it. Clarification leaves the task `INPUT_REQUIRED` for the owner to answer; selection never grants permission. |
| `AgentConfig` | Validated defaults for instructions and execution limits. A default is distinct from a non-overridable host policy ceiling. |
| `RunLimits` | Defaults: context-derived input cap, 100000 total tokens, 10 model attempts, 20 tool calls, 120 seconds. Host ceilings are separate constraints. |
| `TracingConfig` | Optional runtime tracing with an application-owned OpenTelemetry provider and content capture off by default. Never owns token enforcement. |
| `AgentApp` | ASGI composition for native A2A and optional MCP adapters over the common runtime. Publishes the native card and coordinates task lifecycle and owned resources through protocol SDKs. |
| `HostConfig` | Explicit `auth` choice, verified `role_permissions`, advertised endpoint and host ceilings; defaults 32 concurrent runs, 1 MiB request, 4 MiB response, 30-second shutdown grace. |
| `A2AConfig` | A2A path, selected binding, task-store integration, and actual supported endpoint features. A2A remains the default interface. |
| `MCPConfig` | Optional MCP endpoint and an explicit `exposed_skills` allowlist, empty by default. Does not publish internal model tools or grant caller authorization. |

`Runtime(authorization=..., policy=...)` separates required permission/resource
decisions from supplementary content/action checks. Missing integration cannot
permit protected execution. Both apply at their relevant execution boundaries;
an earlier model or prompt-filter decision cannot authorize a later action.
See [authorization](../guides/authorization.md) and [prompt safeguarding](prompt-safeguarding.md)
for API examples and application responsibilities.

Each skill accepts `ctx: RunContext`, including model-free skills; unused context
is valid. It belongs to the invocation, not exclusively to model inference.
`ctx.generate(text, instructions=...)` uses the injected model, shared agent
instructions, and trusted call-specific instructions, with user text kept separate.
Missing model configuration produces an explicit error on generation, not a
failure to construct or run a model-free agent.
`ctx.generate(text, output=Summary)` returns a validated Pydantic instance;
`ctx.generate(text, tools=[lookup_order])` runs only registered authorized tools
with continuations. Unsupported capability fails before sending; refusal and
truncation raise `ModelRefusalError` and `IncompleteOutputError`. A skill opts
into truncated text by catching `IncompleteOutputError` and reading
`.partial_text` (text only, M4 #54); its return value is still validated.

`AgentApp` composes one server/shared runtime with audience-specific endpoints,
not per-skill routes: A2A, optional MCP and later AG-UI. Preferred structured A2A
requests name a skill through a card-declared, caller-activated extension and
carry JSON arguments in a native data part. This bypasses routing/model-selection
cost, not the common runtime's validation, authorization, limits, accounting or
tracing. The extension URI/schema/advertisement details remain open; local
skill-ID invocation is not a base A2A wire operation.
Free-text requests use an explicitly configured custom or opt-in model router
for selection/extraction. Ambiguous/missing input asks back via an
input-required task; invalid/unknown structured selection fails explicitly,
never silently reroutes. See [routing boundaries](../guides/annotations.md#skill-registration-versus-request-routing).
Task state transitions and cancellation must follow the selected A2A version.
First release returns complete validated results; streaming is later.
Native task support does not imply application approval pause/resume.

## Model interfaces

| Type | Definition |
| --- | --- |
| `Model` | A Python `Protocol` for asynchronous generation through normalized requests and responses. Does not require inheritance from a vendor-specific class. |
| `FoundryModel` | Adapter for a configured Foundry GPT deployment through OpenAI v1 Responses and official `openai`. Credential/client injection and lifecycle remain proposed; provider packages stay optional. |
| `OllamaModel` | Adapter for an explicit Ollama endpoint and model tag, initially targeting locally executed Llama models. Never downloads or switches models implicitly. |
| `ModelSettings` | Canonical per-request `max_output_tokens=4096`, with typed provider options; not duplicated on `RunLimits`. |
| `ModelCapabilities` | Supported features of the actual model/deployment, including streaming, tools, structured output, and accepted modalities. Unknown support is not assumed to be available. |
| `ModelRequest` | Provider-neutral messages, optional tool definitions, output contract, and permitted generation settings. Keeps instructions distinct from user content. |
| `ModelResponse` | Normalized generated content, optional tool calls, available usage data, and completion status. Missing usage remains unknown, not zero. |

The first proof (M1) is model-free/text-only; M3 meets the README's
Foundry/Llama configuration-switching success criterion. The M7 first usable release includes
text, typed structured outputs and bounded explicitly registered tool calls,
validated results and generation continuations. All final results are complete
and validated. Unsupported model features fail explicitly; normalized messages,
tool calls/results, structured-output capability checks, usage, refusal and
incomplete-generation representations still need concrete contracts.
There is no claim that every deployment supports every feature or produces
identical results. Streaming is a later delivery with scoped optional
[AG-UI frontend support](streaming.md), not automatic A2A streaming.

Resource ownership must be explicit: library-created clients are closed by the
library; injected clients remain caller-owned unless ownership is explicitly
transferred. Clients and credential providers must not be constructed at module
import time.

## Discovery interfaces (next core release)

The next core multi-agent release (M8+), after the M7 single-agent release, adds
read-first discovery building blocks; see the [discovery design](discovery.md)
and its [API reference](../api/discovery.md). Native remote A2A calls let ordinary orchestrator
skills compose independently deployed agents, without a workflow/graph engine.

| Type | Definition |
| --- | --- |
| `AgentRegistry` | Small abstract base with proposed async `get(agent_id) -> AgentReference` and `list(query: AgentQuery) -> list[AgentReference]`; pagination remains open. No required write operations. |
| `AgentReference` | Validated stable identity, endpoint/discovery location, transport and non-secret connection metadata; exact fields open. |
| `AgentQuery` | Supported ID/skill/tag matching; skill/tag filtering requires source metadata or explicit card enrichment, not assumed reference fields. |
| `EnvironmentAgentRegistry` | `EnvironmentAgentRegistry("AGENTFRAME_AGENTS")`; payload encoding still open. |
| `FileAgentRegistry` | `FileAgentRegistry("agents.yaml")`, YAML name+URL list; detailed keys/schema/version open. |
| `FoundryAgentRegistry` | Chosen Foundry project agents API via `azure-ai-projects` 2.x; A2A-enabled filtering, card mapping, auth/RBAC and pagination remain open. No tested implementation; model deployments are not agent endpoints. |
| `AgentCardResolver` | Fetches/validates native SDK cards with application-configured outbound authentication and destination/redirect policy. |
| `CardCacheConfig` | Default TTL 300 seconds; refresh stale access automatically, fail on refresh error unless explicit `allow_stale=True`. No implicit background polling. |
| `AgentDirectory` | `sources=[...]`, native card-backed `find(skill=... / tags=...)`, cache/refresh and owned cleanup; `AgentApp(directory=...)` validates named remotes at startup. |
| `Remote["name"]` | Managed `call(skill, ..., output=Type, max_total_tokens=...)` and `ask(text)`; input-required raises `ClarificationNeededError`, no auto-conversation. |
| `RemoteAgentClient` | Underlying/explicit native A2A client; construction still open, author calls above decided. |

No import/construction I/O or implicit polling. Directory lifecycle is not
deploy/start/stop/delete, leases/federation, write registration or A2A task
management. Hosts may own a library-created/explicitly transferred directory's
lifespan; injection alone does not transfer ownership. Source errors are not
empty results/deletion; failed refresh cannot silently make stale current.
Freshness is not health, trust or authorization; invocation/actions authorize
again. See [failure/isolation boundaries](discovery.md#failure-trust-and-isolation-boundaries).
Per-remote OBO (Entra default) or explicit service identity never forwards the
incoming token or elevates on exchange failure. Shared deadlines/token allowance,
reported AgentFrame usage and maximum 5 hops use the same A2A extension (wire
schema still open). No first-release orchestration helpers; use Python TaskGroup.
See [remote budgets](../guides/limits.md#cross-agent-budgets-and-depth-m8) and
[development/packaging](../guides/development.md).

Later streaming uses `AGUIConfig(enabled=True)` on `AgentApp(ag_ui=...)` at
`/ag-ui`. Shared checks, lifecycle/text/tool-name-status/errors, whole-answer
buffering, disconnect cancellation and no reconnect/resume are decided; exact
event projection remains open. Durable workflows are revisited after M8+ real
usage and, if added, belong in an optional separate package.

## Native A2A types

Reuse `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and `Artifact` from
the selected official A2A SDK release. Reuse SDK task-store interfaces and
request-handling components where possible.

AgentFrame should not create competing wire models, rename protocol states, or
copy SDK schemas into a separately maintained contract.

## Errors and extension points

Distinguish definition/configuration errors, input/output validation failures,
authentication/authorization failures, unsupported model features, provider
failures, deadline expiration and cancellation using the
[decided error hierarchy](../api/runtime.md#agentframeerror). Base/subclass names
are approved; constructors, named limit subtypes and exact protocol mappings
remain open.

Keep detailed diagnostics in redacted, correlated application logs. Return safe
protocol errors to remote callers, not stack traces, credentials, or raw provider
responses.

Allow explicit injection of model implementations, provider clients, credential
providers, supported task stores, and authentication integration. Avoid a
global plugin registry or custom configuration language in the first release.
The later [read-only discovery registry](discovery.md) is a separate explicit
source dependency, not that global plugin mechanism.

## Proposed API examples

All snippets are illustrative, unimplemented API proposals. Constructor names,
module exports, and field names are not stable. The `Summarizer` class below is
the example in [annotations](../guides/annotations.md#proposed-authoring-shape).
For complete multi-skill definitions and local invocation, see
[agents with and without models](../guides/agent-examples.md).

### Inject a local model

```python
from agentframe import OllamaModel

local_model = OllamaModel(
    model="llama3.2:3b",
    endpoint="http://localhost:11434",
    context_window=8192,
)
local_agent = Summarizer(model=local_model)
```

The tag is illustrative and must already be installed. Construction performs no
download or inference; unsupported model features fail explicitly.

### Switch to Foundry without changing behavior

```python
from azure.core.credentials_async import AsyncTokenCredential
from agentframe import FoundryModel


def build_cloud_agent(
    endpoint: str, deployment: str, credential: AsyncTokenCredential
) -> Summarizer:
    model = FoundryModel(
        endpoint=endpoint,
        deployment=deployment,
        credential=credential,
    )
    return Summarizer(model=model)
```

The application supplies a supported endpoint, deployment, and credential. This
proposal uses an async Azure token credential from the optional provider
dependencies; mapping to the selected `openai` client is not settled.
The [chosen API path](../roadmap.md#resolved-m0-platform-and-api-decisions)
is OpenAI v1 Responses, with Entra token-provider or API-key authentication;
this is not the retired Azure AI Inference SDK. Final accepted AgentFrame
credential types and async wrapping remain open.
No secrets belong in the definition or generated card.

### Configure defaults and compose the host

```python
from agentframe import APIKeyAuth, AgentApp, AgentConfig, HostConfig, RunLimits, Runtime

configured_agent = Summarizer(
    model=local_model,
    config=AgentConfig(limits=RunLimits(timeout_seconds=30)),
)
app = AgentApp(
    agent=configured_agent,
    runtime=Runtime(authorization=development_authorization_policy),
    host=HostConfig(
        public_url="http://127.0.0.1:8000",
        auth=APIKeyAuth(keys=development_keys),
    ),
)
```

`development_keys` comes from secure application configuration (entry schema
open); `development_authorization_policy` is application resource policy.
`APIKeyAuth` is a decided development option, not a production security default.
This illustrates a local ASGI application, not a running server. The ASGI server
must separately bind to loopback on the matching port. Thirty seconds is an
example override of the 120-second default. Remote hosting requires explicit
authentication and HTTPS; a public URL does not configure either automatically.
Resolve library → agent → skill → per-run within host ceilings; skills
use `@skill(limits=RunLimits(...))`. Omitted `auth` refuses startup; anonymous
access requires explicit `AllowAnonymous()`.

See [limits](../guides/limits.md), [tracing](tracing.md), and
[MCP skill configuration](mcp-skills.md) for invocation budgets, optional
telemetry, explicit skill publication, and a combined server example.

### Implement an interface without subclassing an agent framework

```python
from agentframe import Model, ModelRequest, ModelResponse


class CustomModel:
    def __init__(self, client: Model) -> None:
        self.client = client

    async def generate(self, request: ModelRequest) -> ModelResponse:
        return await self.client.generate(request)
```

This structural delegation example wraps an injected model already using
AgentFrame's request/response types. A real vendor adapter must explicitly translate its
messages, output, errors, and feature support. Final client typing, capability
reporting, and lifecycle requirements must be specified before implementation.
No inheritance from `Agent`, or from another agent framework, is needed.

## Decided execution contracts and remaining details

Use `Inject[T]` parameters with `Services.add(T, singleton)` or
`Services.add_scoped(T, factory)`, supplied via `AgentApp(services=...)` or
`Runtime(services=...)`. Applications close singletons; AgentFrame closes only
scoped instances it created. Missing services fail startup; factory/disposal
method signatures remain open.

`Runtime.invoke` returns the value; `Runtime.run` returns `RunResult`.
The [safe error hierarchy](../api/runtime.md#agentframeerror) is decided, including
validation/auth/policy/model/limit/cancellation failures and correlation.
Startup ID allocation, usage marker representation, duration units and precise
transport mappings remain open.

Model retries cover transient rate-limit/timeout/server failures only, default
two retries with backoff; disable SDK retries. Side-effecting tools declare
`@tool(side_effects=True)` and never auto-retry; `idempotent=True` is not a
blanket replay grant. A2A deduplicates same caller/message ID for 10 minutes
in memory by default, with a pluggable multi-instance store, not exactly-once.
Retry/store configuration syntax, atomicity details, normalized usage layouts,
A2A typed-part mappings and router extraction/clarification remain open.
Applications own human
approval workflows; required authorization/action checks still fail closed.
No built-in first-release approval suspension/resumption or implicit replay.
