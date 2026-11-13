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
| Errors and identity | Named [safe error hierarchy](api/runtime.md#agentframeerror); `Authenticator` with `EntraIDAuth`, `APIKeyAuth`, explicit `AllowAnonymous()`. `HostConfig.auth` is mandatory remotely; `role_permissions` maps verified claims. | Error constructors/startup correlation and exact transport mapping, principal/custom-auth methods and API-key entries. `EntraIDAuth` maps roles and scopes together (#76). |
| Retry and host defaults | Transient model failures only, two retries/backoff, SDK retries disabled; side-effecting tools never auto-retry. Same caller/A2A message ID dedupes for 10 minutes (memory, pluggable store), not exactly-once. Host defaults 32 runs/1 MiB request/4 MiB response/30 seconds drain; `/health` and `/ready`. | Retry configuration; defaults and minimal probe bodies decided. Dedup configuration (`A2AConfig`, `MessageDedupStore.put_if_absent`), atomicity and changed-payload refusal are implemented (M7 #75). |
| A2A request styles | Prefer deterministic structured skill/JSON-argument requests through a card-declared, caller-activated extension. Free-text uses custom or opt-in model routing; ambiguity/missing input asks back via input-required tasks. No automatic routing from agent model injection. | Router/extraction/clarification contract, native result mappings. The skill-call extension wire format is implemented (M2 #5). |
| Human approvals | Application-owned workflows; no built-in approval pause/resume in first release. Required tool/action authorization remains enforced. | Application integration/decision types; protocol tasks are not durable approval workflows. |
| Multi-agent composition | M8+: `Remote[Literal["name"]]` injection, structured `call`, plain `ask`, input-required error; multi-source directory/find, YAML files, 300-second stale-refresh cache, OBO/service identity, deadline/token/depth controls (5 hops), plain Python orchestration, dev CLI/containers. | Detailed extension schema/identity config, source conflicts/card mapping/search pagination, accounting trust and dev config; typed class proxy only later, no initial orchestration helpers. |
| Durable workflows | Revisit after multi-agent usage; no design now. Long work uses native tasks, in-memory default/persistent store pluggable; approvals application-owned. If added, optional separate package (candidate `agentframe-workflows`). | Future evidence/scope; task persistence is not Python crash recovery or durable approval resume. |
| Evaluation | Planned M9 ([evaluation](design/evaluation.md), decided 2026-11-13): an AgentFrame-owned harness for local skills and A2A agents in `agentframe.evaluation` in the main wheel, no new dependency or extra; `Case` from Python/JSONL, plain-function scorers, binary rubric judge, concurrency 4, optional evaluation-wide budget, private-by-default reports, no pytest plugin. | Exact signatures and report fields while building; Foundry export is a later adapter. |
| Model-planned orchestration | Planned M10 ([orchestration](design/orchestration.md), proposed 2026-11-13, open questions pending, #177): a catalog of remote skills from Agent Cards, remote skills as model tools, and a model-made, validated execution plan (flat DAG with data references) run with `asyncio.TaskGroup` and the existing deadline, hop, token, identity and policy controls. Revisits #17's orchestration-helper and dynamic-invocation cuts as opt-in features. | All 18 open questions in the design: eligibility, identity for discovered agents, plan shape/limits, data passing, budgets, failure policy, approval hook, packaging and placement. |
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

As part of the next core multi-agent release (M8+) after M7, a read-only
`AgentRegistry` abstract base with async `get`/`list`, validated
`AgentReference(name, card_url)` / `AgentQuery(names=...)` and explicit
`EnvironmentAgentRegistry` (comma-separated `name=url`) and `FileAgentRegistry`
(YAML `name`/`url` list, PyYAML core dependency) sources is implemented (#83).
`FoundryAgentRegistry(project_endpoint=..., credential=...)` lists a Foundry
project's A2A-enabled agents and maps each to its v1.0 card, fetched with the
source's own Entra token through `AgentRegistry.card_headers` (#86; proven
offline, real-project opt-in test pending). This is agent discovery, not a process-global
plugin registry; model deployments are not agent endpoints.

Implemented (#84): `AgentDirectory(sources=[...])` fetches/validates native
cards with an internal fetcher (no public resolver/cache classes), caches one
credential-free card variant per card URL for 300 seconds, exposes `stale` and
explicit `refresh`, raises `RemoteAgentError` for remote failures and closes
only owned resources. Applications should not write
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
- Host defaults: 32 concurrent runs (excess immediately rejected with HTTP 503
  and `Retry-After: 1`, M7 #72), 1 MiB request (HTTP 413 before
  authentication, no task, M7 #73), 4 MiB response (serialized result part or
  question; over it the task fails with `LimitExceededError`, M7 #73),
  30-second shutdown grace (stop accepting, drain, then cancel, measured from
  the shutdown signal, M7 #74). `/health` is liveness; `/ready` means startup
  checks passed and not shutting down; probe bodies are minimal `{"status":
  ...}` JSON. All are configurable.
- A2A deduplicates the same verified caller and `messageId` for a configurable
  10-minute window, returning the original task (M7 #75:
  `A2AConfig(dedup_window_seconds=600, dedup_store=None)`). In-memory default,
  pluggable multi-instance store; not exactly-once or restart-durable; anonymous
  callers are not deduplicated. Task access is still authorized.

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
the invocation ID and a safe usage snapshot (token counts, estimated flag, model and tool call counts), never `RunResult` internals or provider details;
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
| 7 | Hardening and first usable release (complete) | Prove decided defaults/precedence within ceilings, deadlines/cancellation, DI startup/scoped cleanup, `RunResult` without telemetry, safe correlated errors and identity/resource/policy failures. Prove host admission/byte/shutdown limits, health/readiness and A2A deduplication/caller isolation without exactly-once claims; installed-wheel examples return complete validated results. No built-in approval pause/resume. |

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
M7 hardening completes the first usable release. `EntraIDAuth` validates
Microsoft Entra v2.0 access tokens (signature via the tenant's keys, issuer,
tenant, client-ID audience, expiry, `ver`), maps `roles` and `scp` through
`role_permissions`, fails closed when keys are unavailable and declares an
OpenID Connect card scheme; it needs the optional `entra` extra (#76).
`ExecutionPolicy` (`Runtime(policy=...)` / `AgentApp(policy=...)`) runs
application input checks, model-request checks before every model request
(including routing with `skill=None`, structured output and tool-loop
continuations, once per logical request) and output checks; only an explicit
`True` allows, a denial raises `PermissionDeniedError` and an erroring or
late check `PolicyUnavailableError` (#77). A manually triggered, reviewer-approved
GitHub workflow runs the opt-in Foundry tests with OpenID Connect sign-in and no
stored secret; normal CI still never calls a provider (#81). Runnable examples
in `examples/` cover a model-free agent behind a development API key
(`echo.py`), text and structured output (`assistant.py`), tool calling
(`orders.py`) and built-in routing (`routed_assistant.py`); each model-backed
`create_app(model=None)` defaults to the environment-selected provider.
`examples/smoke.py` serves each one in-process with `ScriptedModel`, calls it
with the official A2A client and checks a complete, validated result; CI's
`build` job runs it against the installed wheel with the `server` extra on
Python 3.13, with no network beyond localhost (#80).

M7 hardening acceptance was proven on **2026-11-12** (#79): host ceilings
(#71), admission (#72), size limits (#73), graceful shutdown and probes (#74),
deduplication (#75), `EntraIDAuth` (#76) and `ExecutionPolicy` (#77, #78) are
merged, and `tests/integration/test_hardening.py` drives every M7 guarantee
offline in CI, through the official A2A client where hosting is involved:
library -> agent -> skill -> run limits capped by `limits_ceiling`; policy
checks, scoped service setup, routing, model calls and retry waits spending
one deadline; local `asyncio` cancellation re-raised unchanged and hosted
`CancelTask`/shutdown ending tasks `CANCELED`, with per-run services closed on
success, failure, timeout and cancel; missing services failing startup;
`RunResult` metadata without tracing; every hosted failure carrying an
invocation ID and safe message, with a log-capture check that no argument
values, prompts or credentials reach callers or logs; API-key, permission,
resource-policy, execution-policy and Entra failures; and admission, byte
limits, probes, drain, deduplication and caller isolation. It added one
fix: every `FAILED` hosted run now logs its invocation ID, error class and safe
message. Local runs never convert `asyncio.CancelledError`; AgentFrame
`CancelledError` only reports a cancelled hosted task (later a remote call).
Skill tracebacks stay in operator logs. A shutdown race it found, harmless with
the in-memory task store, is tracked in #128. With installed-wheel
examples (#80) merged, the M7 build stories are complete. The two checks that
needed the maintainer's own setup passed: the first manual Foundry workflow run
on `main` (#81, fixes in #133) and the opt-in real-token Entra test
(`pytest -m entra`, #76), on **2026-11-13**.

M7 was completed on **2026-11-13** with the first release (#82): AgentFrame
[0.1.0](https://pypi.org/project/agentframe-py/0.1.0/) was published to PyPI
after a TestPyPI rehearsal, via trusted publishing (no stored credentials) from
the GitHub release [v0.1.0](https://github.com/malcmiller/agentframe/releases/tag/v0.1.0)
on a `v*` tag. `.github/workflows/release.yml` published to TestPyPI, ran the
examples smoke runner against a clean install from there, published to PyPI
behind the `pypi` environment's reviewers, then installed from PyPI on Python
3.13, 3.14 and 3.15 (see [releasing](guides/releasing.md)). The release carries
the stability note "Pre-1.0: public APIs may change in minor releases; changes
are listed in the changelog." and the [changelog](../CHANGELOG.md). The PyPI
distribution is `agentframe-py`, since PyPI refused `agentframe` as too similar
to an existing project; the import package and `agentframe` command are
unchanged. The next core release is M8+ multi-agent A2A composition (#17).

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
| A2A | Target protocol **1.0**, using the official `a2a-sdk` Python package. [Latest specification release observed: v1.0.1](https://github.com/a2aproject/A2A/releases/tag/v1.0.1), published 2026-05-28; [SDK observed: 1.3.0, Python >=3.10](https://pypi.org/project/a2a-sdk/1.3.0/). Do not advertise 0.3 support. **M2 (2026-11-12):** pinned `a2a-sdk[http-server]==1.3.0` as a core dependency (hosting is the default interface; the optional `agentframe-py[server]` extra only adds uvicorn for `agentframe run`, M2 #9); the `http-server` extra explicitly supplies Starlette/SSE. Selected binding: **JSON-RPC over HTTP** (`JSONRPC`, protocol version `1.0`), the most widely supported 1.0 binding. The SDK's 0.3 compatibility mode stays disabled, so requests without `A2A-Version: 1.0` are rejected; gRPC and HTTP+JSON/REST are not offered. `tests/unit/hosting/test_a2a_binding.py` and the CI wheel check prove the supported install. Native type mappings and independent official-client interoperability with AgentFrame hosting are proven end to end in `tests/integration/test_interoperability.py` (M2 #10). | Upgrade the SDK only deliberately, with the binding tests. Selecting JSON-RPC does not select every optional capability. |
| Foundry model inference | Use OpenAI v1 **Responses API**, base URL `https://<resource>.openai.azure.com/openai/v1/`, via official `openai`. Inject either an API key or Microsoft Entra ID token provider (`azure-identity` `get_bearer_token_provider`, scope `https://ai.azure.com/.default`). The [Azure AI Inference SDK is retired](https://learn.microsoft.com/en-us/azure/foundry/how-to/model-inference-to-openai-migration); do not use it. | Keep `openai`/`azure-identity` optional provider extras; pin compatible dependencies and prove M3 deployment/capability/auth and client-lifecycle behavior. AgentFrame constructor/credential wrapping remains proposed. |
| Foundry discovery (M8+) | Choose the project agents API through `azure-ai-projects` **2.x** ([2.10.0 observed](https://pypi.org/project/azure-ai-projects/2.10.0/)), project endpoint `https://{account}.services.ai.azure.com/api/projects/{project}`. [Incoming A2A documentation](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint) shows prompt agents enabling A2A with `agent_card` and protocol configuration, publishing cards and accepting A2A requests. Select GA protocol 1.0 explicitly. | Implemented (#86) with `azure-ai-projects==2.11.0` in the `foundry` extra: every page is read, `agent_endpoint.protocol_configuration.a2a` marks A2A-enabled agents, each maps to its v1.0 card. This is read discovery of existing agents, not deployment of AgentFrame agents to Foundry. |

Foundry's documented card route for enabled agents ends in
`/agents/{agent}/endpoint/protocols/a2a/agentCard/v1.0`; its existence is not a
substitute for validating each listed agent's enabled endpoint. Enabling incoming
A2A requires Foundry User or higher; authenticated card/invocation access uses
Foundry Agent Consumer or another role granting endpoint access. Listing agents
needs the `agents/read` data action, which Foundry Agent Consumer lacks;
Foundry User is the least-privileged built-in role for the registry identity,
to be verified with the opt-in test. Foundry's
[outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)
are a possible additional reference source, not a selected registry contract.

### Next core release: multi-agent A2A composition (M8+)

This is the next core release after M7, not an optional discovery follow-up.
Keep M0-M7 as the single-agent release, with single-agent `run` in M2.
The M8+ authoring contracts below are decided, while unspecified wire/configuration
details and finer milestone breakdown remain open.

| Capability | Planned direction and acceptance |
| --- | --- |
| Discovery and remote invocation | `AgentDirectory(sources=[...])` with YAML `FileAgentRegistry("agents.yaml")`, Foundry endpoint/credential source, explicit environment source and custom get/list. `AgentApp(directory=...)` validates `Remote[Literal["name"]]` at startup. Native card-backed `find(skill=... / tags=...)`, 300-second cache/stale refresh, explicit stale opt-in, ownership/isolation/destination safety. `call("skill", ..., output=Type)` uses structured A2A; `ask(text)` works with any compatible peer. Both wait within deadline; input-required raises `ClarificationNeededError`, no automatic dialogue. Implemented: `Remote[Literal["name"]]`, `directory=` and `ask` (#87); `call` (#88); deadline and hop-depth propagation with `RunLimits.max_remote_hops` (#89); per-call token ceilings and reported-usage reconciliation (#90); root/parent invocation ID log correlation and unchanged W3C trace context forwarding (#91); interoperability with a non-AgentFrame A2A client and server, the official SDK in CI and a Foundry agent opt-in (#94). |
| Orchestrator agents | Ordinary skills with plain `await`, `asyncio.TaskGroup` and directory search. No orchestration helpers in the first multi-agent release; later only after repeated usage. Planned M10 revisits this cut as opt-in [model-planned orchestration](design/orchestration.md) (#177). Optional `Remote[ResearchAgent]` typed proxy later uses the same structured wire request, not proprietary RPC. |
| Cross-agent context | Per-target Entra `on_behalf_of` default or explicit `service` (implemented as `remotes={name: Service(credential=...)}`, #92, and `OnBehalfOf(scope=...)` with `EntraIDAuth(client_id=..., client_credential=...)`, #93); API-key/anonymous ingress service only, never raw token forwarding or identity fallback on exchange failure. Propagate remaining deadline (receiver min with own timeout), token ceiling (default caller remaining; receiver only lowers), actual peer usage charged once, max 5 hops and trace/correlation. Shared concurrent reservations; other-peer usage unknown. Same A2A extension, exact schema/config open; each target enforces policy and no hard billing guarantee. |
| Development and deployment | `agentframe.yaml` lists named module paths/ports. `agentframe dev` starts localhost agents, writes local registry, configures development API keys and combines agent/invocation-tagged logs. Implemented (#95): `agents: {name: {app: module:create_app, port: int}}`, one `agentframe run` process per agent on `127.0.0.1`, `.agentframe/agents.yaml`, random per-session keys passed only through child environments, `dev_settings()`, optional `AGENTFRAME_DEV_CLIENT_KEY` caller `developer`. One container per agent. Implemented (#96): `agentframe compose` writes a development `compose.yaml` (service-name `AGENTFRAME_PUBLIC_URL`/`AGENTFRAME_AGENTS`, `AGENTFRAME_DEV=1` plus `AGENTFRAME_ALLOW_HTTP_HOSTS` opt-in, `${VAR}` key references only, `127.0.0.1` ports, `/ready` healthchecks) and, if missing, a digest-pinned non-root `Dockerfile`; `--force` overwrites only `compose.yaml`. Kubernetes/Foundry templates later, not infrastructure management. |

Release acceptance: deploy two or more AgentFrame agents independently; an
ordinary orchestrator discovers and calls a specialist over A2A with identity,
budget and trace/correlation propagation. Prove interoperability with a
non-AgentFrame A2A client and server, not just AgentFrame-to-AgentFrame calls.
Foundry discovery uses the chosen projects agents API, but release acceptance
must prove A2A-enabled filtering, card mapping, authentication/RBAC and pagination;
model deployments are not agent endpoints.

**Status (2026-11-13):** the M8+ build stories (#83-#97) are complete on
`main`; no multi-agent release has been published yet, and 0.1.0 remains the
latest release. `tests/integration/test_multi_agent_release.py` (#97) runs
the [`examples/multi_agent`](../examples/multi_agent) planner and research
agent as separate `agentframe run` processes and calls the planner with the
official A2A client: discovery by name, a validated structured `call`, a
free-text `ask`, `TaskGroup` fan-out with explicit per-call
`max_total_tokens`, service identity, the propagated deadline, the research
agent's usage charged to the planner once, the same root invocation ID in
both processes' logs, a recursive configuration failing at 5 hops and a
plain SDK peer receiving the caller's `traceparent` unchanged. In CI, identity
is service mode; `test_multi_agent_interop.py` proves both interop directions
with official-SDK peers.

Pending the maintainer's runs against real resources (opt-in tests written,
not yet run):

- Foundry agent registry, invocation and interop: `test_foundry_agents.py`
  (`AGENTFRAME_TEST_FOUNDRY_AGENTS=1`), which proves A2A-enabled filtering and
  card mapping against a real project.
- Entra on-behalf-of with two app registrations: the opt-in `entra` test in
  `test_on_behalf_of.py`.
- Containers: `test_compose_containers.py` (`AGENTFRAME_TEST_DOCKER=1`).

No durable workflow engine, built-in approval workflow, federation or deployment
service is included. Checkpointed multistep flows, pause/resume and crash
recovery are revisited only after multi-agent real usage. If added, use an
optional separate package (candidate `agentframe-workflows`). Long work uses
A2A tasks with an in-memory default and pluggable persistent store; no built-in
approval workflow or promise of recovering skill execution from persisted tasks.

### M9: Evaluation (planned)

Planned, not built (#164). The design and its decisions, confirmed on
2026-11-13, are in [evaluation](design/evaluation.md). M9 follows the M8+
release and has no imposed order relative to the optional adapters below.

| Capability | Decided direction |
| --- | --- |
| Cases and datasets | Typed `Case` (`id`, exactly one of `arguments` or `text`, optional `expected`, `tags`) from Python or strict, size-bounded JSONL. |
| Targets | A local skill through `Runtime.run`, or any A2A 1.0 agent (AgentFrame, non-AgentFrame, Foundry) through the existing `Remote` binding: `call` for a named skill, `ask` for free text. |
| Scorers | Plain sync/async functions returning `bool` or `Score`; built-in `equals`, `contains`, `valid(T)` and a rubric `Judge` using any AgentFrame `Model` with a typed verdict. |
| Controls | Existing `RunLimits`, deadlines and token ceilings per case; bounded concurrency, repeats, optional evaluation-wide token budget and timeout. |
| Reports | Pass rate, per-scorer/per-tag rates, tokens (unknown never zero), latency, call counts and errors by class from `RunResult`; console summary, versioned JSON and `report.require(...)` for pytest/CI. No prompts or outputs unless `capture_content=True`. |

Release acceptance: evaluate a model-free skill and a `ScriptedModel`
skill in CI, a real Ollama and Foundry skill with a judge in opt-in tests, and a
non-AgentFrame A2A agent over A2A, gating a build on a JSON report threshold.
No new runtime dependency, hosted service, dataset store, trajectory scoring or
pytest plugin. Foundry evaluation export is a later optional adapter.

### M10: Model-planned orchestration (planned)

Planned, not built (#177); open questions await confirmation. The design is in
[orchestration](design/orchestration.md). It deliberately revisits two M8+ scope
cuts from #17, "orchestration helpers" and "dynamic invocation of agents found
at run time" (also #85 decision 2), as opt-in features; plain `await` and
`asyncio.TaskGroup` stay the default, and every other cut (durable workflows,
pause/resume, built-in approval, typed proxies) is kept. M10 starts after the
M8+ release and can be built in parallel with M9; only its acceptance story
uses M9's harness.

| Capability | Proposed direction |
| --- | --- |
| Catalog | `ctx.remote_skills(...)` builds `RemoteSkill` entries from declared remotes' Agent Cards: card skills with skill-call extension schemas, or one free-text `ask` entry for other A2A agents; bounded untrusted card text, at most 20 entries. Discovered agents only behind an application allowlist with per-name identity. |
| Remote skills as tools | `ctx.generate(goal, tools=[*skills])` lets the model call remote skills through the M5 loop; each call is a tool call, never retried, with the existing remote guarantees. |
| Plans | `ctx.plan(goal, skills)` makes one structured call returning a typed `Plan` (flat DAG, `{"$ref": "step.field"}` references), checked against the catalog and limits before anything runs. |
| Running plans | `ctx.run_plan(plan, skills)` runs ready steps in parallel (default 4) under `asyncio.TaskGroup` within the run's deadline, hops and token budget; `stop` or opt-in `continue` failure policy; per-step logs and usage. |
| Safety | Per-entry permissions, existing `tool_checks` per call and new `plan_checks` before execution; human approval stays application-owned; no automatic replanning. |

Release acceptance: separately deployed AgentFrame agents and a non-AgentFrame
A2A agent; an orchestrator calls remote skills as model tools and plans and runs
a parallel-plus-dependent plan within the caller's limits with correlated logs
and per-step usage; unsafe plans are denied before sending. CI uses
`ScriptedModel`; opt-in Ollama and Foundry tests and an M9 planner evaluation.
No new runtime dependency, workflow engine, durable plans or new wire fields.

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
- Host probe bodies; retry configuration/
  backoff/transient codes. Protection defaults, no-side-effect replay and the
  dedupe store/window API (M7 #75) are decided.
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
  authenticator/resource-policy methods and API-key entry format.
  `ExecutionPolicy` tool checks are implemented (M7 #78).
  `auth`, built-in authenticators, `role_permissions` and explicit
  `AllowAnonymous()` are decided; `EntraIDAuth` maps roles and scopes into one
  `role_permissions` namespace (M7 #76).
- Free-text routing beyond one task's clarification: durable or cross-task
  follow-ups and multi-skill orchestration. The `SkillRouter` protocol,
  `ModelRouter`, validated argument extraction and the `INPUT_REQUIRED`
  clarification contract are implemented (M6 #67-#69); structured requests use
  the native A2A extension, not a standardized base skill field.
- YAML name/URL schema details, environment encoding, source conflict/order,
  card enrichment bounds, resolver
  auth/destination interfaces, source reconciliation, cache isolation/stale-age
  bounds and ownership details, Foundry mapping/RBAC/pagination. `sources`,
  `directory=`, `find`, TTL 300/on-stale refresh and named-remote validation decided.
- Shared extension URI/schema for structured calls, deadline/token/usage/depth;
  root/hop/clock semantics, atomic reservations and descendant usage trust/
  reconciliation, per-target identity config and result/argument-reserved-name
  collisions. `Remote.call` / `ask`, clarification error and max 5 hops decided.
  CLI config/loading/key lifecycle details remain open;
  no first-M8+ orchestration helpers (planned M10 revisits this, with its own
  open questions in [orchestration](design/orchestration.md#open-questions)).
  Typed class proxies/templates later.
- Future runtime streaming API, AG-UI version/transport/native event projection,
  typed-result serialization, buffer/write/backpressure/shutdown details.
  `AGUIConfig`, `ag_ui=`, `/ag-ui`, first profile/disconnect cancellation/no
  reconnect are decided. Frontend tools/state only later opt-in; workflows
  revisited after multi-agent usage in a separate optional package if added.

Resolve these with small executable examples before declaring a stable API.
