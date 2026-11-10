# Initial outline

## Product definition

AgentFrame makes creating agents declarative: a Python class defines behavior,
decorators describe the agent and its skills, and types define validated data
contracts. The library connects those definitions to native A2A interactions
and optionally to a model host.

The intended audience is developers building agents as components of an agentic
platform. The first deliverable is a focused library, not the whole platform.

## Independence and dependency boundaries

AgentFrame is intended as an independent alternative to existing agent
frameworks, not an authoring facade over one. Do not depend on major agent
packages such as LangChain, LangGraph, Microsoft Agent Framework, Pydantic AI,
Google ADK, CrewAI, AutoGen, or Semantic Kernel to define or execute agents.
Do not introduce such dependencies indirectly through an integration package.

AgentFrame owns agent definitions, validated invocation, execution limits,
model/tool coordination when introduced, and mapping between its execution
behavior and A2A. Reuse the official A2A SDK for protocol types and protocol
handling rather than reinventing the standard.

This is not a zero-dependency requirement. Standard-library utilities, Pydantic
for validation, ASGI/HTTP infrastructure, official model-provider clients, and
credential libraries are appropriate when justified. The official MCP Python SDK
and OpenTelemetry are optional protocol/observability dependencies, not agent
frameworks. Pydantic and Pydantic AI
are different packages: the former is permitted; the latter is an agent framework.
Keep provider-specific dependencies optional so model-free use remains lightweight.

## Authoring and extension model

Use decorators for descriptive metadata, Python types for data contracts, and
an `Agent` subclass for behavior. Use typed configuration for settings and
dependency injection for models and infrastructure. Do not force every piece
into a decorator or an inheritance hierarchy.

Each behavior is a separate async `@skill` method accepting runtime-injected
`ctx: RunContext`. An agent requires at least one valid skill, not an abstract
`handle()` method. Skills may be model-free, model-backed, or mixed in one class.
An injected model is optional and is only used when requested.

`Runtime.invoke` selects a stable skill ID explicitly for local calls. Remote A2A
requests enter through the host, with an explicit router for multi-skill agents.
Do not require authors to implement a large dispatch handler, infer an A2A
method-call protocol from skill metadata, or silently pick the first skill.
Model-assisted selection is opt-in and separate from model-assisted execution.

For explicit remote skill invocation, prefer an optional MCP adapter over a new
REST protocol. Publish only named, allowlisted skills as MCP tools; keep this
separate from internal model-callable `@tool` registration and caller authorization.
A2A stays first-class and MCP does not block the minimal initial slice.

