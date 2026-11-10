# AgentFrame repository context

## Purpose and current status

AgentFrame is an independent Python library for annotation-based agent creation:
classes implement behavior, decorators describe agents and skills, and types
define validated contracts. A2A and native Agent Cards are first-class.
The eventual agentic platform is a direction, not the first deliverable.

This repository currently contains design documentation only. There is no
implemented SDK, published package, stable API, or test/build command. Keep
proposed API examples labeled as proposals. Do not scaffold implementation or
add dependencies merely to make design snippets executable.

## Confirmed dependency boundary

Do not build AgentFrame on a major agent framework. In particular, do not add
LangChain, LangGraph, Microsoft Agent Framework, Pydantic AI, Google ADK, CrewAI,
AutoGen, or Semantic Kernel as dependencies, including through integration
packages. AgentFrame may compete with these projects; it must own its authoring
and execution abstractions instead of wrapping theirs.

This is not a ban on useful dependencies. The official A2A SDK, Pydantic
validation, ASGI/HTTP libraries, credential libraries, and official model-provider
clients are appropriate when needed. Pydantic is not Pydantic AI. Keep provider
dependencies optional and reuse protocol types instead of copying schemas.

## Authoring decisions

- Use `@agent` for identity and `@skill` for advertised skills.
- Implement behavior in an `Agent` subclass; the common path needs no additional
  AgentFrame subclass.
- Use Python types, Pydantic data models, and `Annotated`/`Field` for contracts.
- Use `@tool` plus explicit registration for optional model-callable functions.
  Tools and A2A skills are different concepts.
- Put instructions and execution settings in typed configuration, with an
  optional trusted callable for dynamic instructions.
- Inject models, state stores, policies, retrieval dependencies, and hooks.
  Custom components may implement structural protocols without inheritance.
- Keep `RunContext` library-managed and invocation-scoped.
- Supply default bounded execution in AgentFrame, not another framework.
- Generate native Agent Cards from metadata plus actual host configuration.
- Compose A2A hosting through `AgentApp`; do not require server subclassing.

The complete piece-to-code decision table and examples live in
`docs/base-classes.md`. Exact signatures, concrete limits, and extension
interfaces remain proposals until proven by implementation.

## A2A and model boundaries

An A2A skill is descriptive metadata, not a Python-method RPC endpoint. Messages,
tasks, artifacts, and actual endpoint capabilities follow the selected official
protocol/SDK version. A card's security declaration does not enforce security.
Do not advertise features the host does not implement.

Initial models are GPT deployments through Microsoft Foundry inference and
locally executed Llama through Ollama. Llama is a model family, not a host.
Foundry inference is not Foundry-hosted agent deployment. Keep provider imports
out of agent business logic. Switching providers should change configuration,
not the handler.

## Safety and scope

Use explicit model selection, finite execution limits, validated inputs/outputs,
isolated run state, cancellation propagation, redacted operational logs, and
explicit failures. No implicit downloads, cloud fallback, tool grants, server
startup, or inference at import time. Runtime overrides must not weaken host
security or exceed its resource ceilings.

First prove a model-free agent, then independent A2A interoperability, then
interchangeable Foundry/Ollama models. The authoring goal is approximately 25
lines for a text agent, excluding host and credential setup.

Do not expand the initial scope into UI, orchestration platforms, durable
workflows, federation, RAG infrastructure, evaluation platforms, or cloud-agent
deployment unless explicitly requested.

## Documentation maintenance

Read `README.md` and `docs/README.md` first. Keep `docs/initial-outline.md`,
`docs/annotations.md`, and `docs/base-classes.md` consistent. Preserve the
distinction between confirmed product direction and illustrative API details.

When implementation starts, select compatible Python/dependency versions and
add focused validation. Until then, do not invent build/test instructions or
claim that documentation snippets run against an existing AgentFrame package.
