# Proposed API reference

AgentFrame is in early development: M1 model-free authoring (`@agent`, `@skill`,
`Agent`, `RunContext`, `Inject`, `Services`, `Principal`, `RunLimits` with
`timeout_seconds`, local `Runtime.invoke` / `run`, `RunResult`, `Usage`,
`FinishReason` and the M1 error classes) is available from source. M3 #33 adds
the normalized model types, the `Model` protocol and the model errors; #35 adds
`Agent(model=...)`, agent `instructions` / `model_settings` and text
`ctx.generate`; #42 adds field-by-field settings precedence and per-run
`RunOptions`. Version 0.1.0 is published as `agentframe-py`; pre-1.0 there is
no stable import surface. This reference organizes the
current contracts for iteration; it does not turn illustrative signatures into
implemented release commitments. Entries note where M1 or M2 implements them. Explicitly approved names/defaults are design
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
| [Decorators and data contracts](decorators.md) | [`@agent`](decorators.md#agent), [`@skill`](decorators.md#skill), [`@tool`](decorators.md#tool), [`ToolDefinition`](decorators.md#tooldefinition); ordinary Python/Pydantic contracts. |
| [Agents, context and services](agents.md) | [`Agent`](agents.md#agent), [`RunContext`](agents.md#runcontext), [`generate`](agents.md#runcontextgenerate), [`Inject[T]`](agents.md#injectt), [`Services`](agents.md#services), [`add`](agents.md#servicesadd), [`add_scoped`](agents.md#servicesadd_scoped). |
| [Local runtime and results](runtime.md) | [`Runtime`](runtime.md#runtime), [`validate_model`](runtime.md#runtimevalidate_model), [`invoke`](runtime.md#runtimeinvoke), [`run`](runtime.md#runtimerun), [`RunResult`](runtime.md#runresult), future [`StateStore`](runtime.md#statestore-deferred). |
| [Safe errors](runtime.md#agentframeerror) | `AgentFrameError`, `DefinitionError`, validation/auth/policy errors, `ModelError` and capability/refusal/incomplete/provider subtypes, `LimitExceededError` and its token/model-call/timeout subtypes, AgentFrame `CancelledError`; each has its own entry. |
| [Models](models.md) | [`Model`](models.md#model), [`Model.generate`](models.md#modelgenerate), [`Model.capabilities`](models.md#modelcapabilities-1), [`Model.close`](models.md#modelclose), [`Message`](models.md#message), [`FoundryModel`](models.md#foundrymodel), [`OllamaModel`](models.md#ollamamodel), [`ScriptedModel`](models.md#scriptedmodel) (test helper), [`ModelSettings`](models.md#modelsettings), [`ModelCapabilities`](models.md#modelcapabilities), [`ModelRequest`](models.md#modelrequest), [`OutputSchema`](models.md#outputschema), [`ModelResponse`](models.md#modelresponse), [`ToolSpec`](models.md#toolspec), [`ToolCall`](models.md#toolcall). |
| [Execution configuration](configuration.md) | [`AgentConfig`](configuration.md#agentconfig), [`RunLimits`](configuration.md#runlimits), [precedence](configuration.md#precedence), [`RunOptions`](configuration.md#runoptions), [`TracingConfig`](configuration.md#tracingconfig). |
| [Identity, authorization and content policy](authorization.md) | [`Principal`](authorization.md#principal), [`AuthorizationPolicy`](authorization.md#authorizationpolicy), [`ToolAuthorizationPolicy`](authorization.md#toolauthorizationpolicy), [`ExecutionPolicy`](authorization.md#executionpolicy), [`Authenticator`](authorization.md#authenticator), [`DeclaresCardSecurity`](authorization.md#declarescardsecurity), [`EntraIDAuth`](authorization.md#entraidauth), [`APIKeyAuth`](authorization.md#apikeyauth), [`AllowAnonymous`](authorization.md#allowanonymous); outbound identity (M8+ #92): [`Service`](authorization.md#service), [`OnBehalfOf`](authorization.md#onbehalfof), [`OutboundCredential`](authorization.md#outboundcredential), [`APIKeyCredential`](authorization.md#apikeycredential), [`EntraAppCredential`](authorization.md#entraappcredential). |
| [Hosting](hosting.md) | [`AgentApp`](hosting.md#agentapp), [`HostConfig`](hosting.md#hostconfig), [`A2AConfig`](hosting.md#a2aconfig), [`MessageDedupStore`](hosting.md#messagededupstore), [`MCPConfig`](hosting.md#mcpconfig), [`SkillRouter`](hosting.md#skillrouter), [`RoutingContext`](hosting.md#routingcontext); native SDK types. |
| [`ModelRouter`](hosting.md#modelrouter) | Opt-in built-in router alongside custom routers: two structured model calls select a skill and extract its arguments, or ask (M6 #68); checked by `AgentApp` at construction and startup (#69). |
| [Future streaming](streaming.md) | [`AGUIConfig`](streaming.md#aguiconfig), optional [AG-UI adapter](streaming.md#optional-ag-ui-adapter) and [runtime event boundary](streaming.md#runtime-streaming-boundary); runtime stream method open. |
| [Remote authoring](discovery.md#remoteliteralname) | [`Remote[Literal["name"]]`](discovery.md#remoteliteralname), [`call`](discovery.md#remotecall), [`ask`](discovery.md#remoteask), [`ClarificationNeededError`](discovery.md#clarificationneedederror), [`AgentDirectory.find`](discovery.md#agentdirectoryfind). |
| [Development and packaging](../guides/development.md) | M2 `agentframe run module:create_app` (implemented, with localhost-only `--dev`); M8+ `agentframe dev`, `agentframe.yaml`, registry/API-key/log wiring and container templates; not installed tooling. |
| [Multi-agent release discovery and calls](discovery.md) | Registries implemented (M8+ #83): [`AgentRegistry`](discovery.md#agentregistry), [`get`](discovery.md#agentregistryget), [`list`](discovery.md#agentregistrylist), [`AgentReference`](discovery.md#agentreference), [`AgentQuery`](discovery.md#agentquery), [`EnvironmentAgentRegistry`](discovery.md#environmentagentregistry), [`FileAgentRegistry`](discovery.md#fileagentregistry), [`FoundryAgentRegistry`](discovery.md#foundryagentregistry) and [`card_headers`](discovery.md#agentregistrycard_headers) (#86), directory implemented (#84): [`AgentDirectory`](discovery.md#agentdirectory), [`DiscoveredAgent`](discovery.md#discoveredagent), [`get`](discovery.md#agentdirectoryget), [`refresh`](discovery.md#agentdirectoryrefresh), [`__aenter__`](discovery.md#agentdirectory__aenter__), [`__aexit__`](discovery.md#agentdirectory__aexit__), [`find`](discovery.md#agentdirectoryfind) (#85); [`RemoteAgentClient`](discovery.md#remoteagentclient). |

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
each accepting injected `ctx: RunContext`. Local managed calls select a stable
skill ID through `Runtime.invoke`. Remote A2A uses native messages/tasks and
preferred structured skill selection through a card-declared, caller-activated
extension and native data-part JSON arguments, or free-text routing/extraction.
Descriptive A2A skills alone are not Python-method RPC.
Optional MCP publishes allowlisted skills as native tools, not internal `@tool`
functions. `Inject[T]` service parameters are excluded from all caller/card/MCP
schemas; registered services and M8+ `Remote[Literal["name"]]` bindings are validated at
startup and remote annotations likewise excluded. No custom REST skill API
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
fail before sending; refusals/truncation raise typed errors (catch `IncompleteOutputError` and read `.partial_text` to accept truncated text).
Built-in opt-in model-based A2A routing and custom routers are both required;
agent model injection alone never switches routing on.
Structured selection bypasses routing/model-selection cost, not execution
controls or the selected skill's inference cost. Unknown skills/invalid
arguments fail explicitly. Free-text ambiguity/missing input asks back via an
A2A `TASK_STATE_INPUT_REQUIRED` task. See the [request-style decision and sources](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary);
extension URI/schema/name, detailed advertisement, router API and clarification
contracts remain open. M8+ `Remote.call` uses that structured extension;
`ask` uses plain text with any compatible peer. Both wait within deadline;
input-required raises `ClarificationNeededError`, not automatic dialogue.
Later optional `Remote[ResearchAgent]` proxies use the same structured request.
MCP exposure stays opt-in per skill, empty by default; quick typed/single-shot
tool-like skills are candidates, while judgment/multi-turn/clarification skills
usually stay A2A-only. That is guidance, not an enforced classifier.

The [delivery milestones](../roadmap.md#delivery-milestones) separate M0
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
See [AG-UI design and primary sources](../design/streaming.md).

Discovery/registry and remote A2A invocation are **next-core-release M8+ scope
after the single-agent M7 release**: source adapters return
references, while the managed directory/resolver owns native card fetching,
validation, caching and explicit refresh. It enables application-built
orchestrator skills calling specialists, not a workflow/graph engine. Discovery does not invoke
remote agents or grant permissions, and A2A itself is not a registry protocol.
See the [discovery guide](../design/discovery.md) for subclassing and owned-resource
lifecycle examples; `FoundryAgentRegistry` reads the Foundry project agents API
(#86), with the registry-read role still to verify against a real project.
Cross-hop identity/delegation, permissions, deadlines, remaining token allowance
and trace/correlation are planned, without authority amplification or overriding
remote limits. Per-target OBO/service identity, remaining deadlines and token
ceilings, reported AgentFrame usage and maximum 5 hops are decided; wire/config
details remain open. Directory `sources=[...]`, native card `find`, YAML files,
300-second cache/stale refresh and `AgentApp(directory=...)` are decided.
No initial orchestration helpers; use Python `await` / TaskGroup / find.
M2 single-agent `run`, M8+ `dev`/local registry/auth/logging and one-container
packaging are decided; Foundry/Kubernetes templates are later, not a hosting
platform. Acceptance includes independent deployment and non-AgentFrame A2A
client/server interoperability; see the [M8+ plan](../roadmap.md#next-core-release-multi-agent-a2a-composition-m8).

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
even without tracing. Ordinary remote A2A receives the invocation ID and
a safe usage snapshot (token counts, estimated flag, model and tool call counts); negotiated M8+ extension usage is an accounting exchange, not `RunResult`.
Services support app-owned singletons and AgentFrame-created/closed per-run
instances. Defaults resolve library → agent → skill → per-run within host
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

The [2026-11-11 platform/API decisions](../roadmap.md#resolved-m0-platform-and-api-decisions)
resolve Python (3.13+, test through 3.15), A2A (protocol 1.0 via official
`a2a-sdk`, observed 1.3.0/specification v1.0.1), Foundry models (OpenAI v1
Responses through optional `openai`, Entra token-provider/API-key auth), and
the M8+ Foundry registry source (project agents API via `azure-ai-projects` 2.x,
pinned `azure-ai-projects==2.11.0` in the `foundry` extra, #86). Versions observed are not pins or tested compatibility,
except the M2 A2A pin: `a2a-sdk[http-server]==1.3.0` with the JSON-RPC over HTTP
binding, protocol 1.0 only, and the M3 provider extras (`ollama==0.6.3`;
`openai==3.49.0`, `azure-identity==1.27.0` and `aiohttp==3.14.5` for
`FoundryModel`).

| Area | Open contract |
| --- | --- |
| Packaging | Final Ruff rules/configuration, pytest async integration, exact compatible tool/dependency pins and Python 3.13-3.15 CI coverage. Ruff formatter/linter, strict mypy on `agentframe` and pytest are decided; follow the [readability-first rules](../../AGENTS.md#code-quality-and-readability). Exported modules, unspecified constructors and accepted types remain open. |
| Definitions | Metadata fallbacks, deterministic inheritance/overrides and typed dynamic-instruction callables. `Inject[T]` / `Services` and startup missing-service validation are decided. |
| Invocation | Exact generic typing, duration unit, usage completeness/estimate representation and failed-run usage access; startup ID allocation and error constructors (token, model-call and timeout subtypes are implemented, M3 #36; `ToolCallLimitExceededError`, M5 #61). `invoke`, `run`, `RunResult`, defaults and field-by-field `RunOptions` precedence are decided. |
| Models | Exact normalized usage layouts, provider settings, credential wrapping/lifecycle. AgentFrame normalization ownership, `output=` / `tools=`, fail-before-send and typed refusal/truncation are decided; per-provider structured-output schema mappings are implemented (M4 #55/#56); message/tool-call layouts and finish reasons are implemented (M5 #59), and so is the bounded tool loop with `output=` / `tools=` combined (M5 #61). |
| Accounting | Token-counting support, strict-mode contract, unknown-usage reconciliation and provider-specific output/reasoning semantics. |
| Identity and policy | Principal construction, a richer `AuthorizationPolicy` decision type and delegation trust. `auth`, built-ins, `role_permissions` and explicit `AllowAnonymous()` are decided; the `Authenticator` protocol, `APIKey`/`APIKeyAuth` (`X-API-Key` header) and union-of-roles permission mapping are implemented (M2 #4), as is `EntraIDAuth` with roles and scopes mapped together (M7 #76), as is `AuthorizationPolicy.check` with local `Runtime` (M2 #19) and `ToolAuthorizationPolicy.check_tool` with `@tool(requires_permissions=...)` (M5 #62), as are `ExecutionPolicy` input, model-request and output checks (M7 #77) and tool checks (M7 #78). |
| Hosting | Structured extension selector handling (#5); follow-up lifecycle beyond one task's clarification; MCP mappings/auth; byte measurement, probe bodies. Protection defaults and probe purposes are decided. The SDK pin/JSON-RPC 1.0 binding (M2 #1), Agent Card generation with the skill-call extension URI and per-skill argument schemas (M2 #2), and the `AgentApp` ASGI host with authenticated `/a2a`, public card and `HostConfig.role_permissions` (M2 #3), the native result/error mapping (M2 #7), `SkillRouter`, `ModelRouter` and input-required clarification (M2 #6, M6 #67-#69), `HostConfig.limits_ceiling` (M7 #71; a `max_total_tokens` ceiling also bounds output caps) and `HostConfig.max_concurrent_runs` (M7 #72; excess gets HTTP 503) are implemented. |
| Streaming | Native event/version/binding and ID/typed-result mapping, bounds/backpressure/write/shutdown details. `AGUIConfig`, `ag_ui=`, `/ag-ui`, tool names/status, whole-answer buffering, disconnect cancellation and no initial reconnect/resume decided; no blanket conformance. |
| Discovery | YAML name/URL key schema, environment encoding, source conflict/order, resolver auth/destination/source reconciliation/cache isolation/stale-age/ownership details, Foundry registry-read role verification against a real project. Sources, directory host wiring, find, 300s TTL/on-stale refresh and startup named binding validation decided. |
| Multi-agent release | Extension schema for token/usage (deadline/depth fields and hop semantics implemented, #89), OBO/service config/claim validation, atomic reservations/descendant usage. Remote.call/ask (result mapping and reserved `output`/`max_total_tokens` names, #88), clarification error, max 5 hops and no initial helpers decided. CLI config/loading/key lifecycle and Compose generation details open; typed class proxy later. |
| Execution and DI | Factory/disposal interfaces, explicit local startup method, app/runtime service-container conflicts. DI/ownership, default retries, deduplication (`A2AConfig`/`MessageDedupStore`, M7 #75), `@tool` flag defaults/combinations (M5 #58) and no automatic tool retry or replay (M5 #63) are decided; model retries (`RunLimits.max_model_retries`, backoff) are implemented (M3 #37). |
| Later extensions | Frontend tools/shared state later opt-in only. Revisit durable workflows after multi-agent usage, not designed now; optional separate package if added (candidate `agentframe-workflows`). Pluggable persistent A2A task storage is not workflow crash recovery. No core approval engine/hosting platform. |

Use the [roadmap](../roadmap.md#open-design-decisions) to track
rationale and delivery milestones. Resolve open details with implementation and
interoperability evidence before advertising support.

## Guides and design rationale

Start with [agent examples](../guides/agent-examples.md) for a walkthrough. The
[documentation index](../README.md) links the existing design discussions on
annotations, base classes, limits, tracing, protocols, authorization, and prompt
safeguarding. They remain in place; this reference is a lookup companion, not a
replacement or a second set of independently evolving decisions.