The complete [piece-to-code mapping](base-classes.md#authoring-and-extension-decisions)
records which surfaces are annotation-based, subclass-derived, injected, or
library-managed. The normal authoring path needs only one AgentFrame subclass.
Custom components may implement structural protocols without inheriting a
framework base class.

## Architectural rules

1. Agent identity and advertised skills are explicit authoring metadata.
2. Agent Cards use official A2A SDK types and combine authoring metadata with
   truthful host capabilities, interfaces, and security declarations.
3. Agents receive messages and produce messages, tasks, or artifacts according
   to A2A semantics. A skill is not an automatically callable RPC method.
4. Python types validate local inputs and outputs. A Python signature does not
   create a standard A2A function schema or method-dispatch contract.
5. Model hosts are dependencies. Agent code does not import provider SDKs.
6. Agent instances do not hold mutable shared conversation or caller state.
7. Unsupported features and invalid configuration fail explicitly.
8. Defining or importing an agent never starts a server, downloads a model,
   provisions infrastructure, or performs inference.

## Initial adapters

| Adapter | Initial responsibility | Explicitly excluded |
| --- | --- | --- |
| Foundry | Call a configured GPT deployment through a selected supported inference API, with injected credentials and client lifecycle. | Hosted Agent Service deployment and automatic cloud provisioning. |
| Ollama | Call an explicit locally installed Llama model tag through an Ollama endpoint. | Model downloads, server management, and implicit cloud-model execution. |

The Foundry API path and supported deployment features must be documented and
tested when implemented. An adapter must not treat all Foundry endpoints as
interchangeable. Ollama support must distinguish genuinely local models from
cloud-routed model tags; localhost alone does not guarantee local inference.

## Configuration and safe defaults

- Resolve defaults in the order library, agent, then permitted per-run settings.
  Host security policy and resource ceilings are constraints, not overridable defaults.
- Require explicit model/deployment selection. Never silently switch hosts or models.
- Use loopback for local serving. Require intentional authentication and an
  explicit advertised URL for non-local hosting; use HTTPS outside local development.
- Do not infer the advertised endpoint from untrusted forwarded headers.
- Keep credentials, private prompts, and private tools out of public cards.
- Validate declared inputs and outputs, and reject unsupported required model features.
- Use finite request deadlines, bounded concurrency, output limits, and bounded
  model/tool iterations. Concrete limits must be chosen and tested before release.
- Propagate cancellation and deadlines through the adapter where supported.
  Do not promise that cancellation always prevents remote inference charges.
- Coordinate retries in one layer. Do not automatically replay side-effecting
  tools or multiply retries across SDK and runtime layers.
- Keep sampling settings model-aware rather than forcing universal temperature
  or reasoning parameters.
- Inject credentials rather than embedding them in source. Use an intentional
  production identity; broad development credential chains are not production policy.
- Log correlation and operational metadata by default, not prompts, outputs, or secrets.
- Treat in-memory task storage as ephemeral. Never claim restart durability.
- Isolate task and conversation access by verified caller identity when authentication
  is enabled. A client-supplied context ID is not authorization.
- Offer no shell, filesystem, network-fetching, or other tools by default.

## Identity, authorization, and prompt safeguarding

Skills declare all-of `requires_permissions` requirements rather than hardcoded
user roles. Applications map roles to grants and supply trusted authentication
and resource policy. The runtime exposes an immutable `Principal` through
`ctx.principal` and enforces authorization before executing protected work.
Untrusted arguments cannot set identity, roles, permissions, or runtime context.

Use an injected `AuthorizationPolicy` for application checks such as document
ownership, tenant boundaries, approval state, and permitted destinations.
Declared permission requirements remain mandatory; admission, discovery/routing,
skill invocation, and consequential actions need checks at the appropriate
boundaries. Anonymous remote access is explicit, never inferred from an empty
permission list or loopback binding. Missing identity/policy, denial, timeout,
or policy unavailability fails protected operations.

Prompt safeguarding is shared responsibility, not a universal model filter.
Keep trusted instructions separate from untrusted content, credentials outside
model context, and content out of default logs. Application-supplied
`ExecutionPolicy` checks cover input, each outbound model request (including
model routing), tools/actions, and outputs. Required checks fail closed.
External screening is explicit data egress, not an automatic safety fallback.

Validate model output but never treat it as authority. Preserve or reduce
permissions during delegation; service credentials do not expand caller grants.
Resource decisions must be enforced at the actual side-effect boundary, not
only at skill admission. See [authorization](authorization.md) and
[prompt safeguarding](prompt-safeguarding.md).

## Token budgets and tracing

Use `ModelSettings.max_output_tokens` for one model request and `RunLimits` for
assembled-input bounds, aggregate invocation tokens, model-attempt count, and
elapsed time. Model context capacity is a capability, not a configurable
guarantee. Host ceilings are independent of overridable defaults.

Preflight/reserve allowances and reconcile actual usage; label estimates and
unknown usage honestly. Strict accounting must reject calls without a reliable
bound. Do not promise a hard financial cap or cancellation without charges.
Budget any model-backed routing before skill execution and inherit remaining
allowances rather than reset them. See [limits](limits.md).

Use optional `TracingConfig` on `Runtime`, with an application-owned
OpenTelemetry provider. Sampling/export are application policy; content capture
is off by default. Keep correlation and accounting independent of tracing.
Do not leak credentials, prompts, outputs, or private reasoning; incoming trace
context is not identity. See [tracing](tracing.md).

## Delivery gates

| Gate | Deliverable | Acceptance |
| --- | --- | --- |
| 1 | Model-free agent authoring | A class with multiple decorated skills needs no `handle()`; metadata is deterministic; missing/duplicate skill definitions and invalid arguments fail explicitly; skill-ID invocations use isolated context and validated outputs without network access. |
| 2 | Native A2A interoperability | The agent publishes a valid card and accepts an independent official SDK client's message. Multi-skill routing is explicit, ambiguity is handled safely, and supported task retrieval/cancellation are verified; unimplemented optional capabilities are not advertised. |
| 3 | Interchangeable model hosts | One text-agent implementation runs against Foundry GPT and locally served Llama with only configuration changes. Mixed agents can run model-free skills without inference; generation without a model fails explicitly. Provider integration tests are explicit opt-ins. |
| 4 | Safe configuration and packaging | Configuration precedence, invalid outputs, timeouts, cancellation, isolation, verified identity, permission/resource denials, policy outages, no side effects on denial, and installed-wheel examples are covered. |

Streaming, tools, structured model output, and persistent task stores may be
added incrementally. They must not block a minimal text-only, model-free agent.
Each advertised feature needs an interoperability or behavioral test.

Optional MCP delivery follows the minimal A2A slice. Its gate includes
initialization, explicit tool discovery/invocation, unsupported schema rejection,
authorization, shared limits/tracing, and independent-client interoperability.
Tracing adds a separate opt-in check for safe metadata, correct span boundaries,
and accounting that still works with tracing disabled. These additions do not
reintroduce a UI, workflow platform, or custom REST skill API.
See the [combined hosting proposal](mcp-skills.md).

## Open design decisions

- Minimum supported Python version and dependency versions.
- Initial A2A binding and compatibility policy.
- Structured skill-input mappings and minimal-message versus task lifecycle behavior.
- Initial Foundry inference API and supported credential mechanisms.
- Concrete execution limits, error taxonomy, and configuration field names.
- Token counting/strict-enforcement capabilities and incomplete-usage semantics
  for each supported provider.
- Pinned MCP/telemetry versions, authorization hooks, schema/result mappings,
  and transport-specific correlation metadata.
- Principal construction and identity/delegation trust contracts, explicit
  anonymous-access syntax, resource-policy interfaces, and content-check decisions.
- Exact router interface, clarification behavior, and any negotiated remote
  skill-selection contract. Multiple skill methods and no required `handle()`
  are the agreed authoring direction, not open questions.

Resolve these with small executable examples before declaring a stable API.
