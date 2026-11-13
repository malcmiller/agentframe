# Roadmap

## Product definition

AgentFrame makes creating agents declarative: a Python class defines behavior,
decorators describe the agent and its skills, and types define validated data
contracts. The library connects those definitions to native A2A interactions
and optionally to a model host.

The goal is a default way to build 1 to N independently deployable agents:
specialists and ordinary orchestrator agents that cooperate over A2A, including
with other frameworks' A2A agents. Deliver the single-agent library through M7,
then multi-agent composition as the next core release (M8+), not a hosted platform.

## Independence and dependency boundaries

AgentFrame is intended as an independent alternative to existing agent
frameworks, not an authoring facade over one. Do not depend on major agent
packages such as LangChain, LangGraph, Microsoft Agent Framework, Pydantic AI,
Google ADK, CrewAI, AutoGen, or Semantic Kernel to define or execute agents.
Do not introduce such dependencies indirectly through an integration package.

AgentFrame owns agent definitions, validated invocation, execution limits,
bounded model/tool coordination, and mapping between its execution
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
`Inject[T]` skill parameters resolve trusted `Services` registrations and never
appear in caller input, Agent Cards or MCP schemas. `add` registers app-owned
singletons; `add_scoped` creates per-run instances closed by AgentFrame.
Missing required services fail startup. Both `Runtime` and `AgentApp` accept `services=`.

`Runtime.invoke` selects a stable skill ID explicitly for local calls. Remote A2A
requests enter through the host: prefer structured skill selection and JSON
arguments in a data part under a card-declared, caller-activated A2A 1.0
extension. This bypasses routing, not validation/authorization/limits/accounting.
Free-text requests use an explicit router for selection and argument extraction.
Do not require authors to implement a large dispatch handler, infer an A2A
method-call protocol from skill metadata, or silently pick the first skill.
Provide a built-in opt-in model-based router plus custom-router support.
Model-assisted selection is separate from model-assisted skill execution; an
injected model never implicitly enables it. Router selection, validated
argument extraction and clarification still need an exact contract. Ambiguity
or missing prompt arguments asks back via `TASK_STATE_INPUT_REQUIRED`, never guesses.

