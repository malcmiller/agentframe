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
credential libraries are appropriate when justified. Pydantic and Pydantic AI
are different packages: the former is permitted; the latter is an agent framework.
Keep provider-specific dependencies optional so model-free use remains lightweight.

## Authoring and extension model

Use decorators for descriptive metadata, Python types for data contracts, and
an `Agent` subclass for behavior. Use typed configuration for settings and
dependency injection for models and infrastructure. Do not force every piece
into a decorator or an inheritance hierarchy.

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

## Delivery gates

| Gate | Deliverable | Acceptance |
| --- | --- | --- |
| 1 | Model-free agent authoring | A decorated class produces deterministic metadata; invalid definitions fail without network access; isolated local invocations return validated results. |
| 2 | Native A2A interoperability | The same agent publishes a valid card and accepts a message from an independent official SDK client. Supported task retrieval and cancellation are verified; unimplemented optional capabilities are not advertised. |
| 3 | Interchangeable model hosts | One text-agent implementation runs against Foundry GPT and locally served Llama with only configuration changes. Provider integration tests are explicit opt-ins. |
| 4 | Safe configuration and packaging | Configuration precedence, invalid outputs, timeouts, failures, cancellation, concurrent isolation, authentication boundaries, and installed-wheel examples are covered. |

Streaming, tools, structured model output, and persistent task stores may be
added incrementally. They must not block a minimal text-only, model-free agent.
Each advertised feature needs an interoperability or behavioral test.

## Open design decisions

- Minimum supported Python version and dependency versions.
- Initial A2A binding and compatibility policy.
- Exact agent handler signature and minimal-message versus task lifecycle behavior.
- Initial Foundry inference API and supported credential mechanisms.
- Concrete execution limits, error taxonomy, and configuration field names.
- How multiple declared skills inform the agent's message handling without
  inventing undocumented method routing.

Resolve these with small executable examples before declaring a stable API.
