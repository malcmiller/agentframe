# Base classes and interfaces

These are proposed responsibilities, not stable class signatures. Start with
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
| Tools | Annotation-based with explicit registration | `@tool(...)` defines typed callables; registration and authorization remain separate. |
| Execution loop | Library default with a replaceable interface | AgentFrame owns bounded execution; advanced executors are a later extension point. |
| Run context | Library-managed class | A fresh `RunContext` supplies per-invocation dependencies and control signals. Do not subclass it to store application state. |
| Conversation state | Injected interface implementation | A future `StateStore` protocol isolates conversation state. It is not the A2A task store. |
| Knowledge / retrieval | Injected interface or annotated tool | Application-directed retrieval uses a dependency; model-directed retrieval uses an explicitly registered tool. |
| Execution policy | Typed configuration and policy interface | Limits and runtime checks enforce policy; metadata may declare requirements but cannot enforce them. |
| Authorization | Skill permission annotations and an injected policy | `requires_permissions` declares all-of grants; verified identity and resource policy govern execution. Roles remain application-owned. |
| Failure handling | Library defaults and exception subclasses | Typed failures and explicit protocol mappings; no required error decorator. |
| Observability | Configuration and injected hooks | Correlated operational logs and optional tracing without changing agent inheritance. |
| Agent Card | Generated from annotations and host configuration | Native SDK type with validated customization, not a required custom subclass. |
| A2A host | Configured composition | `AgentApp` assembles an agent, host configuration, and SDK components. |

These are design responsibilities, not a requirement to implement every
extension in the first release. One `Agent` subclass is enough for the normal
authoring path. Pydantic schema subclasses are data definitions, not another
layer of agent inheritance.

## Agent authoring and execution

| Type | Definition |
| --- | --- |
| `Agent` | Base class for immutable authoring configuration and one or more valid decorated skill methods, including inherited skills. Supports model-free, model-backed, and mixed skills with no required abstract `handle()`. |
| `RunContext` | Per-invocation model access, `principal`, application dependencies, deadline, cancellation, and correlation. Provides helpers such as `generate`; never reused as mutable global state. |
| `Principal` | Immutable trusted caller identity and verified grants/claims, supplied by authentication or trusted local application context, not skill arguments. Exact fields remain open. |
| `AuthorizationPolicy` | Injected policy for verified caller grants and resource-specific rules. Adds restrictions to mandatory skill requirements and fails closed on denial or unavailability. |
| `ExecutionPolicy` | Optional configured input, outbound-model, tool/action, and output checks. Does not replace authorization or guarantee injection prevention. |
| `Runtime` | Registers skill IDs and runs explicitly selected local skills through validation, policy, context creation, deadlines, and output validation. Does not require inference. |
| `SkillRouter` | Proposed injected interface for selecting a skill from a remote request. Deterministic or explicitly model-backed; exact signature remains open. Selection is subject to authorization and never grants execution permission by itself. |
| `AgentConfig` | Validated defaults for instructions and execution limits. A default is distinct from a non-overridable host policy ceiling. |
| `RunLimits` | Validated invocation budgets, per-model-request input bound, inference-attempt count, and deadline. Also used to express host ceilings separately from defaults. |
| `TracingConfig` | Optional runtime tracing with an application-owned OpenTelemetry provider and content capture off by default. Never owns token enforcement. |
| `AgentApp` | ASGI composition for native A2A and optional MCP adapters over the common runtime. Publishes the native card and coordinates task lifecycle and owned resources through protocol SDKs. |
| `HostConfig` | Advertised endpoint, authentication integration, and non-overridable execution ceilings. Separate from agent defaults and adapter-specific settings. |
| `A2AConfig` | A2A path, selected binding, task-store integration, and actual supported endpoint features. A2A remains the default interface. |
| `MCPConfig` | Optional MCP endpoint and an explicit `exposed_skills` allowlist. Does not publish internal model tools or grant caller authorization. |

`Runtime(authorization=..., policy=...)` separates required permission/resource
decisions from supplementary content/action checks. Missing integration cannot
permit protected execution. Both apply at their relevant execution boundaries;
an earlier model or prompt-filter decision cannot authorize a later action.
See [authorization](authorization.md) and [prompt safeguarding](prompt-safeguarding.md)
for API examples and application responsibilities.