Use one server/shared runtime and an endpoint per audience, not per skill:
A2A for agent callers, optional MCP for LLM apps/tools, AG-UI later for frontends.
All entry points share authorization, validation, limits, accounting and tracing.
Do not add a custom REST skill API. Publish only named, allowlisted skills as
MCP tools (empty by default); keep this
separate from internal model-callable `@tool` registration and caller authorization.
A2A stays first-class and MCP does not block the minimal initial slice.
Tool-like typed/quick/single-shot skills may also use MCP; agent-like judgment,
multi-turn or clarification skills usually stay A2A-only. This is guidance,
not an enforced exposure rule. The [request-style guide](design/mcp-skills.md#a2a-request-styles-and-extension-boundary)
cites the native extension, data-part and task-state mechanisms; the extension
URI/schema/name and detailed support advertisement remain open.

The complete [piece-to-code mapping](design/base-classes.md#authoring-and-extension-decisions)
records which surfaces are annotation-based, subclass-derived, injected, or
library-managed. The normal authoring path needs only one AgentFrame subclass.
Custom components may implement structural protocols without inheriting a
framework base class.

## Reviewed release decisions

| Deliverable | Confirmed direction | Still open |
| --- | --- | --- |
| First usable release | One `ctx.generate`: text, `output=Summary` for validated Pydantic output, `tools=[...]` for registered bounded continuations; complete validated results, not partial streams. AgentFrame owns normalized model types; unsupported capabilities fail before sending; refusals/truncation raise typed errors. | Normalized usage layouts; provider schema subsets are documented per adapter (M4 #55/#56); no implicit partial-output success (explicit opt-in: catch `IncompleteOutputError`, read `.partial_text`, M4 #54). |
| Services and results | `Inject[T]`, `Services.add` / `add_scoped`, `services=` on app/runtime; startup missing-service errors. `invoke` returns a value; `run` returns `RunResult` independent of tracing. | Factory/disposal interfaces, generic typing, duration unit, usage marker fields and failed-run metadata access. |
| Errors and identity | Named [safe error hierarchy](api/runtime.md#agentframeerror); `Authenticator` with `EntraIDAuth`, `APIKeyAuth`, explicit `AllowAnonymous()`. `HostConfig.auth` is mandatory remotely; `role_permissions` maps verified claims. | Error constructors/startup correlation and exact transport mapping, principal/custom-auth methods, API-key entries and scope/permission merge rules. |
| Retry and host defaults | Transient model failures only, two retries/backoff, SDK retries disabled; side-effecting tools never auto-retry. Same caller/A2A message ID dedupes for 10 minutes (memory, pluggable store), not exactly-once. Host defaults 32 runs/1 MiB request/4 MiB response/30 seconds drain; `/health` and `/ready`. | Retry/store configuration, atomicity/payload conflicts, byte measurement, busy binding/probe schema; defaults themselves decided. |
| A2A request styles | Prefer deterministic structured skill/JSON-argument requests through a card-declared, caller-activated extension. Free-text uses custom or opt-in model routing; ambiguity/missing input asks back via input-required tasks. No automatic routing from agent model injection. | Router/extraction/clarification contract, native result mappings. The skill-call extension wire format is implemented (M2 #5). |
| Human approvals | Application-owned workflows; no built-in approval pause/resume in first release. Required tool/action authorization remains enforced. | Application integration/decision types; protocol tasks are not durable approval workflows. |
| Multi-agent composition | M8+: `Remote["name"]` injection, structured `call`, plain `ask`, input-required error; multi-source directory/find, YAML files, 300-second stale-refresh cache, OBO/service identity, deadline/token/depth controls (5 hops), plain Python orchestration, dev CLI/containers. | Detailed extension schema/identity config, source conflicts/card mapping/search pagination, accounting trust and dev config; typed class proxy only later, no initial orchestration helpers. |
| Durable workflows | Revisit after multi-agent usage; no design now. Long work uses native tasks, in-memory default/persistent store pluggable; approvals application-owned. If added, optional separate package (candidate `agentframe-workflows`). | Future evidence/scope; task persistence is not Python crash recovery or durable approval resume. |
| Streaming | Later `AgentApp(ag_ui=AGUIConfig(enabled=True))` at `/ag-ui`, same checks, lifecycle/text/tool-name-status/errors, whole-answer buffering, disconnect cancels, no reconnect/resume. | Version/binding, native event mapping, bounds/backpressure and typed final serialization; frontend tools/shared state later opt-in only. |

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

## Multi-agent discovery building blocks

As part of the next core multi-agent release (M8+) after M7, propose
a read-only `AgentRegistry` abstract base with async `get`/`list`, validated `AgentReference`/`AgentQuery`,
explicit environment/file sources, and the chosen Foundry project agents API
with A2A-enabled filtering/card mapping still to prove. This is agent discovery, not a process-global
plugin registry; model deployments are not agent endpoints.

A separate resolver and library-managed `AgentDirectory` fetch/validate native
cards, cache with caller/tenant/auth variant isolation, expose freshness and
explicit refresh, and close only owned resources. Applications should not write
HTTP/refresh loops per source. Construction/import does no I/O/polling;
background refresh is opt-in. Source failure is not deletion, and stale cards
are never silently current. Source reconciliation/removal, card TTL and
invocation authorization remain distinct.

These are building blocks for ordinary AgentFrame orchestrator skills, not a
workflow/graph engine, deployment service, write-registration system,
lease/federation layer, or A2A task manager. Discovery itself does not delegate;
a separate `RemoteAgentClient` uses native A2A after selection/authorization,
without a finalized method/payload API. A2A is not a registry protocol.
See [discovery design](design/discovery.md) and [surface reference](api/discovery.md).

## Initial adapters

| Adapter | Initial responsibility | Explicitly excluded |
| --- | --- | --- |
| Foundry | Call a configured GPT deployment through OpenAI v1 Responses using the official `openai` Python package, with injected credentials and client lifecycle. | Retired Azure AI Inference SDK, hosted Agent Service deployment and automatic cloud provisioning. |
| Ollama | Call an explicit locally installed Llama model tag through an Ollama endpoint. | Model downloads, server management, and implicit cloud-model execution. |

First-release model adapters must specify capability checks and tested support
for text, typed structured output and tool calls/results with continuations.
Not every deployment/model supports every feature; unsupported requested
capabilities fail explicitly rather than silently degrading to text.

The Foundry API path is selected below; supported deployment features and
compatibility must still be tested when implemented. An adapter must not treat all Foundry endpoints as
interchangeable. Ollama support must distinguish genuinely local models from
cloud-routed model tags; localhost alone does not guarantee local inference.

## Configuration and safe defaults

- Resolve defaults **library → agent → skill → per-run**.
  Use `@skill(limits=..., model_settings=...)` and per-run
  `options=RunOptions(...)`, merged field by field (M3 #42). Host ceilings are constraints, not overridable defaults.
- Require explicit model/deployment selection. Never silently switch hosts or models.
- Use loopback for local serving. All remote hosts require an explicit `auth`
  choice, even on loopback; omission refuses startup. Choose Entra/API key,
  explicit anonymous, or custom `Authenticator`. Require an explicit advertised
  URL and HTTPS outside local development.
- Do not infer the advertised endpoint from untrusted forwarded headers.
- Keep credentials, private prompts, and private tools out of public cards.
- Validate declared inputs and outputs, and reject unsupported required model features.
- Use finite request deadlines, bounded concurrency, output limits, and bounded
  model/tool iterations. Approved defaults: 120 seconds, 100000 total tokens,
  10 model attempts, 20 tool calls; `ModelSettings.max_output_tokens=4096`.
  Input cap defaults to reported context size, with input plus output reservation
  still fitting capacity; overflow fails, never silently truncates.
- Propagate cancellation and deadlines through the adapter where supported.
  Do not promise that cancellation always prevents remote inference charges.
- Retry transient model rate-limit/timeout/server failures only, with backoff,
  default two retries. Disable provider-SDK retries; each dispatched attempt
  consumes the same deadline/budget. `@tool(side_effects=True)` never auto-retries;
  `@tool(idempotent=True)` is not a blanket retry grant.
- Keep sampling settings model-aware rather than forcing universal temperature
  or reasoning parameters.
- Inject credentials rather than embedding them in source. Use an intentional
  production identity; broad development credential chains are not production policy.
- Log correlation and operational metadata by default, not prompts, outputs, or secrets.
- Treat in-memory task storage as ephemeral. Never claim restart durability.
- Isolate task and conversation access by verified caller identity when authentication
  is enabled. A client-supplied context ID is not authorization.
- Offer no shell, filesystem, network-fetching, or other tools by default.
- Bound tool execution and generation continuations, validate tool arguments
  and results, authorize each action, and never retry side effects implicitly.
  Normalized tool-call layouts are settled (M5 #59). No tool is ever retried
  automatically, model retries never replay tools, and repeated model-requested
  calls are new counted calls, not blocked (M5 #63).
- Host defaults: 32 concurrent runs (excess immediately rejected with standard
  busy error), 1 MiB request, 4 MiB response, 30-second shutdown grace (stop
  accepting, drain, then cancel). `/health` is liveness; `/ready` means startup
  checks passed and not shutting down. All are configurable; exact byte/error
  mappings and probe response bodies remain open.
- A2A deduplicates the same caller and `messageId` for a configurable 10-minute
  window, returning the original task. In-memory default, pluggable multi-instance
  store; not exactly-once or restart-durable. Task access is still authorized.

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
`HostConfig(auth=EntraIDAuth(tenant_id=..., audience=...),
role_permissions={"Editor": ["documents:write"]})` validates Entra identity and
maps verified roles/scopes; API-key configuration is for explicit dev/simple
service use, `AllowAnonymous()` is never default. Application owns resource policy.

Prompt safeguarding is shared responsibility, not a universal model filter.
Keep trusted instructions separate from untrusted content, credentials outside
model context, and content out of default logs. Application-supplied
`ExecutionPolicy` checks cover input, each outbound model request (including
model routing), tools/actions, and outputs. Required checks fail closed.
External screening is explicit data egress, not an automatic safety fallback.

Validate model output but never treat it as authority. Preserve or reduce
permissions during delegation; service credentials do not expand caller grants.
Resource decisions must be enforced at the actual boundary where side effects happen, not
only at skill admission. See [authorization](guides/authorization.md) and
[prompt safeguarding](design/prompt-safeguarding.md).
Human approval workflows belong to the application. No built-in approval
pause/resume is part of the first release; unavailable required approval or
authorization must prevent the action, not silently allow it. Native A2A tasks
do not establish durable approval storage or workflow resumption.

## Token budgets and tracing

Use `ModelSettings.max_output_tokens` for one model request and `RunLimits` for
assembled-input bounds, aggregate invocation tokens, model-attempt count, and
tool-call count/elapsed time. Model context capacity is a capability, not a configurable
guarantee. Host ceilings are independent of overridable defaults.

Preflight/reserve allowances and reconcile actual usage; label estimates and
unknown usage honestly. Strict accounting must reject calls without a reliable
bound. Do not promise a hard financial cap or cancellation without charges.
Budget any model-backed routing before skill execution and inherit remaining
allowances rather than reset them. See [limits](guides/limits.md).

Use optional `TracingConfig` on `Runtime`, with an application-owned
OpenTelemetry provider. Sampling/export are application policy; content capture
is off by default. Keep correlation and accounting independent of tracing.
Do not leak credentials, prompts, outputs, or private reasoning; incoming trace
context is not identity. See [tracing](design/tracing.md).
`Runtime.invoke` returns the validated value. `Runtime.run` returns `RunResult`
with value, invocation ID, token usage (complete/estimated; unknown never zero),
model/tool calls, duration and finish reason even with tracing off. A2A returns
only invocation ID execution metadata, not internal counters/provider details;
the negotiated M8+ extension separately reports downstream accounting usage.

## Delivery milestones

Each milestone is independently releasable and testable; M7 completes the first
usable single-agent release. Earlier milestones prove narrower slices, not the entire
first-release capability set. M0 introduced the package skeleton and
development tooling; M1 adds model-free authoring and local execution.

| Milestone | Deliverable | Acceptance |
| --- | --- | --- |
| 0 | Development setup (complete) | Installable typed `src/` skeleton, no runtime dependencies, exact development pins and `uv.lock`. Ruff formatting/lint (88-character lines), strict mypy, pytest-asyncio auto mode, pre-commit Ruff/mypy, CI checks on Python 3.13/3.14/3.15 and isolated wheel import. All checks passed locally and on `main`. |
| 1 | Model-free authoring and `Runtime.invoke` (complete) | A class with multiple decorated skills needs no `handle()`; metadata is deterministic; invalid definitions, missing services and invalid arguments fail explicitly before skill effects. Prove annotated `Inject[T]`, singleton/scoped ownership, isolated context and validated outputs without network access. `invoke` returns the value; `run` exposes local `RunResult` metadata. |
| 2 | Native A2A hosting with structured requests and custom routing only (complete) | Publish a valid card and accept an independent official SDK client's message with complete validated results. Prove declared/activated structured extension selection with JSON data-part arguments and no router call; unknown skills/invalid args fail explicitly. Prove free-text custom selection/extraction, input-required clarification, argument validation and typed input/output mappings, with explicit access policy and labeled application-supplied development auth/policy placeholders. Add `agentframe run module:create_app` serving the application's configured `AgentApp`, plus a localhost-only anonymous `module:Class --dev` mode. Verify task retrieval/cancellation only if advertised. Do not advertise streaming or unimplemented capabilities; built-in model routing arrives in M6. |
| 3 | Text generation through the shared `Model` contract (complete) | Prove text generation with Ollama and Foundry, switching between local Llama and a Foundry GPT deployment through configuration only, not skill changes. This meets the README success criterion. Test usage accounting; model-free skills still run without inference, and no model means generation fails explicitly. Provider tests are explicit opt-ins. |
| 4 | Typed structured outputs (complete) | Prove `ctx.generate(..., output=Summary)` returns a validated model. Unsupported capability fails before sending; invalid output, refusal and truncation raise the decided errors, never fake successful text. Partial-result opt-in remains explicit and cannot waive validation. |
| 5 | Bounded tool calling (complete) | Prove registration plus `ctx.generate(..., tools=[...])`, normalized arguments/results, continuations, action authorization and `max_tool_calls` exhaustion. Side-effecting tools never auto-retry; transient-model retry defaults and idempotence declarations do not replay actions. |
| 6 | Built-in opt-in model router (complete) | Depends on M3 and M4. Prove free-text selection, validated argument extraction and input-required clarification alongside custom-router support. Structured extension requests bypass this router and model-selection cost. Budget routing before skill execution and pass the remaining allowance onward; injecting an agent model never enables routing implicitly. |
| 7 | Hardening and first usable release | Prove decided defaults/precedence within ceilings, deadlines/cancellation, DI startup/scoped cleanup, `RunResult` without telemetry, safe correlated errors and identity/resource/policy failures. Prove host admission/byte/shutdown limits, health/readiness and A2A deduplication/caller isolation without exactly-once claims; installed-wheel examples return complete validated results. No built-in approval pause/resume. |

M0 acceptance was verified on **2026-11-11**: all four checks passed locally on
Python 3.13.16, 3.14.8 and 3.15.0; fresh-clone `uv sync`, pre-commit, sdist/wheel
builds, and an isolated wheel import/type marker check passed. The
[green CI run on `main`](https://github.com/malcmiller/agentframe/actions/runs/43155553347)
at commit `8d6a825` passed all three Python jobs and the build/import job.
CI uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.3`; the plan's
`setup-uv@v10` alias did not resolve, so the verified release tag is pinned.

M1 decisions confirmed on **2026-11-11** and implemented: `@agent` name/description
default to the class name/docstring (`version` required); `@skill` requires `id`,
with name defaulting to the title-cased method name, description to the method
docstring (a description is required for agents and skills), and empty
permissions. Skill tags originally defaulted to empty; on **2026-11-12** M2 made at
least one non-blank tag required for A2A 1.0 compliance. Definitions validate at class creation without I/O. Subclasses
inherit skills; redefining a skill needs `@skill` again, and duplicate ids across
methods fail. Pydantic lax validation rejects unknown/missing arguments (`ctx`,
injected names and `principal` count as unknown) with value-free messages.
`Services.add` is app-owned; `add_scoped` accepts sync, async or async-generator
factories whose post-`yield` cleanup always runs; duplicates fail. `RunResult`
uses a UUID-string `invocation_id`, `timedelta` duration, `Usage(0, 0, 0,
estimated=False)` for model-free runs and `FinishReason.COMPLETED`. A frozen
`Principal(id, permissions)` enforces all-of `requires_permissions`, and
`RunLimits.timeout_seconds` (default 120) raises `LimitExceededError`.
M1 is complete: CI is green on `main`.

M2 native A2A hosting was completed on **2026-11-12**: its stories (#1-#10, #19,
#25) are merged, and `tests/integration/test_interoperability.py` drives every acceptance
scenario with the independent official `a2a-sdk` client over localhost HTTP:
card discovery, structured requests with typed model/constrained arguments and results, free-text requests, clarification in the same
task, API-key authentication, permission and policy rejection, safe failures,
non-blocking send, cancellation and caller isolation. CI runs it with no
network beyond localhost.

M3 text generation was completed on **2026-11-12**: its stories (#33-#42) are
merged. `examples/assistant.py` serves one agent class with `OllamaModel` or
`FoundryModel`, chosen only by environment variables, meeting the
[README success criterion](../README.md#first-success-criterion).
`tests/integration/test_success_criterion.py` proves it offline in CI with the official A2A
client, two scripted models, a model-free skill that runs without inference and
the hosted usage snapshot; the opt-in `tests/integration/test_providers.py` runs the same
contract against real Ollama and Foundry.

M4 typed structured outputs were completed on **2026-11-12**: its stories
(#53-#57, after the #98 restructure) are merged. `ctx.generate(text, output=Summary)` returns a validated
Pydantic instance; `FoundryModel` sends a strict JSON schema (#55) and
`OllamaModel` the schema as `format` (#56); invalid output, refusal and
truncation raise `OutputValidationError`, `ModelRefusalError` and
`IncompleteOutputError`, and a model without structured output fails before
sending with `UnsupportedCapabilityError`. Truncated text is an explicit
opt-in through `.partial_text` (#54); structured output never has one.
`examples/assistant.py` gained a `summarize` skill, and
`tests/integration/test_structured_example.py` calls it with the official A2A
client offline in CI: one data part `{"value": {...}}` with the usage snapshot,
and safe `FAILED` tasks for every failure. The opt-in
`tests/integration/test_providers.py` proves structured calls, structured
truncation and the hosted example against real Ollama and Foundry.

M5 bounded tool calling was completed on **2026-11-12**: its stories (#58-#66)
are merged. `@tool` functions are registered on the agent and selected per
`ctx.generate(..., tools=[...])` call (#58, #60); messages, tool calls and
results are normalized (#59); the bounded loop runs tool calls until the model
answers, within `max_tool_calls` (#61); every call is checked against
`requires_permissions` and an optional `ToolAuthorizationPolicy` (#62); model
retries never replay a tool and no tool is auto-retried (#63); and
`FoundryModel` (#64) and `OllamaModel` (#65) map tools natively.
`examples/orders.py` serves an order desk with a read-only `lookup_order` and a
side-effecting, permission-gated `cancel_order` tool, and
`tests/integration/test_tools_example.py` calls it with the official A2A client
offline in CI: a tool round trip with model and tool calls in the usage
snapshot, no tools on the served card, safe failures for tool-call limit
exhaustion (`FAILED`), a denied call (`REJECTED`), a policy outage, a failing
tool (generic message, details only in logs) and a model without tool support,
and a transient model error after `cancel_order` that does not run it again.
The opt-in `tests/integration/test_providers.py` runs a one-tool round trip,
locally and hosted, against real Ollama and Foundry.

M6 built-in opt-in model routing was completed on **2026-11-12**: its stories
(#67-#70) are merged. Router model calls go through `RoutingContext.generate`,
charged to the same run as the selected skill, so routing counts toward the
agent's token and model-call limits before the skill runs and the skill gets
what remains (#67). `ModelRouter(model)` selects one of the caller's eligible
skills, then extracts and validates its arguments, or asks a question instead
of guessing (#68). It is enabled only by `AgentApp(router=ModelRouter(model))`,
never by an agent model; the app builds its schemas at construction and checks
its model at startup (#69). `examples/routed_assistant.py` serves two skills
with the built-in router, and `tests/integration/test_routing_example.py`
calls it with the official A2A client offline in CI: free text routed to the
right skill with routing plus skill calls in the usage snapshot, a missing
argument asked for (`INPUT_REQUIRED`) and answered in the same task
(`COMPLETED`), a structured call that never calls the router model, a
model-call limit that fails routing before the skill runs, and a custom
`SkillRouter` on another app. The opt-in `tests/integration/test_providers.py`
routes one unambiguous free-text request against real Ollama and Foundry,
checking only the selected skill and that its argument is present (#70).

M1 is the first proof: a model-free agent. M2 adds independent native A2A
interoperability; M3 meets the [README success criterion](../README.md#first-success-criterion)
with interchangeable Foundry/Ollama configuration; M4 adds typed structured
output; M5 adds bounded tool calling; M6 adds built-in opt-in model routing.
M7 hardening completes the first usable release.

### Resolved M0 platform and API decisions

Approved and source-checked on **2026-11-11**. These resolve the Python, A2A
protocol/SDK target and Foundry API choices. Observed versions are snapshots,
not pins; pin and test each protocol/provider package when introduced in its
milestone. M2 has pinned the A2A SDK and selected its binding (row below); the
other packages are not yet installed dependencies or evidence of compatibility
testing. M0 configures the Python/tooling matrix; future releases are not
automatically tested.

Human readability is a top-priority acceptance criterion: simple, focused code
and reviewable diffs, not speculative abstractions. Follow the
[coding-agent and contributor rules](../AGENTS.md#code-quality-and-readability).
M0 uses uv **0.13.1** (pinned in CI), hatchling **1.33.0**, Ruff **0.17.4**,
mypy **2.4.1**, pytest **9.2.0**, pytest-asyncio **1.4.1.post1** in **auto** mode,
and pre-commit **4.7.0**. Build/development dependencies are exactly pinned in
`pyproject.toml`; transitive development versions are locked in `uv.lock`.
The Ruff rule families are `E`, `W`, `F`, `I`, `B`, `UP`, `SIM`, `N`, `C4`
and `RUF`. See [development commands](../README.md#development-and-contributions).

| Area | Decision | Implementation work still needed |
| --- | --- | --- |
| Python | Support 3.13+ and test 3.13, 3.14 and 3.15. Python has no LTS designation; the [official release-cycle data](https://peps.python.org/api/release-cycle.json) lists 3.15 in bugfix since 2026-10-09 and 3.13 in security maintenance. | The M0 CI matrix is configured; retain all three versions as the implementation grows. |
| Development tooling | Ruff formatter/linter instead of Black/flake8/isort, PEP 8 conventions with 88-character lines; strict mypy on `agentframe` and tests; pytest with pytest-asyncio auto mode. | Configured in `pyproject.toml`, CI and pre-commit. All four checks must pass before committing; tests run in CI rather than on every commit. Avoid rules that contort readable code. |
| A2A | Target protocol **1.0**, using the official `a2a-sdk` Python package. [Latest specification release observed: v1.0.1](https://github.com/a2aproject/A2A/releases/tag/v1.0.1), published 2026-05-28; [SDK observed: 1.3.0, Python >=3.10](https://pypi.org/project/a2a-sdk/1.3.0/). Do not advertise 0.3 support. **M2 (2026-11-12):** pinned `a2a-sdk[http-server]==1.3.0` as a core dependency (hosting is the default interface; the optional `agentframe[server]` extra only adds uvicorn for `agentframe run`, M2 #9); the `http-server` extra explicitly supplies Starlette/SSE. Selected binding: **JSON-RPC over HTTP** (`JSONRPC`, protocol version `1.0`), the most widely supported 1.0 binding. The SDK's 0.3 compatibility mode stays disabled, so requests without `A2A-Version: 1.0` are rejected; gRPC and HTTP+JSON/REST are not offered. `tests/unit/hosting/test_a2a_binding.py` and the CI wheel check prove the supported install. Native type mappings and independent official-client interoperability with AgentFrame hosting are proven end to end in `tests/integration/test_interoperability.py` (M2 #10). | Upgrade the SDK only deliberately, with the binding tests. Selecting JSON-RPC does not select every optional capability. |
| Foundry model inference | Use OpenAI v1 **Responses API**, base URL `https://<resource>.openai.azure.com/openai/v1/`, via official `openai`. Inject either an API key or Microsoft Entra ID token provider (`azure-identity` `get_bearer_token_provider`, scope `https://ai.azure.com/.default`). The [Azure AI Inference SDK is retired](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration); do not use it. | Keep `openai`/`azure-identity` optional provider extras; pin compatible dependencies and prove M3 deployment/capability/auth and client-lifecycle behavior. AgentFrame constructor/credential wrapping remains proposed. |
| Foundry discovery (M8+) | Choose the project agents API through `azure-ai-projects` **2.x** ([2.10.0 observed](https://pypi.org/project/azure-ai-projects/2.10.0/)), project endpoint `https://{account}.services.ai.azure.com/api/projects/{project}`. [Incoming A2A documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint) shows prompt agents enabling A2A with `agent_card` and protocol configuration, publishing cards and accepting A2A requests. Select GA protocol 1.0 explicitly. | Prove listed-agent to card-URL mapping, filter to A2A-enabled agents, settle authentication/RBAC and pagination. This is read discovery of existing agents, not deployment of AgentFrame agents to Foundry. |

Foundry's documented card route for enabled agents ends in
`/agents/{agent}/endpoint/protocols/a2a/agentCard/v1.0`; its existence is not a
substitute for validating each listed agent's enabled endpoint. Enabling incoming
A2A requires Foundry User or higher; authenticated card/invocation access uses
Foundry Agent Consumer or another role granting endpoint access. Registry read
permissions and role mapping remain to be verified. Foundry's
[outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)
are a possible additional reference source, not a selected registry contract.

### Next core release: multi-agent A2A composition (M8+)

This is the next core release after M7, not an optional discovery follow-up.
Keep M0-M7 as the single-agent release, with single-agent `run` in M2.
The M8+ authoring contracts below are decided, while unspecified wire/configuration
details and finer milestone breakdown remain open.

| Capability | Planned direction and acceptance |
| --- | --- |
| Discovery and remote invocation | `AgentDirectory(sources=[...])` with YAML `FileAgentRegistry("agents.yaml")`, Foundry endpoint/credential source, explicit environment source and custom get/list. `AgentApp(directory=...)` validates `Remote["name"]` at startup. Native card-backed `find(skill=... / tags=...)`, 300-second cache/stale refresh, explicit stale opt-in, ownership/isolation/destination safety. `call("skill", ..., output=Type)` uses structured A2A; `ask(text)` works with any compatible peer. Both wait within deadline; input-required raises `ClarificationNeededError`, no automatic dialogue. |
| Orchestrator agents | Ordinary skills with plain `await`, `asyncio.TaskGroup` and directory search. No orchestration helpers in the first multi-agent release; later only after repeated usage. Optional `Remote[ResearchAgent]` typed proxy later uses the same structured wire request, not proprietary RPC. |
| Cross-agent context | Per-target Entra `on_behalf_of` default or explicit `service`; API-key/anonymous ingress service only, never raw token forwarding or identity fallback on exchange failure. Propagate remaining deadline (receiver min with own timeout), token ceiling (default caller remaining; receiver only lowers), actual peer usage charged once, max 5 hops and trace/correlation. Shared concurrent reservations; other-peer usage unknown. Same A2A extension, exact schema/config open; each target enforces policy and no hard billing guarantee. |
| Development and deployment | `agentframe.yaml` lists named module paths/ports. `agentframe dev` starts localhost agents, writes local registry, configures development API keys and combines agent/invocation-tagged logs. One container per agent; Dockerfile template and generated docker-compose example. Config/generation details open; Kubernetes/Foundry templates later, not infrastructure management. |

Release acceptance: deploy two or more AgentFrame agents independently; an
ordinary orchestrator discovers and calls a specialist over A2A with identity,
budget and trace/correlation propagation. Prove interoperability with a
non-AgentFrame A2A client and server, not just AgentFrame-to-AgentFrame calls.
Foundry discovery uses the chosen projects agents API, but release acceptance
must prove A2A-enabled filtering, card mapping, authentication/RBAC and pagination;
model deployments are not agent endpoints.

No durable workflow engine, built-in approval workflow, federation or deployment
service is included. Checkpointed multistep flows, pause/resume and crash
recovery are revisited only after multi-agent real usage. If added, use an
optional separate package (candidate `agentframe-workflows`). Long work uses
A2A tasks with an in-memory default and pluggable persistent store; no built-in
approval workflow or promise of recovering skill execution from persisted tasks.

### Optional follow-ups after M7

Optional MCP delivery follows M7. Its acceptance includes
initialization, explicit tool discovery/invocation, unsupported schema rejection,
authorization, shared limits/tracing, and independent-client interoperability.
Tracing integration adds a separate opt-in check for safe metadata, correct span boundaries,
and accounting that still works with tracing disabled. These additions do not
reintroduce a UI, workflow platform, or custom REST skill API.
See the [combined hosting proposal](design/mcp-skills.md).

Streaming/AG-UI is another separate follow-up after M7, with its existing
[acceptance criteria](design/streaming.md#open-contracts-and-delivery-gate) for ordering,
errors/truncation, refusal/incomplete results, safe side effects, isolation and
no secrets/private reasoning, proven with an independent AG-UI client. Pin the
feature profile, versions/binding and lifecycle/policy contracts before claiming
support. Persistent task storage is pluggable for long-running A2A work, not a
workflow engine. Streaming uses `ag_ui=AGUIConfig(enabled=True)` on `/ag-ui`,
same server/auth/limits/permissions, lifecycle/text/tool-name-status/error events
without raw arguments by default. Whole-answer checks emit complete text at end,
disconnect cancels, and first version has no reconnect/resume. AG-UI does not imply
a UI framework, orchestration platform or A2A streaming. Each advertised feature
needs a behavioral/interoperability test. These optional integrations have no
imposed order relative to one another or M8+; they do not demote multi-agent
composition from the next core release. Cross-agent trace/correlation propagation
belongs to that release even though telemetry exporters/integration stay optional.

## Open design decisions

- Exact AgentFrame-normalized usage layouts (message/tool-call layouts and
  finish reasons are settled, M5 #59).
  Ownership of normalization, `output=` / `tools=` (combined, M5 #61), typed
  failures, the partial-text opt-in (M4 #54) and per-provider structured-output
  schema mappings (Foundry strict schema #55, Ollama `format` #56) are decided.
- Scoped factory/disposal interfaces, explicit local startup method and app/runtime
  service-container conflict handling; `Inject`, `Services`, ownership and startup
  validation are decided, as are the M1 scoped-factory forms (sync, async,
  async generator), `timedelta` duration and `Usage(..., estimated)` markers.
  `RunResult` generics for `run`, provider usage completeness details and
  failed-run usage access remain open, not whether local metadata exists.
- Host byte measurement, busy-error binding and probe bodies; retry configuration/
  backoff/transient codes; dedupe store/window API, atomicity, changed payloads
  and message-only interactions. Protection defaults and no-side-effect replay
  are decided.
- Exact compatible runtime dependency pins when each is introduced. M0 tool
  pins, Ruff rules and pytest-asyncio auto integration are configured; Python
  3.13+ with testing through 3.15 and the A2A 1.0/official SDK target are decided above.
- A2A compatibility policy beyond the exact `a2a-sdk==1.3.0` pin and JSON-RPC
  binding (SDK upgrade cadence, independent-client matrix).
- Structured skill-input mappings and minimal-message versus task lifecycle behavior.
- Foundry deployment capability discovery beyond a configured context window;
  credential wrapping, injected-client lifecycle and response mapping are
  implemented in M3 #39 (`openai==3.49.0`, `azure-identity==1.27.0`;
  `aiohttp==3.14.5` added in M3 #40 for async Entra credentials).
- Error constructors/startup ID allocation/transport mapping and named limit
  subtypes. Approved defaults and error class names are no longer open.
- Token counting/strict-enforcement capabilities and incomplete-usage semantics
  for each supported provider.
- Pinned MCP/telemetry versions, authorization hooks, schema/result mappings,
  and transport-specific correlation metadata.
- Principal construction and OBO/service configuration/claims/consent, custom
  authenticator/resource-policy/content-check methods, API-key entry format,
  Entra scope mapping and permission merge rules. `auth`, built-in authenticators,
  `role_permissions` and explicit `AllowAnonymous()` are decided.
- Free-text routing beyond one task's clarification: durable or cross-task
  follow-ups and multi-skill orchestration. The `SkillRouter` protocol,
  `ModelRouter`, validated argument extraction and the `INPUT_REQUIRED`
  clarification contract are implemented (M6 #67-#69); structured requests use
  the native A2A extension, not a standardized base skill field.
- YAML name/URL schema details, environment encoding, source conflict/order,
  card enrichment bounds, find filter combinations/pagination, resolver
  auth/destination interfaces, source reconciliation, cache isolation/stale-age
  bounds and ownership details, Foundry mapping/RBAC/pagination. `sources`,
  `directory=`, `find`, TTL 300/on-stale refresh and named-remote validation decided.
- Shared extension URI/schema for structured calls, deadline/token/usage/depth;
  root/hop/clock semantics, atomic reservations and descendant usage trust/
  reconciliation, per-target identity config and result/argument-reserved-name
  collisions. `Remote.call` / `ask`, clarification error and max 5 hops decided.
  CLI config/loading/key lifecycle and Compose generation details remain open;
  no first-M8+ orchestration helpers. Typed class proxies/templates later.
- Future runtime streaming API, AG-UI version/transport/native event projection,
  typed-result serialization, buffer/write/backpressure/shutdown details.
  `AGUIConfig`, `ag_ui=`, `/ag-ui`, first profile/disconnect cancellation/no
  reconnect are decided. Frontend tools/state only later opt-in; workflows
  revisited after multi-agent usage in a separate optional package if added.

Resolve these with small executable examples before declaring a stable API.
