# AgentFrame

**Define agents with Python. Communicate through A2A. Choose your model host.**

AgentFrame is a proposed Python library for creating agents through classes,
decorators, and type annotations. Agent-to-Agent (A2A) interoperability and
Agent Cards are core contracts, not optional exports.
The goal is a default authoring path for building one to many agents:
independently deployable specialists and orchestrator agents that cooperate
over A2A when needed, including with other frameworks' A2A agents.

AgentFrame is an independent agent framework, not a wrapper around LangChain,
LangGraph, Microsoft Agent Framework, Pydantic AI, Google ADK, CrewAI, or another
major agent package. It owns its authoring API and execution behavior while
reusing protocol SDKs, validation libraries, and model-provider clients.

## Status

This repository currently contains initial design documentation only. There is
no implemented SDK, installable release, or supported public API yet. Reviewed
capability and contract decisions are recorded below and in the reference.
Approved names/defaults are design decisions; unspecified interfaces remain
open. No example implies an installed SDK or tested compatibility.

## Goals

- Make a small agent easy to define with explicit Python declarations.
- Generate native A2A Agent Cards from agent and skill metadata plus host configuration.
- Support model-free agents and optional model-assisted behavior.
- Start with Microsoft Foundry model inference and local Llama through Ollama.
- Allow model selection to change without rewriting agent behavior.
- Provide safe, validated defaults with typed configuration and dependency injection.
- Build on the official A2A Python SDK instead of maintaining another wire format.
- Build and deploy 1..N agents; compose specialists through ordinary orchestrator
  skills using interoperable A2A calls, not a required workflow/graph engine.

## Proposed authoring experience

This is design syntax, not an implemented API. Decorators describe the agent;
an `Agent` subclass implements behavior. Models and infrastructure are injected.
Each ability is a separate `@skill` method; no abstract `handle()` or manual
dispatch method is required.

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Echo", description="Returns supplied text.", version="0.1.0")
class Echo(Agent):
    @skill(id="echo", name="Echo text", description="Return text.", tags=["text"])
    async def echo(self, text: str, ctx: RunContext) -> str:
        return text