Each skill accepts `ctx: RunContext`, including model-free skills; unused context
is valid. It belongs to the invocation, not exclusively to model inference.
`ctx.generate(text, instructions=...)` uses the injected model, shared agent
instructions, and trusted call-specific instructions, with user text kept separate.
Missing model configuration produces an explicit error on generation, not a
failure to construct or run a model-free agent.

`AgentApp` owns the A2A entry point and uses an explicit routing strategy for
multiple skills, then invokes the selected skill through the common runtime.
One matching skill can be selected directly; ambiguous requests need clarification
or an explicit error. An internal Python skill method name must not become an
undocumented remote RPC method. Local skill-ID invocation is not a standard A2A
operation. See [routing boundaries](annotations.md#skill-registration-versus-request-routing).
Task state transitions and cancellation must follow the selected A2A version.
Disconnecting a stream is not automatically equivalent to canceling a task.

## Model interfaces

| Type | Definition |
| --- | --- |
| `Model` | A Python `Protocol` for asynchronous generation through normalized requests and responses. Does not require inheritance from a vendor-specific class. |
| `FoundryModel` | Adapter for a configured Foundry model deployment and an explicitly supported inference API. Accepts injected credentials or a configured SDK client. |
| `OllamaModel` | Adapter for an explicit Ollama endpoint and model tag, initially targeting locally executed Llama models. Never downloads or switches models implicitly. |
| `ModelSettings` | Validated shared generation settings such as per-request `max_output_tokens`, with typed provider-specific options. Unsupported requested settings fail explicitly. |
| `ModelCapabilities` | Supported features of the actual model/deployment, including streaming, tools, structured output, and accepted modalities. Unknown support is not assumed to be available. |
| `ModelRequest` | Provider-neutral messages, optional tool definitions, output contract, and permitted generation settings. Keeps instructions distinct from user content. |
| `ModelResponse` | Normalized generated content, optional tool calls, available usage data, and completion status. Missing usage remains unknown, not zero. |

The minimal contract needs non-streaming text generation first. Streaming events
and richer content should be introduced only with corresponding adapter tests.
There is no claim that every model host supports the same features or produces
identical results.

Resource ownership must be explicit: library-created clients are closed by the
library; injected clients remain caller-owned unless ownership is explicitly
transferred. Clients and credential providers must not be constructed at module
import time.

## Native A2A types

Reuse `AgentCard`, `AgentSkill`, `Message`, `Part`, `Task`, and `Artifact` from
the selected official A2A SDK release. Reuse SDK task-store interfaces and
request-handling components where possible.

AgentFrame should not create competing wire models, rename protocol states, or
copy SDK schemas into a separately maintained contract.

## Errors and extension points

Distinguish definition/configuration errors, input/output validation failures,
authentication/authorization failures, unsupported model features, provider
failures, deadline expiration, and cancellation. Exact exception names remain
an implementation decision.

Keep detailed diagnostics in redacted, correlated application logs. Return safe
protocol errors to remote callers, not stack traces, credentials, or raw provider
responses.

Allow explicit injection of model implementations, provider clients, credential
providers, supported task stores, and authentication integration. Avoid a
global plugin registry or custom configuration language in the first release.

## Proposed API examples

All snippets are illustrative, unimplemented API proposals. Constructor names,
module exports, and field names are not stable. The `Summarizer` class below is
the example in [annotations](annotations.md#proposed-authoring-shape).
For complete multi-skill definitions and local invocation, see
[agents with and without models](agent-examples.md).

### Inject a local model

```python
from agentframe import OllamaModel

local_model = OllamaModel(
    model="llama3.2:3b",
    endpoint="http://localhost:11434",
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
dependencies; the final accepted credential types depend on the selected client.
No secrets belong in the definition or generated card.

### Configure defaults and compose the host

```python
from agentframe import AgentApp, AgentConfig, HostConfig, RunLimits

configured_agent = Summarizer(
    model=local_model,
    config=AgentConfig(limits=RunLimits(timeout_seconds=30)),
)
app = AgentApp(
    agent=configured_agent,
    host=HostConfig(public_url="http://127.0.0.1:8000"),
)
```

This illustrates a local ASGI application, not a running server. The ASGI server
must separately bind to loopback on the matching port. Thirty seconds is an
example override, not a settled library default. Non-local hosting needs explicit
authentication and HTTPS; a public URL does not configure either automatically.
Instance configuration overrides class defaults, and permitted run overrides
remain subject to host-enforced ceilings.

See [limits](limits.md), [tracing](tracing.md), and
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
