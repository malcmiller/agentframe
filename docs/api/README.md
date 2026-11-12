# Proposed API reference

AgentFrame is a documentation-only proposal: there is no implemented SDK,
installable release, or stable import surface. This reference organizes the
current contracts for iteration; it does not turn illustrative signatures into
implemented release commitments. Explicitly approved names/defaults are design
decisions, not still-unresolved recommendations.

**Decision** identifies agreed behavior or a product boundary. **Proposed shape**
records syntax already illustrated in the design docs. **Open** identifies
details still needing implementation evidence. All import paths are proposed.
In signature blocks, `...` means unspecified, not a Python default value.
Only explicitly documented defaults should be treated as decisions; omitted
configuration must not be interpreted as unlimited or anonymous access.

Each entry covers purpose, signature status, parameters or conceptual fields,
an example, behavior/failures, and related rationale. Constructors are documented
under their class headings; known public methods have separate headings.
For interfaces without an agreed constructor or method signature, the reference
deliberately shows typed dependency use instead of inventing one.

## Public surfaces

| Surface | Entries |
| --- | --- |
| [Decorators and data contracts](decorators.md) | [`@agent`](decorators.md#agent), [`@skill`](decorators.md#skill), [`@tool`](decorators.md#tool); ordinary Python/Pydantic contracts. |
| [Agents, context and services](agents.md) | [`Agent`](agents.md#agent), [`RunContext`](agents.md#runcontext), [`generate`](agents.md#runcontextgenerate), [`Inject[T]`](agents.md#injectt), [`Services`](agents.md#services), [`add`](agents.md#servicesadd), [`add_scoped`](agents.md#servicesadd_scoped). |
| [Local runtime and results](runtime.md) | [`Runtime`](runtime.md#runtime), [`invoke`](runtime.md#runtimeinvoke), [`run`](runtime.md#runtimerun), [`RunResult`](runtime.md#runresult), future [`StateStore`](runtime.md#statestore-deferred). |
| [Safe errors](runtime.md#agentframeerror) | `AgentFrameError`, `DefinitionError`, validation/auth/policy errors, `ModelError` and capability/refusal/incomplete/provider subtypes, `LimitExceededError`, AgentFrame `CancelledError`; each has its own entry. |
| [Models](models.md) | [`Model`](models.md#model), [`Model.generate`](models.md#modelgenerate), [`FoundryModel`](models.md#foundrymodel), [`OllamaModel`](models.md#ollamamodel), [`ModelSettings`](models.md#modelsettings), [`ModelCapabilities`](models.md#modelcapabilities), [`ModelRequest`](models.md#modelrequest), [`ModelResponse`](models.md#modelresponse). |
| [Execution configuration](configuration.md) | [`AgentConfig`](configuration.md#agentconfig), [`RunLimits`](configuration.md#runlimits), [`TracingConfig`](configuration.md#tracingconfig). |
| [Identity, authorization and content policy](authorization.md) | [`Principal`](authorization.md#principal), [`AuthorizationPolicy`](authorization.md#authorizationpolicy), [`ExecutionPolicy`](authorization.md#executionpolicy), [`Authenticator`](authorization.md#authenticator), [`EntraIDAuth`](authorization.md#entraidauth), [`APIKeyAuth`](authorization.md#apikeyauth), [`AllowAnonymous`](authorization.md#allowanonymous). |
| [Hosting](hosting.md) | [`AgentApp`](hosting.md#agentapp), [`HostConfig`](hosting.md#hostconfig), [`A2AConfig`](hosting.md#a2aconfig), [`MCPConfig`](hosting.md#mcpconfig), [`SkillRouter`](hosting.md#skillrouter); native SDK types. |
| [Built-in model routing](hosting.md#built-in-model-based-router-planned-implementation) | Opt-in library implementation alongside custom routers; public implementation name/constructor still open. |
| [Future streaming](streaming.md) | Planned optional [AG-UI adapter](streaming.md#optional-ag-ui-adapter) and [runtime event boundary](streaming.md#runtime-streaming-boundary); no selected public class/stream method. |
| [Multi-agent release discovery and calls](discovery.md) | [`AgentRegistry`](discovery.md#agentregistry), [`get`](discovery.md#agentregistryget), [`list`](discovery.md#agentregistrylist), [`AgentReference`](discovery.md#agentreference), [`AgentQuery`](discovery.md#agentquery), [`EnvironmentAgentRegistry`](discovery.md#environmentagentregistry), [`FileAgentRegistry`](discovery.md#fileagentregistry), [`FoundryAgentRegistry`](discovery.md#foundryagentregistry), [`AgentCardResolver`](discovery.md#agentcardresolver), [`CardCacheConfig`](discovery.md#cardcacheconfig), [`AgentDirectory`](discovery.md#agentdirectory), [`get`](discovery.md#agentdirectoryget), [`refresh`](discovery.md#agentdirectoryrefresh), [`__aenter__`](discovery.md#agentdirectory__aenter__), [`__aexit__`](discovery.md#agentdirectory__aexit__), [`RemoteAgentClient`](discovery.md#remoteagentclient). |

AgentFrame owns its authoring and execution abstractions, not wrappers around a
major agent framework. Official A2A/MCP SDKs, Pydantic, HTTP/ASGI libraries,
credential libraries, provider clients, and optional OpenTelemetry are compatible
with that boundary. A future optional native AG-UI protocol SDK is also appropriate,
without UI/agent-framework integration dependencies. Provider/protocol/telemetry dependencies stay optional where
appropriate; model-free authoring must not require them all.

## Reading the examples

Examples illustrate one entry, not an installed package. Usage blocks include
their imports; signature-only blocks omit surrounding module context. Examples
using an agent from another page explicitly link its definition.
Values named as function parameters or described as application dependencies must
be supplied by the application. No placeholder is a built-in authentication,
policy, publishing, or routing implementation. Syntax checks do not prove SDK
compatibility or runtime behavior.

The authoring path is an `Agent` subclass with separate async `@skill` methods,
Each accepting injected `ctx: RunContext`. Local managed calls select a stable
skill ID through `Runtime.invoke`. Remote A2A uses native messages/tasks and
preferred structured skill selection through a card-declared, caller-activated
extension and native data-part JSON arguments, or free-text routing/extraction.
Descriptive A2A skills alone are not Python-method RPC.
Optional MCP publishes allowlisted skills as native tools, not internal `@tool`
functions. `Inject[T]` service parameters are excluded from all caller/card/MCP
schemas; registered services are validated at startup. No custom REST skill API
is proposed for the initial slice.
One server/shared runtime has an endpoint per caller audience, not per skill:
A2A for agents, optional MCP for LLM apps/tools and AG-UI later for frontends.
All entry points share authorization, validation, limits, accounting and tracing.

## Reviewed capability and release decisions

The first usable release supports text generation, typed structured outputs
and bounded explicitly registered tool execution/results with generation
continuations. It returns **complete validated results**; streaming is later.
The decided author surface is `ctx.generate(text) -> str`,
`ctx.generate(text, output=Summary) -> Summary`, and
`ctx.generate(text, tools=[registered_tool])`. Adapters translate AgentFrame-owned
normalized messages/tool calls/results/usage/finish reasons. Unsupported requests
fail before sending; refusals/truncation raise typed errors (partial opt-in syntax open).
Built-in opt-in model-based A2A routing and custom routers are both required;
agent model injection alone never switches routing on.
Structured selection bypasses routing/model-selection cost, not execution
controls or the selected skill's inference cost. Unknown skills/invalid
arguments fail explicitly. Free-text ambiguity/missing input asks back via an
A2A `TASK_STATE_INPUT_REQUIRED` task. See the [request-style decision and sources](../mcp-skills.md#a2a-request-styles-and-extension-boundary);
extension URI/schema/name, detailed advertisement, router API and clarification
contracts remain open. Any future typed AgentFrame peer calls use structured
selection, with their method syntax still undecided.
MCP exposure stays opt-in per skill, empty by default; quick typed/single-shot
tool-like skills are candidates, while judgment/multi-turn/clarification skills
usually stay A2A-only. That is guidance, not an enforced classifier.

The [delivery milestones](../initial-outline.md#delivery-milestones) separate M0
development setup, M1 model-free `Runtime.invoke`, M2 native A2A with custom
routing only, M3 shared-model text generation, M4 typed structured outputs,
M5 bounded tools, M6 built-in opt-in model routing (dependent on M3/M4), and M7
hardening to the first usable release. M1 is the first proof; M3 meets the README
success criterion. M8+ is the next core multi-agent release; MCP, optional
telemetry integration and streaming/AG-UI remain separate optional follow-ups.

Applications own human approval workflows; no built-in first-release approval
pause/resume. Tool authorization/action checks still apply. Future streaming
supports a scoped optional AG-UI frontend adapter, not all AG-UI features, an
OTel trace stream, automatic frontend tools, persistence or resumability.
See [AG-UI design and primary sources](../streaming.md).

Discovery/registry and remote A2A invocation are **next-core-release M8+ scope
after the single-agent M7 release**: source adapters return
references, while the managed directory/resolver owns native card fetching,
validation, caching and explicit refresh. It enables application-built
orchestrator skills calling specialists, not a workflow/graph engine. Discovery does not invoke
remote agents or grant permissions, and A2A itself is not a registry protocol.
See the [discovery guide](../discovery.md) for subclassing and owned-resource
lifecycle examples; the Foundry project agents API is chosen, while enabled-agent
filtering, card mapping, auth/RBAC, pagination and compatibility remain to prove.
Cross-hop identity/delegation, permissions, deadlines, remaining token allowance
and trace/correlation are planned, without authority amplification or overriding
remote limits. Typed remote calls and small delegation/fan-out helpers are
options, not finalized signatures. Local N-agent development comes before
container packaging; Foundry/Kubernetes templates are later, not a hosting
platform. Acceptance includes independent deployment and non-AgentFrame A2A
client/server interoperability; see the [M8+ plan](../initial-outline.md#next-core-release-multi-agent-a2a-composition-m8).

## Cross-cutting behavior and failures

Managed invocation validates definitions, arguments, and outputs; creates
isolated context; enforces mandatory permissions, resource policy, configured
content checks, and effective limits; and propagates cancellation where supported.
Direct Python method calls bypass that pipeline. Python is not a security sandbox.

The [error hierarchy](runtime.md#agentframeerror) is decided: definition,
input/output validation, authentication/permission/policy, model capability/
refusal/incomplete/provider, limits and AgentFrame cancellation. Every error
carries invocation ID and a safe message; exact constructors, named limit
subtypes and adapter mappings remain open. A2A maps failures to native
failed/rejected states as appropriate; never expose raw provider exceptions,
stack traces or secrets. Logs are redacted; clarification remains input-required.

`invoke` returns a validated value; `run` returns `RunResult` with local metadata
even without tracing. Remote A2A receives only invocation ID response metadata.
Services support app-owned singletons and AgentFrame-created/closed per-run
instances. Defaults resolve library -> agent -> skill -> per-run within host
ceilings: 120 seconds, 100000 total tokens, 10 model attempts, 20 tool calls;
input cap derives from reported context size, while input plus reserved output
still fits capacity. Output cap is canonical `ModelSettings.max_output_tokens=4096`.

Remote hosts require `HostConfig.auth` (Entra, API key, explicit anonymous or
custom); no choice means no startup. Host defaults: 32 concurrent runs,
1 MiB request, 4 MiB response, 30-second drain then cancellation, with `/health`
and `/ready`. Model retries: transient failures only, two retries/backoff, SDK
retries disabled. Side-effecting tools never auto-retry. A2A same caller/message
ID returns the original task for 10 minutes, memory by default with pluggable
multi-instance storage, explicitly not exactly-once.

## Decisions still needed

The [2026-11-11 platform/API decisions](../initial-outline.md#resolved-m0-platform-and-api-decisions)
resolve Python (3.13+, test through 3.15), A2A (protocol 1.0 via official
`a2a-sdk`, observed 1.3.0/specification v1.0.1), Foundry models (OpenAI v1
Responses through optional `openai`, Entra token-provider/API-key auth), and
the M8+ Foundry registry source (project agents API via `azure-ai-projects` 2.x,
observed 2.10.0). Versions observed are not pins or tested compatibility.

| Area | Open contract |
| --- | --- |
| Packaging | Final Ruff rules/configuration, pytest async integration, exact compatible tool/dependency pins and Python 3.13-3.15 CI coverage. Ruff formatter/linter, strict mypy on `agentframe` and pytest are decided; follow the [readability-first rules](../../AGENTS.md#code-quality-and-readability). Exported modules, unspecified constructors and accepted types remain open. |
| Definitions | Metadata fallbacks, deterministic inheritance/overrides and typed dynamic-instruction callables. `Inject[T]` / `Services` and startup missing-service validation are decided. |
| Invocation | Exact generic typing, duration unit, usage completeness/estimate representation and failed-run usage access; per-run options/partial-field composition, startup ID allocation and error constructors/limit subtype names. `invoke`, `run`, `RunResult` and defaults are decided. |
| Models | Exact normalized field layouts/finish enums, schema subset and output/tools combinations, partial-result opt-in, provider mappings/settings, credential wrapping/lifecycle. AgentFrame normalization ownership, `output=` / `tools=`, fail-before-send and typed refusal/truncation are decided. |
| Accounting | Token-counting support, strict-mode contract, unknown-usage reconciliation and provider-specific output/reasoning semantics. |
| Identity and policy | Principal construction, custom authenticator/authorization/content-check methods, API-key map schema, Entra scope mapping/permission merging and delegation trust. `auth`, built-ins, `role_permissions` and explicit `AllowAnonymous()` are decided. |
| Hosting | SDK pin/transport binding; structured extension URI/name/schema/selector/advertisement; native result/error metadata mappings, follow-up lifecycle, routers/extraction/clarification; MCP mappings/auth; byte measurement, busy binding, probe bodies and host output-ceiling carrier. Protection defaults and probe purposes are decided. |
| Streaming | Later runtime event API and scoped AG-UI version/binding/mapping, native vs invocation identifiers, tenant isolation, policy gating, cancellation/disconnect/buffering/backpressure/reconnect and typed-result serialization; no blanket conformance. |
| Discovery | Reference/JSON schemas, query matching/enrichment/pagination, resolver auth/destination interfaces, source reconciliation, result/error types, TTL/stale/refresh/background contracts, cache isolation, ownership/host wiring, Foundry listed-agent/card mapping, A2A-enabled filtering, auth/RBAC and pagination, separate remote-client API. The project agents source API is chosen. |
| Multi-agent release | Native A2A remote-call and optional typed-call mapping; trusted cross-hop identity/permission/deadline/budget/trace propagation, optional delegation/fan-out helpers, local N-agent development and container packaging. No chosen client/helper/deployment syntax. |
| Execution and DI | Factory/disposal interfaces, explicit local startup method, app/runtime service-container conflicts; retry configuration/backoff/classification, tool flag combinations and additional retry rules; dedupe store/window API, atomicity, changed payloads and message-only interactions. DI/ownership, default retries/deduplication and no automatic side-effect replay are decided. |
| Later extensions | Streaming, executor/state-store extensions and additional modalities. Durable checkpointed flows/pause/resume/crash recovery are possible later work, not rejected or committed; no current workflow/graph engine, built-in approval workflow or hosting platform API. |

Use the [design outline](../initial-outline.md#open-design-decisions) to track
rationale and delivery milestones. Resolve open details with implementation and
interoperability evidence before advertising support.

## Guides and design rationale

Start with [agent examples](../agent-examples.md) for a walkthrough. The
[documentation index](../README.md) links the existing design discussions on
annotations, base classes, limits, tracing, protocols, authorization, and prompt
safeguarding. They remain in place; this reference is a lookup companion, not a
replacement or a second set of independently evolving decisions.