```

The normal path needs only one AgentFrame subclass, `Agent`. Model-free agents
need no provider configuration. See [agents with and without models](docs/agent-examples.md)
for multi-skill, mixed, and local invocation examples, and the
[API design examples](docs/base-classes.md#proposed-api-examples) for host
composition and advanced extension points.

Llama is a model family; Ollama is the initial local model host. Foundry model
inference is distinct from deploying an agent to Foundry Agent Service.

## First success criterion

M1 is the first proof: model-free authoring and managed `Runtime.invoke`.
M2 adds native A2A hosting with custom routing only. M3 meets this success
criterion:

Define one text agent in approximately 25 lines of agent-authoring code, generate
and publish a valid Agent Card, invoke it with an independent official A2A client,
and switch between a Foundry GPT deployment and local Llama by changing only model
configuration. Host bootstrap and credential setup are documented separately.

The same public contract must work with both hosts; identical generated answers
are not required. A deterministic model-free example must work without cloud
credentials, model downloads, or an inference server.

## Initial scope

The first usable release (M0-M7) focuses on a single agent: authoring, native
A2A hosting, model adapters, text generation, typed structured outputs, and bounded execution of
explicitly registered tools with generation continuations. It returns complete
validated results, not streamed partial output. Multi-skill A2A supports a
built-in opt-in model-based router plus custom routers; injecting a model alone
never enables model routing. Router construction/extraction contracts remain open;
the generation calls are decided: `ctx.generate(text)` for text,
`ctx.generate(text, output=Summary)` for validated Pydantic output and
`ctx.generate(text, tools=[registered_tool])` for bounded continuations.
One server/shared runtime serves audience-specific endpoints, not per-skill
routes: A2A for agents, optional MCP for LLM apps/tools and AG-UI later for
frontends. Prefer structured A2A skill/JSON-argument requests through a declared,
activated extension; free text uses routing and input-required clarification.
Structured selection bypasses routing, not authorization or execution controls.
See the [request-style design and open extension contract](docs/mcp-skills.md#a2a-request-styles-and-extension-boundary).

The [delivery plan](docs/initial-outline.md#delivery-milestones) splits this work
into independently releasable/testable milestones: M0 development setup, M1
model-free execution, M2 A2A/custom routing, M3 interchangeable text models,
M4 structured outputs, M5 bounded tools, M6 built-in opt-in model routing
(after M3/M4), and M7 hardening to the first usable release.

The application owns human approval workflows; the first release has no built-in
approval pause/resume. Tool authorization/action checks remain mandatory.
Protocol task support is not a resumable human workflow. The current core does
not include a UI, durable workflow/graph engine, hosting/deployment service,
federation, RAG system, evaluation platform, or Foundry-hosted agent deployment.
Revisit durable workflows after the multi-agent release based on real usage;
no design now. If added, use an optional separate package (candidate
`agentframe-workflows`). Long work uses A2A tasks with an in-memory default
and pluggable persistent store, not automatic checkpoint/recovery or approvals.

Token budgets and deadlines are runtime-enforced configuration. Tracing
integration and MCP are separate optional follow-ups after M7. OpenTelemetry
content capture is off by default; MCP publishes explicitly allowlisted skills,
not every skill or internal tool. A custom REST skill API is deferred.

Skills may declare required permissions; application roles map to grants outside
the agent definition. The proposed runtime enforces verified identity and
application resource policy across entry points. Prompt safeguarding combines
library trust boundaries with application checks, not a guaranteed injection filter.

The [runtime contracts](docs/api/runtime.md) distinguish `invoke` (validated
value) from `run` (`RunResult` with local metadata even without tracing).
Use [annotated `Inject[T]` and `Services`](docs/api/agents.md#injectt) for service
dependencies, validated at startup and excluded from caller schemas.
[Decided defaults](docs/limits.md) apply within independent host ceilings;
remote hosts require an explicit [authenticator](docs/api/authorization.md#authenticator),
never an implicit anonymous mode. Safe typed errors carry invocation correlation;
transient model retries and A2A deduplication never promise exactly-once actions.

**The next core release (M8+) is multi-agent A2A composition**, not an optional
discovery add-on. Read-first registries and a managed directory provide validated
references, native card resolution, five-minute caching and stale refresh.
Skills receive `Remote["name"]` bindings: `call("skill", ..., output=Type)`
uses structured A2A, while `ask(text)` uses plain A2A text. Both await completion;
input-required raises `ClarificationNeededError`, never automatic back-and-forth.
Orchestrators are ordinary AgentFrame
agents whose skills call specialists, not a workflow/graph engine. The release
must carry identity/delegation, permissions, deadlines, remaining token allowance
and trace/correlation across hops without amplifying authority or overriding
remote services' limits. Per-remote Entra OBO or explicit service identity,
remaining deadline/token ceilings and default maximum 5 hops constrain calls.
No orchestration helpers in the first multi-agent release: use plain Python
`await`, `asyncio.TaskGroup` and `directory.find(skill=... / tags=...)`.
Typed `Remote[ResearchAgent]` proxies are a later option, not proprietary RPC.

M2 adds `agentframe run module:Class`; M8+ adds `agentframe dev` reading
`agentframe.yaml`, local registry/development API-key wiring and tagged combined
logs. Package one container per agent, with a Dockerfile template and generated
Compose example; Foundry/Kubernetes templates are later. See the
[development plan](docs/development.md). AgentFrame does not become a
hosting platform. Discovery itself does not deploy agents, grant authority or
invoke them automatically. Foundry registry discovery uses the chosen projects
agents API (`azure-ai-projects` 2.x); A2A-enabled filtering, card mapping,
auth/RBAC and pagination still need implementation proof. See
[discovery design](docs/discovery.md) and the [reference](docs/api/discovery.md).

Streaming is a separate follow-up after M7, planned to support **AG-UI** as an
optional agent-to-frontend
protocol adapter, distinct from A2A agent-to-agent and MCP tool interfaces.
A2A/cards stay first-class; no UI or major agent-framework dependency is implied.
`AgentApp(ag_ui=AGUIConfig(enabled=True))` will serve `/ag-ui` with shared
auth/limits/permissions: lifecycle, text deltas, tool names/status and safe errors.
Whole-answer checks send complete text at end; disconnect cancels, with no
first-version reconnect/resume. Frontend tools/shared state are later opt-in.
The [streaming proposal](docs/streaming.md) scopes event mapping, trust and
output-policy boundaries without claiming implemented AG-UI conformance or
automatically advertising A2A streaming.

The multi-agent release follows the single-agent M7 release. A larger hosted
platform or durable workflow engine is not a prerequisite for either.

## Documentation

- [Documentation index](docs/README.md)
- [Proposed API reference: public surfaces, parameters, and open contracts](docs/api/README.md)
- [Decided local results and safe error contracts](docs/api/runtime.md)
- [Service injection and lifecycle](docs/api/agents.md#injectt)
- [Initial outline and delivery milestones](docs/initial-outline.md)
- [Decorators and type annotations](docs/annotations.md)
- [Base classes and interfaces](docs/base-classes.md)
- [Agent examples: model-free, model-backed, and mixed](docs/agent-examples.md)
- [Read-first registries and managed agent discovery](docs/discovery.md)
- [Local CLI development and container packaging plan](docs/development.md)
- [Future streaming and the optional AG-UI adapter](docs/streaming.md)
- [Execution limits and token accounting](docs/limits.md)
- [Tracing and correlation](docs/tracing.md)
- [A2A hosting and optional MCP skill configuration](docs/mcp-skills.md)
- [Skill authorization and caller identity](docs/authorization.md)
- [Prompt safeguarding and shared responsibilities](docs/prompt-safeguarding.md)
- [Contributor and coding-agent context](AGENTS.md)

## Development and contributions

The project is in API-design discovery. Proposals should connect to the first
success criterion and identify interoperability, safety, and authoring tradeoffs.
There are no build or test commands until the Python package is introduced.
Human readability is a top-priority contribution rule: prefer simple,
maintainable code over cleverness or speculative abstractions. M0 will use
Ruff for formatting/linting (88-character lines), strict mypy on `agentframe`
and pytest, enforced through CI and pre-commit. See
[code quality and readability](AGENTS.md#code-quality-and-readability) for the
agent/contributor rules; configuration and exact pins come with M0 implementation.
The [approved platform/API decisions](docs/initial-outline.md#resolved-m0-platform-and-api-decisions)
(2026-11-11) are Python **3.13+**, testing through **3.15** (Python has no LTS);
A2A protocol **1.0** through official `a2a-sdk` (1.3.0 observed, specification
release v1.0.1); and Foundry **OpenAI v1 Responses API** through optional
`openai`, with injected Entra token-provider (`azure-identity`) or API-key
authentication. Do not use the retired Azure AI Inference SDK.
Foundry registry reads use the project agents API through `azure-ai-projects`
2.x (2.10.0 observed), separately from model inference and still in M8+.
These are decisions, not compatibility-test results or dependency pins.
M0 still must configure the selected tooling, pin compatible tool/SDK/dependency
versions and establish the Python test matrix; concrete A2A transport binding
and provider mappings remain implementation work.

The intended repository and distribution name is `agentframe`; the intended
Python import is `agentframe`. Package-index names are not reserved by creating
this repository, and no package availability or trademark clearance is guaranteed.

## References

- [A2A specification](https://a2a-protocol.org/latest/specification/)
- [Selected A2A specification release v1.0.1](https://github.com/a2aproject/A2A/releases/tag/v1.0.1)
- [Official A2A Python SDK](https://github.com/a2aproject/a2a-python)
- [Observed official Python SDK 1.3.0](https://pypi.org/project/a2a-sdk/1.3.0/)
- [Python release-cycle data](https://peps.python.org/api/release-cycle.json)
- [Ollama Python client](https://github.com/ollama/ollama-python)
- [Microsoft Foundry documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)
- [Foundry OpenAI SDK migration and Responses API](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration)
- [Foundry incoming A2A and authenticated cards](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
- [Foundry outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)

## License

Licensed under the [Apache License 2.0](LICENSE), a permissive open-source license
that includes an explicit patent grant subject to its terms.
