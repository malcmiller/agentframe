# AgentFrame repository context

## Purpose and current status

AgentFrame is an independent Python library for annotation-based agent creation:
classes implement behavior, decorators describe agents and skills, and types
define validated contracts. A2A and native Agent Cards are first-class.
The goal is a default way to build 1 to N independently deployable specialist and
orchestrator agents, interoperable with other frameworks' A2A agents. Deliver
single-agent M0-M7 first, then A2A multi-agent composition as the next core
release (M8+), not an optional add-on or a hosting platform.

This repository contains the M0 development tooling, M1 model-free
authoring/local `Runtime`, M2 native A2A hosting, M3 Ollama/Foundry text
generation, M4 typed structured outputs, M5 bounded tool calling, M6 built-in
opt-in model routing and M7 hardening alongside design documentation. Version
0.1.0, the first usable single-agent release, is published on PyPI as
`agentframe-py` (import `agentframe`). Pre-1.0 there is no stable API. The
M8+ multi-agent stories (#83-#97) are built on `main` but not yet released;
later features (MCP, telemetry, streaming) are not implemented. Keep design API examples beyond
the implemented surface labeled as unimplemented; explicitly approved
names/defaults are decisions, not still-unresolved recommendations. Do not
scaffold implementation or add dependencies merely to make design snippets
executable.

## Code quality and readability

**Top priority for every contributor and coding agent: human-readable code.**

- Make code easy for a human maintainer to read and understand. Readability
  beats cleverness, brevity and premature performance optimization.
- Choose the simplest design that meets the documented contract. Do not add
  speculative abstractions, extension points, generic frameworks, metaclass/
  descriptor magic, deep inheritance or indirection without a current concrete
  need. Three similar lines beat a premature abstraction.
- Keep modules and functions small and focused. Use clear, descriptive names;
  prefer explicit over implicit, flat over nested, and early returns.
- Fully type public APIs and document them with concise docstrings covering
  purpose, arguments, returns and raised errors as applicable. Comments explain
  why, not what the code already says.
- Avoid monkeypatching, import-time side effects, global registries and heavy
  decorator stacking unless the documented design requires them. Keep required
  mechanisms such as `@agent` / `@skill` metadata small and explain non-obvious
  implementation choices.
- Keep diffs small, focused and reviewable by a human; no unrelated refactors.
  When choosing between designs, pick the one a new contributor understands faster.

### Python practices and M0 tooling

- Follow Python best practices and PEP 8 conventions, with an approved line
  length of **88**. Use **Ruff** for Black-compatible formatting (`ruff format`)
  and linting (`ruff check`), replacing separate Black, flake8 and isort tools.
- Use the modest Ruff rule set configured in `pyproject.toml`: `E`, `W`, `F`, `I`,
  `B`, `UP`, `SIM`, `N`, `C4` and `RUF`. Do not enable noisy rules that force
  contorted code. Ruff targets Python files, not unimplemented Markdown examples.
- Use **mypy**, strict on the `agentframe` package, with full public API hints.
- Use **pytest** with **pytest-asyncio in auto mode** for async tests. Make tests
  readable as usage examples.
- Target the approved Python 3.13+ matrix, testing 3.13, 3.14 and 3.15.
  `pyproject.toml` pins development tools and hatchling; commit `uv.lock`.
- CI runs format checks (`ruff format --check`), lint, type checks and tests.
  Pre-commit runs Ruff and mypy; tests stay in CI rather than on every commit.

Use uv 0.13.1 (also pinned in CI). From a fresh clone, `uv sync` creates `.venv`,
installs the package and development tools, and downloads Python if needed:

```bash
uv sync
uv run ruff format
uv run ruff check --fix
uv run mypy
uv run pytest
uv run pre-commit install
uv run pre-commit run --all-files
uv build
```

Before committing, all four checks must pass: `uv run ruff format --check`,
`uv run ruff check`, `uv run mypy` and `uv run pytest`. Run the matrix locally
with `uv run --python 3.13 pytest` (repeat for 3.14 and 3.15).
In PyCharm, select `.venv/bin/python`, mark `src` as Sources Root and `tests`
as Test Sources Root, and use pytest as the test runner.

### Repository layout

Folders stay one level deep. Put new code in the group that owns the concept:

| Path | Contents |
| --- | --- |
| `src/agentframe/__init__.py` | Public API re-exports; every public name is importable from `agentframe`. |
| `src/agentframe/` (root) | `errors.py`, `testing.py` (`ScriptedModel`), `cli.py` (`agentframe run` / `dev` / `compose`), `project.py` (`agentframe.yaml`), `dev.py` (`dev_settings()`), `dev_server.py` (`agentframe dev` supervisor), `compose.py` (`agentframe compose` generator). |
| `src/agentframe/authoring/` | `Agent`, `@agent` / `@skill`, definitions, `RunContext`, `Inject` / `Services`. |
| `src/agentframe/runtime/` | `Runtime`, generation, `RunLimits`, `RunOptions`, `Usage`. |
| `src/agentframe/models/` | `base.py` model protocol and settings; provider adapters `ollama.py`, `foundry.py`; `strict_schema.py` (OpenAI strict JSON-schema converter). |
| `src/agentframe/hosting/` | `AgentApp`, `HostConfig`, Agent Card, A2A executor, routing. |
| `src/agentframe/security/` | `Principal`, authentication, authorization. |
| `src/agentframe/discovery/` | `AgentRegistry`, `AgentReference`, `AgentQuery`, file and environment registries (M8+). |
| `tests/unit/<group>/` | Unit tests mirroring the source groups; root-module tests in `tests/unit/`. |
| `tests/integration/` | Interoperability, real providers, success criterion, README example. |
| `tests/support/` | Shared helpers, on pytest's `pythonpath` (`from local_server import ...`). |
| `examples/` | Runnable examples, each with `create_app()`; `smoke.py` checks them all against the installed wheel in CI. |
| `docs/roadmap.md` | Product boundaries, decisions and milestones. |
| `docs/guides/` | How to use AgentFrame and how its behavior works. |
| `docs/design/` | Rationale and proposed designs. |
| `docs/api/` | Public surface reference. |

Keep subpackage `__init__.py` files to a short docstring; add public exports to
the top-level `__init__.py` only. Import internal modules by full path, for
example `from agentframe.runtime.limits import RunLimits`. Keep test basenames
unique, since test folders are not packages.

### Real-provider tests (opt-in, local only)

`tests/integration/test_providers.py` runs one agent against a real Ollama
server and a real Foundry deployment: text generation with usage, a tiny
`max_output_tokens` raising `IncompleteOutputError` (text and structured), a
clear error for an unknown model/deployment, a hosted structured call through
the official A2A client, a validated typed `output=` result and the example's
`summarize` skill returning a typed data part over A2A, a one-tool round trip
run locally and through `examples/orders.py` over A2A (skipped if the model
lacks tool support), a Foundry one-tool round trip that replays encrypted
reasoning items and one unambiguous free-text request routed by
`examples/routed_assistant.py` over A2A (checking only the selected skill and
that its argument is present, since routing is nondeterministic). They are marked
`ollama` / `foundry` and skipped unless enabled, so
default `uv run pytest` and CI never call a provider. The manually triggered
`.github/workflows/foundry.yml` runs the `foundry` tests from GitHub behind the
reviewer-approved `foundry` environment, signing in with OpenID Connect (no
secrets); its one-time setup is in README.md "Real-provider tests" (M7 #81).
Endpoints and model names come only from environment variables:

| Variable | Needed | Meaning |
| --- | --- | --- |
| `AGENTFRAME_TEST_OLLAMA=1` | Ollama | Enables the `ollama` tests. |
| `AGENTFRAME_OLLAMA_MODEL` | Ollama | Installed model tag, for example `llama3.2:3b` (never pulled). |
| `AGENTFRAME_OLLAMA_ENDPOINT` | Optional | Server URL; defaults to `OllamaModel`'s `http://localhost:11434`. |
| `AGENTFRAME_TEST_FOUNDRY=1` | Foundry | Enables the `foundry` tests. |
| `AGENTFRAME_FOUNDRY_ENDPOINT` | Foundry | OpenAI v1 base URL, `https://<resource>.openai.azure.com/openai/v1/`. |
| `AGENTFRAME_FOUNDRY_DEPLOYMENT` | Foundry | Deployment name. |

Foundry signs in with the Azure CLI credential (run `az login`; no API key).
Foundry calls cost money, so prompts and caps are tiny; run them sparingly.

```bash
AGENTFRAME_TEST_OLLAMA=1 AGENTFRAME_OLLAMA_MODEL=llama3.2:3b uv run pytest -m ollama
AGENTFRAME_TEST_FOUNDRY=1 AGENTFRAME_FOUNDRY_ENDPOINT=<base-url> \
  AGENTFRAME_FOUNDRY_DEPLOYMENT=<deployment> uv run pytest -m foundry
```

An enabled provider with a missing required variable fails rather than skips.

`tests/integration/test_foundry_agents.py` has opt-in `foundry` tests for
`FoundryAgentRegistry` (#86): it lists a real project with one A2A-enabled
agent and fetches that agent's card, read-only. It runs only with
`AGENTFRAME_TEST_FOUNDRY_AGENTS=1`, `AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT`
(`https://<account>.services.ai.azure.com/api/projects/<project>`) and
`AGENTFRAME_FOUNDRY_A2A_AGENT` (the agent's name), after `az login`. Setup and
roles are in `docs/api/discovery.md#foundryagentregistry`. The same agent is the
real non-AgentFrame server for #94: one test `ask`s it once, another checks
`call` is refused before sending. In CI, `test_multi_agent_interop.py` proves
both directions against a plain SDK peer (`tests/support/a2a_peer.py`).

`tests/integration/test_entra_interoperability.py` has one opt-in `entra` test
using a real token from `az account get-access-token`, enabled with
`AGENTFRAME_TEST_ENTRA=1`, `AGENTFRAME_ENTRA_TENANT_ID` and
`AGENTFRAME_ENTRA_AUDIENCE` (the API's client ID; optional
`AGENTFRAME_ENTRA_RESOURCE`, default `api://<audience>`). It needs an existing
app registration; setup is in `docs/api/authorization.md#entraidauth`.

Do not implement M1+ features or add runtime dependencies just to execute design
snippets.

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
The official MCP Python SDK and OpenTelemetry are permitted optional
protocol/observability dependencies; they do not justify adding an agent framework.
For future streaming, the official AG-UI Python protocol SDK is an appropriate
optional dependency to evaluate; do not add a UI framework or another agent
framework to obtain AG-UI support. No AG-UI dependency/version is selected yet.

Platform/API choices approved on 2026-11-11: support Python 3.13+ and test 3.13,
3.14 and 3.15; Python has no LTS designation. Target A2A protocol 1.0 via the
official `a2a-sdk` (specification release v1.0.1). M2 pins the core dependency
`a2a-sdk[http-server]==1.3.0` and selects JSON-RPC over HTTP, protocol 1.0 only
(0.3 compatibility disabled). Pin other packages exactly when introduced, not
from observed-version snapshots; no provider compatibility testing has occurred. Sources and remaining implementation
decisions are in `docs/roadmap.md#resolved-m0-platform-and-api-decisions`.

## Authoring decisions

- Use `@agent` for identity and `@skill` for advertised skills.
- Implement behavior in an `Agent` subclass; the common path needs no additional
  AgentFrame subclass.
- Give each skill its own async `@skill` method. Require at least one valid
  declared or inherited skill, not an abstract `handle()` or author-written
  dispatch method. Do not revert examples to the older single-handler design.
- Use Python types, Pydantic data models, and `Annotated`/`Field` for contracts.
- Use `@tool` plus explicit registration for optional model-callable functions.
  Bounded tool execution/results and generation continuations are first-release
  scope, optional per agent. Tools and A2A skills are different concepts.
- Put instructions and execution settings in typed configuration, with an
  optional trusted callable for dynamic instructions.
- Inject models, state stores, policies, retrieval dependencies, and hooks.
  Custom components may implement structural protocols without inheritance.
- Keep `RunContext` library-managed and invocation-scoped.
- Every skill accepts `ctx: RunContext`; model-free skills may leave it unused.
  Models are optional dependencies, and a class may mix model-free and
  model-backed skills. Generation without a configured model fails explicitly.
- Combine shared agent instructions with trusted skill-specific generation
  instructions while keeping user text separate. Instructions are not policy.
- Supply default bounded execution in AgentFrame, not another framework.
- Use one `ctx.generate`: text returns `str`, `output=Summary` returns a validated
  Pydantic instance, `tools=[...]` selects explicitly registered tools.
  AgentFrame owns normalized messages/tool calls/results/usage/finish reasons.
  Unsupported requested capability fails before sending; refusal raises
  `ModelRefusalError`, truncation `IncompleteOutputError` by default. Partial
  text opt-in: a skill catches `IncompleteOutputError` and reads `.partial_text`
  (text path only; `None` for structured output; M4 #54). Message/tool-call
  layouts are settled (M5 #59).
- Annotate service parameters `Inject[T]`; register with `Services.add(T, instance)`
  or `add_scoped(T, factory)`, passed as `services=` on `AgentApp` / `Runtime`.
  Exclude injected params from caller/card/MCP schemas; missing services fail
  startup. App owns/closes singletons; AgentFrame closes only scoped instances
  it created. Factory/disposal interfaces remain open; do not use context bags.
- Generate native Agent Cards from metadata plus actual host configuration.
- Compose A2A hosting through `AgentApp`; do not require server subclassing.

The complete piece-to-code decision table and examples live in
`docs/design/base-classes.md`. Approved defaults/names are documented decisions;
unspecified signatures/extension interfaces still need implementation evidence.

## A2A and model boundaries

`Runtime.invoke` selects registered skill IDs explicitly for local calls and
applies validation, policy, and execution limits. Direct Python method calls
bypass that pipeline. Use one server/shared runtime and an endpoint per caller
audience, not per skill: A2A for agents, optional MCP for LLM apps/tools and
AG-UI later for frontends. All entry points share authorization, validation,
limits, accounting and tracing.
`invoke` returns a validated value; `Runtime.run` returns `RunResult` with
value, invocation ID, usage, model/tool calls, duration and finish reason even
without tracing. Usage marks completeness/estimation; unknown is not zero.
Ordinary remote A2A gets the invocation ID and a safe usage snapshot (token counts, estimated flag, model and tool call counts) (M3 #36), never `RunResult` internals or provider details.
The negotiated M8+ extension additionally reports actual usage for accounting,
not `RunResult` or raw provider details; ordinary callers keep that snapshot only.
Exact usage marker fields, duration unit and failed-run access remain open.

Prefer structured A2A requests naming a skill with JSON arguments in a native
data part under an Agent Card-declared, caller-activated A2A 1.0 extension.
They bypass routing/model-selection cost, not runtime checks or selected-skill
inference cost. Unknown skills/invalid arguments fail explicitly; no silent
downgrade to prompts. Implemented wire format (M2 #5): activate the card's
skill-call URI via `A2A-Extensions` (echoed in the response), skill ID in message
metadata `agentframe.skill`, exactly one JSON-object data part; malformed ->
JSON-RPC InvalidParams, no task. Not a standard A2A `skill` field.
Implemented (#89): the same URI carries caller context; an activated message
without `agentframe.skill` is free text. Cards list `params.context`
(`deadline_ms`, `hops`, `max_total_tokens` since #90); senders send only
listed fields (0.1.0 lists none).
Receiver timeout = min(own, `agentframe.deadline_ms`); `RunContext.hops`;
`RunLimits.max_remote_hops=5`: caller fails before sending, deeper request
REJECTED with base `LimitExceededError` message; depth is cooperative.
Implemented (#91): context fields `root_invocation_id`/`parent_invocation_id`
(lowercase UUID strings; invalid -> ignored, new root, never echoed or an
error). Hosted runs log `Run started`/`Run completed` (INFO) with `extra`
`agent`, `invocation_id`, `root_invocation_id`, `parent_invocation_id`; no
log format set; root not exposed on `RunContext`. Valid W3C `traceparent`
(version 00) plus valid `tracestate` forwarded unchanged as headers to every
peer, invalid dropped; no spans or trace IDs created. Never authentication.
Free-text/extension-unaware callers use custom or opt-in model routing plus
validated extraction. Ambiguity/missing arguments asks back through
`TASK_STATE_INPUT_REQUIRED`, never guesses; the caller answers in the same task,
which is routed again with the whole conversation (M6).
This is not human approval or durable workflow resumption. Sources are in
`docs/design/mcp-skills.md#a2a-request-styles-and-extension-boundary`.
Multiple-skill orchestration is not automatically enabled.
Built-in opt-in model routing is implemented (M6): `AgentApp(router=
ModelRouter(model))` selects an eligible skill, then extracts and validates its
arguments, or asks a question; routing calls are charged to the run before the
skill runs. Custom `SkillRouter`s implement `route(conversation, skills, ctx)`.
Never enable routing from agent model injection
(`examples/routed_assistant.py`).

An A2A skill is descriptive metadata, not a Python-method RPC endpoint. Messages,
tasks, artifacts, and actual endpoint capabilities follow the selected official
protocol/SDK version. A card's security declaration does not enforce security.
Do not advertise features the host does not implement.

Initial models are GPT deployments through Microsoft Foundry inference and
locally executed Llama through Ollama. Llama is a model family, not a host.
Foundry uses OpenAI v1 Responses at
`https://<resource>.openai.azure.com/openai/v1/` through official `openai`.
Inject Entra token-provider authentication (`azure-identity`
`get_bearer_token_provider`, scope `https://ai.azure.com/.default`) or an API key;
keep these packages optional provider extras. Do not use the retired Azure AI
Inference SDK. Final AgentFrame credential wrapping/constructor types stay open.
Foundry inference is not Foundry-hosted agent deployment. Keep provider imports
out of agent business logic. Switching providers should change configuration,
not skill implementations. Structured skill selection uses the declared and
activated extension; its schema remains open, not base-standard A2A dispatch.

## Limits, tracing, and MCP decisions

- Keep per-request `ModelSettings.max_output_tokens` separate from `RunLimits`
  for assembled input, aggregate invocation tokens, model/tool calls and time.
  Canonical output default is 4096 on `ModelSettings`, never duplicated.
  `RunLimits` defaults: timeout 120 seconds, total tokens 100000, model calls 10,
  tool calls 20, input cap model-reported context size. Input plus reserved
  output must still fit capacity; unknown is not unlimited and overflow fails.
  Model context capacity is not an arbitrary user setting.
- Resolve library → agent → skill → per-run within host ceilings.
  Merge field by field: only explicitly set fields override (M3 #42). Skills use
  `@skill(limits=..., model_settings=...)`; local runs `options=RunOptions(...)`.
- Retry transient model rate-limit/timeout/server failures only, default two
  retries/backoff, one layer with provider SDK retries disabled. Every attempt
  consumes existing budgets/deadline. `@tool(side_effects=True)` never auto-retries;
  `idempotent=True` is not a blanket retry grant; both flags default False and
  may combine (M5 #58). No tool is ever auto-retried and model retries never
  replay tools; repeated model-requested calls are not blocked (M5 #63).
- A2A same verified caller + `messageId` within default 10 minutes returns
  original task (M7 #75): `AgentApp(a2a=A2AConfig(dedup_window_seconds=600,
  dedup_store=None))`, atomic `MessageDedupStore.put_if_absent`; in-memory
  default, not exactly-once. Order size -> auth -> dedupe -> admission (a hit
  takes no slot); anonymous not deduplicated; answers deduplicated; reused ID
  with different parts/metadata (SHA-256) -> InvalidParams.
- `HostConfig` defaults: max_concurrent_runs=32 (immediate HTTP 503 +
  `Retry-After: 1` before the SDK, no task; INPUT_REQUIRED holds no slot; M7 #72),
  max_request_bytes=1048576 (HTTP 413 before auth, no task; M7 #73),
  max_response_bytes=4194304 (serialized result part/question; over it the task
  fails with base `LimitExceededError`, never truncated; M7 #73),
  shutdown_grace_seconds=30 (stop accepting, drain, cancel; measured from the
  signal, `agentframe run` starts it in Uvicorn's handler; M7 #74). `/health` is
  liveness; `/ready` means startup checks passed and not shutting down; minimal
  `{"status": ...}` bodies, fixed paths.
- Enforce host ceilings independently of defaults and permitted tighter run
  options. Preflight/reserve budget and reconcile reported usage, including
  retries. Unknown usage is not zero; strict accounting cannot assume a bound.
- Budget model-backed routing before skill execution and propagate remaining
  allowance. Do not promise hard billing caps or free cancellation.
- Configure optional OpenTelemetry through `Runtime` and `TracingConfig`.
  The application owns the provider, exporters, and sampling. Content capture
  is off by default; credentials and private reasoning are never captured.
- Keep invocation IDs, token accounting, and enforcement working without
  tracing. Incoming trace context and session IDs are not authentication.
- A2A remains first-class. Optional `MCPConfig(exposed_skills=[...])` publishes
  only explicitly allowlisted registered skills through native MCP tool calls.
  It does not expose internal `@tool` functions or authorize callers.
  The allowlist is empty by default. Tool-like deterministic/typed/quick/
  single-shot skills may also use MCP; judgment/multi-turn/clarification or
  long-running skills usually stay A2A-only. This is guidance, not an enforced rule.
- MCP named selection bypasses A2A routing, not validation, policy, limits, or
  tracing. Keep discovery and invocation authorization consistent.
- Use `A2AConfig` and `MCPConfig` for adapter settings; `HostConfig` owns the
  advertised origin, authentication integration, and hard limit ceilings.
- Defer a custom REST skill API unless a concrete client requires it. Do not
  invent MCP/A2A payload fields, response envelopes, or unverified capabilities.

See `docs/guides/limits.md`, `docs/design/tracing.md`, and
`docs/design/mcp-skills.md` for proposed settings, deployed server examples,
accounting caveats, and protocol boundaries.

## Authorization and prompt-safeguarding decisions

- Add all-of `requires_permissions` declarations to skills, not organization-
  specific role names. Applications own role mappings, identity directories,
  trusted grant resolution, and resource policy.
- Put immutable verified caller identity in `RunContext.principal`. Never trust
  roles, grants, principals, tenant ownership, or context from request arguments.
  Protected local calls also need trusted caller context; Python is not a sandbox.
- Inject `AuthorizationPolicy` through `Runtime(authorization=...)`. Enforce
  mandatory skill requirements plus resource checks, not just prompt/filter
  advice. Missing identity/integration, denial, timeout, or policy outage must
  fail protected work.
- Empty permission requirements, MCP allowlists, discovery visibility, and
  loopback binding do not declare anonymous access or grant authorization.
  Anonymous remote access must be explicit; unprotected local examples need no
  identity provider.
- Use `HostConfig(auth=...)`, not `authentication=`. `Authenticator` has
  `EntraIDAuth(tenant_id=..., audience=...)`, `APIKeyAuth(keys=...)`,
  explicit `AllowAnonymous()` and custom implementations. No remote auth
  choice means startup refusal. `role_permissions` maps verified roles/scopes.
  The protocol method is `async authenticate(headers) -> VerifiedCaller | None`
  (M2 #4); API keys use `APIKey(caller_id=..., roles=[...])` and the
  `X-API-Key` header; permissions are the union of mapped roles.
  `EntraIDAuth` (M7 #76, `entra` extra) accepts v2.0 tokens only; `audience` is the API's client ID GUID (`api://`
  refused); `oid` is the caller; `roles` and `scp` share the `role_permissions`
  namespace; unavailable signing keys fail closed (401 plus warning).
  Do not rename separate outbound resolver `authentication=` settings.
- Restrict routing candidates, re-check invocation and protected task access,
  and enforce consequential tool/resource decisions at the action boundary.
  Delegation cannot amplify caller authority through broad server credentials.
- Prompt safeguarding is shared responsibility. Keep trusted instructions
  separate from untrusted user/retrieval/tool content; never store secrets in
  prompts or promise a universal injection detector.
- Optional `ExecutionPolicy` hooks cover input, each outbound model request,
  tools/actions, and final output. Required checks fail closed and cannot waive
  authorization. External screening requires explicit egress approval.
  Implemented (M7 #77): `Runtime(policy=ExecutionPolicy(...))` / `AgentApp(policy=)`
  with input, model-request and output checks, each an async
  `(skill, principal, content) -> bool`; only `True` allows; model-request
  checks run once per logical request in the generation layer (routing gets
  `skill=None`). Tool checks (M7 #78) are async
  `(skill, principal, tool_name, arguments) -> bool`, run after tool
  authorization (#62) and before the tool; denial fails the run, never fed
  back to the model.
- Whole-output checks may require buffering or disabling streaming. Output
  rejection cannot undo previously executed actions or released content.

See `docs/guides/authorization.md` and `docs/design/prompt-safeguarding.md`
for examples and application/library responsibility boundaries. APIs remain proposals.

## Safety and scope

### Read-first discovery direction

Discovery/registry and remote A2A invocation are core to the next multi-agent
release (M8+) after M7. Orchestrators are ordinary AgentFrame agents whose skills
call specialists over A2A, not a workflow/graph engine.
`AgentRegistry` is a small read-only abstract base with proposed async
`get(agent_id) -> AgentReference` and `list(query: AgentQuery) ->
list[AgentReference]`; pagination is unresolved. Environment/file adapters read
explicit sources. Implemented (#83): `AgentReference(name, card_url)` (the Agent
Card URL), `AgentQuery(names=...)`; `FileAgentRegistry("agents.yaml")` reads a
YAML `name`/`url` list on each call (1 MiB, `safe_load`, PyYAML core dep);
`EnvironmentAgentRegistry("AGENTFRAME_AGENTS")` reads comma-separated
`name=url` pairs. Config problems and unknown names raise `DefinitionError`.
`FoundryAgentRegistry` uses the chosen Foundry project agents API through
`azure-ai-projects` 2.x (2.10.0 observed), project endpoint
`https://{account}.services.ai.azure.com/api/projects/{project}`. Incoming A2A
on prompt agents is documented via `agent_card` plus protocol configuration;
target GA 1.0 explicitly. `FoundryAgentRegistry(project_endpoint=..., credential=...)`
returns A2A-enabled agents only. Listed-agent to card mapping/filter implementation,
authentication/RBAC and pagination remain open. Foundry User or higher enables
incoming A2A; endpoint callers need Foundry Agent Consumer or another
endpoint-access role. Verify registry-read permissions separately. Outbound
Foundry A2A connections are a possible additional reference source, not decided.
Model deployments are not agent endpoints; no AgentFrame-to-Foundry deployment
or tested registry integration is claimed.

Implemented (#84): `AgentDirectory(sources=[...], card_ttl_seconds=300,
allow_stale=False, allow_http_hosts=(), http_client=None)` owns SDK card
fetch/validation/cache/refresh via an internal fetcher (no public
`AgentCardResolver`/`CardCacheConfig`). Sources read on every `get`; cards
cached per card URL, refetched when stale; failed refresh raises unless
`allow_stale=True` (`DiscoveredAgent.stale`, warning). https only except
loopback/`allow_http_hosts`, no redirects, 10 s, 1 MiB, interface URL same
origin as the registry `card_url`; no caller credentials. Remote failures
raise `RemoteAgentError`; duplicate names across sources `DefinitionError`.
No construction I/O or background tasks; closes only its own client.
Compose `AgentDirectory(sources=[...])`, inject via `AgentApp(directory=...)`,
and validate configured `Remote[Literal["name"]]` bindings at startup.
`directory.find(skill="find_sources")` / `find(tags=["research"])` returns
matching native cards from bounded validated enrichment, not assumed reference fields.
Implemented (#85): `find(*, skill=None, tags=())`, at least one filter; skill
ID and all-of tags must match on the same card skill; every source listed,
cards resolved through the cache, at most 8 fetches at a time; any source/card
failure raises (honouring `allow_stale`); source order; no pagination (small
curated sources). Found cards are not callable; orchestrators call configured
remotes, dynamic invocation is deferred.
Inject directories as application dependencies;
caller-owned resources stay caller-owned unless explicitly transferred. No
registration writes, deploy/start/stop/delete, leases/federation, or A2A task
lifecycle in the discovery base.

Keep unspecified reference/YAML/query/result fields explicitly proposed. Do not query
skills/tags absent from references without an explicit metadata/enrichment
contract. Source failures are not empty results/deletion; refresh failures never
make stale current. Reconciled removal ends new eligibility, not active tasks,
and TTL is not instant revocation. Isolate caller/tenant/authenticated-card
variants; freshness is not health, trust or permission. Keep secrets out of
references/cards/source files; enforce application-selected outbound auth and
destination/redirect policy, bounded reads/deadlines/cancellation and safe
telemetry. Discovery does not delegate or auto-expose skills as tools. A separate
`RemoteAgentClient` may remain underlying/explicit client with construction open.
Author skills use `Remote[Literal["name"]]` (excluded from external schemas),
`call("skill", ..., output=list[Source], max_total_tokens=...)` for structured
A2A, or `ask(text)` for any compatible peer. Both wait within caller deadline;
remote input-required raises `ClarificationNeededError` with question, no
automatic back-and-forth. Implemented (#87): `class Remote[N: str]` read from the
`Literal` (alias `type Research = Remote[Literal["research-agent"]]`; string form
fails mypy), `Runtime(directory=)`/`AgentApp(directory=)` (app-owned; no
directory -> construction `DefinitionError`), `Runtime.validate_remotes` at
startup, bound per run inside its deadline. `ask` sends via the SDK
`JsonRpcTransport` with the directory client to `interface_url` only,
`return_immediately` then `GetTask` polling (0.25 s -> 1 s), per-request
`A2A-Version`/timeout headers; deadline or cancellation sends best-effort
`CancelTask`.
Implemented (#88): `call(skill, /, *, output=None, **arguments)` checks the
card declares the skill-call extension and skill (else `RemoteAgentError`, no
send, no `ask` fallback), sends the M2 wire format with per-request
`A2A-Extensions`, reuses `ask`'s wait/cancel, decodes text or `{"value": ...}`
and validates `output` (`OutputValidationError`, no remote data).
Implemented (#90): `ask`/`call` take `max_total_tokens=` (default: all the run
has left), held atomically in the run's `RunAccounting` before sending and sent
as `agentframe.max_total_tokens` only to peers listing it in `params.context`
(older receivers reject unknown keys). Receiver uses min(own, incoming). The
task's usage snapshot `total_tokens` replaces the hold, even if higher;
non-advertising peers hold nothing and make usage unknown (`None`). Over the
remaining budget -> `TokenLimitExceededError` before sending (default-ceiling
message says to pass `max_total_tokens` per call for fan-out).
Implemented (#92): `remotes={name: Service(credential=...)}` on
`Runtime`/`AgentApp` (same rule as `services=`); `OutboundCredential` protocol
`async headers()` + `satisfies(scheme)`, built-ins `APIKeyCredential(key,
header="X-API-Key")`, `EntraAppCredential(credential, scope=...)`. Headers per
request (send, polls, cancel) to `interface_url` only; no redirects. Startup:
card security must be met by the entry, else `DefinitionError`; open cards need
none. Implemented (#93): `OnBehalfOf(scope=...)` entries; credential on
inbound `EntraIDAuth(client_id=audience, client_credential=secret|cert bytes|
assertion func)`; delegated tokens (`scp`, not `idtyp=app`) kept privately as
`VerifiedCaller.user_assertion`; one azure `OnBehalfOfCredential` per run.
Non-Entra host/no credential -> startup `DefinitionError`; no user (app-only,
local run) -> `PermissionDeniedError`; failed exchange -> `AuthenticationError`;
never a `Service` fallback. Opt-in `entra` OBO test uses two registrations.
`ClarificationNeededError(question, task_id, context_id)` fails a hosted
orchestrator task. See `docs/design/discovery.md` and `docs/api/discovery.md`.

Plan cross-hop verified identity/delegation without authority amplification,
permissions, deadlines, remaining token allowance and trace/correlation;
remote services authenticate and enforce their own limits. Per-remote
`on_behalf_of` defaults for Entra delegated callers, using standard target-scoped
OBO exchange; explicit `service` uses agent identity. API-key/anonymous ingress
supports service only. Never forward incoming tokens or fall back on exchange
failure; validate target user/actor permissions without authority amplification.
Send remaining deadline (receiver min with own timeout); per-call token cap
defaults to caller remaining, receiver can only lower, actual peer usage charged
once. Other-peer usage is unknown, never zero. TaskGroup fan-out shares atomic
reservations. Default max 5 remote hops, excess fails. These controls use the
same A2A extension; detailed config/wire/usage trust remains open, not base
protocol fields or hard billing guarantees.
No orchestration helpers in the first multi-agent release: plain await,
asyncio.TaskGroup and directory.find. Later optional `Remote[ResearchAgent]`
typed proxy always uses structured selection, never prompt routing. Prove two or more
independently deployed AgentFrame agents, an orchestrator discovering/calling a
specialist with propagated context, and non-AgentFrame A2A client/server interop.
Development support starts with running N agents locally, then container
packaging; Foundry/Kubernetes templates are later. M2 `agentframe run module:create_app`
serves the app's own configured `AgentApp` (`--dev module:Class` is localhost-only
anonymous; Uvicorn is the `server` extra). Implemented (#95): `agentframe dev`
reads agentframe.yaml (`agents: {name: {app: module:create_app, port}}`,
`project.py`), starts each as `python -m agentframe run` on 127.0.0.1 in file
order after the previous `/ready`, writes `.agentframe/agents.yaml` (name/url
only, self-gitignored), generates one random key per agent per session (an
agent calls others with its own key, verified as its name), passes keys only
via child env (`AGENTFRAME_DEV`, `AGENTFRAME_PUBLIC_URL`,
`AGENTFRAME_DEV_REGISTRY`, `AGENTFRAME_DEV_API_KEYS`,
`AGENTFRAME_DEV_REMOTE_KEYS`), never disk/logs; optional
`AGENTFRAME_DEV_CLIENT_KEY` is caller `developer`. Factories call
`dev_settings()` (refuses without `AGENTFRAME_DEV=1`; directory has its own
http client; reads `AGENTFRAME_ALLOW_HTTP_HOSTS`). Output prefixed
`[agent-name]`, agents log INFO; failures stop all and exit 1; Ctrl+C sends
one SIGTERM (children in own session), kills after 35 s; a client key with
`,`/`=` is refused. Implemented (#96): `agentframe compose [--config]
[--force]` (`compose.py`) writes development `compose.yaml` (one service per
agent, `agentframe run APP --host 0.0.0.0 --port P`, `127.0.0.1:P:P`, `/ready`
healthcheck, depends on earlier agents healthy; env = dev contract with
`AGENTFRAME_PUBLIC_URL=http://name:P`, `AGENTFRAME_AGENTS` (read by
`dev_settings()` as an `EnvironmentAgentRegistry`), `AGENTFRAME_DEV=1`,
`AGENTFRAME_ALLOW_HTTP_HOSTS`=service names; keys only
`${NAME_API_KEY:?...}`/`${AGENTFRAME_DEV_CLIENT_KEY:?...}` from user `.env`)
and, only if missing, a `Dockerfile` (digest-pinned `python:3.13-slim`,
`dist/*.whl`, `USER 10001:10001`); `--force` overwrites only `compose.yaml`.
Golden files in `tests/unit/golden/`; opt-in `docker` test
(`AGENTFRAME_TEST_DOCKER=1`, skipped without a running Docker).
No hosting platform is implied. See `docs/guides/development.md`.

### Initial release boundaries

The first usable release supports text generation, typed structured outputs,
and bounded execution of explicitly registered tools with generation
continuations. Return complete validated results by default; streaming is later.
Normalized usage layouts remain open, not normalization ownership, generation
syntax or approved limits/errors; provider schema subsets are documented per
adapter (M4). Do not
reintroduce wholesale deferral of tools/structured outputs.

Applications own human approval workflows; no built-in approval pause/resume
in first release. Required action/authorization checks remain enforced;
protocol task support is not workflow persistence or human resumption.
DI, `RunResult`, host defaults, retries and tool flags are user-approved above.
Factory/disposal interfaces, usage markers, A2A typed-part
mappings, detailed byte/error mappings and dedupe atomicity remain unresolved.
Errors derive from `AgentFrameError`: `DefinitionError`, `InputValidationError`,
`OutputValidationError`, `AuthenticationError`, `PermissionDeniedError`,
`PolicyUnavailableError`, `ModelError` (`UnsupportedCapabilityError`,
`ModelRefusalError`, `IncompleteOutputError`, `ProviderError`),
`LimitExceededError` (`TokenLimitExceededError`, `ModelCallLimitExceededError`,
`TimeoutLimitExceededError`, `ToolCallLimitExceededError`) and AgentFrame
`CancelledError` distinct from `asyncio.CancelledError`: local runs never
convert `asyncio.CancelledError` (it propagates); AgentFrame's only reports a
cancelled hosted task, later a cancelled remote call (M8+) (M7 #79). Every error carries
invocation ID and safe message; constructor/startup-ID/transport details open.
A2A failures use safe failed/rejected states as appropriate; no raw provider
exception, stack trace or secret goes to callers. Log diagnostics with redaction:
failed runs log ID, class and safe message; skill/tool/router tracebacks stay in
operator logs with the ID, so authors keep secrets out of exception messages.
`tests/integration/test_hardening.py` is the M7 end-to-end acceptance suite;
`tests/integration/test_multi_agent_release.py` is M8+'s, running
`examples/multi_agent` as separate `agentframe run` processes.
Network host examples must include clearly labeled application-supplied
development authentication/authorization placeholders where appropriate;
loopback/omission is not access policy or a production-safe default.
M8+ adds `RemoteAgentError` (#84; failures caused by another agent, optional
`task_id`/`task_state`) and `ClarificationNeededError` under
`AgentFrameError`; local client
input-required question is not a failed remote task or durable resumption.

Use explicit model selection, finite execution limits, validated inputs/outputs,
isolated run state, cancellation propagation, redacted operational logs, and
explicit failures. No implicit downloads, cloud fallback, tool grants, server
startup, or inference at import time. Runtime overrides must not weaken host
security or exceed its resource ceilings.

Follow `docs/roadmap.md` milestones 0-7, each independently releasable
and testable: M0 development setup; M1 model-free authoring/`Runtime.invoke`;
M2 native A2A structured extension requests plus custom free-text routing only;
M3 interchangeable Foundry/Ollama text
generation; M4 typed structured outputs; M5 bounded tool calling; M6 built-in
opt-in model routing after M3/M4; M7 hardening to the first usable release.
M1 is the first proof; M3 meets the README success criterion. The authoring goal
is approximately 25 lines for a text agent, excluding host and credential setup.
The next core release (M8+) adds multi-agent discovery/remote A2A composition.
MCP, optional telemetry integration and streaming/AG-UI remain separate
follow-ups after M7; cross-agent trace/correlation propagation is M8+ core scope.
Do not move multi-agent composition into the M7 single-agent release.
The Python 3.13+/test-through-3.15, A2A 1.0/official SDK and Foundry API choices
are resolved. Ruff/mypy/pytest are selected above; exact compatible pins,
final tool configuration, A2A native type mappings and provider mappings
still need implementation proof. Do not silently change approved targets or
choose remaining interfaces unilaterally.

No workflow/graph engine, deployment service, federation or built-in approval
workflow is in the current core. Durable checkpointed flows, pause/resume and
crash recovery are revisited after multi-agent real usage, not designed now.
If added, use an optional separate package (candidate `agentframe-workflows`).
Long-running work uses A2A tasks with an in-memory default and pluggable
persistent store; persisted tasks are not Python workflow crash recovery.
Do not expand scope into UI, RAG infrastructure, evaluation platforms or hosted
cloud-agent deployment without an explicit request; local/container packaging
is part of the multi-agent direction, not a hosting service.

### Future streaming and AG-UI

Future streaming should support a scoped optional AG-UI agent-to-frontend
adapter, distinct from A2A agent-to-agent, MCP tools and OTel tracing. A2A/card-first
remains; no UI framework, implicit frontend tool execution, shared-state
persistence, human approval workflow, resumability or blanket feature/conformance
claim. Decided later configuration is `AgentApp(ag_ui=AGUIConfig(enabled=True))`
at `/ag-ui` on the same server/auth/permissions/limits. Stream run start/finish,
text deltas, tool names/status (not raw arguments by default) and safe errors.
Whole-answer checks release complete text at end; disconnect cancels run,
first version has no reconnect/resume. Frontend tools/shared state later opt-in.
No runtime stream method or event schema is finalized. Use official
primary sources and native protocol types; select/test compatible SDK/schema/
transport versions later. Do not advertise A2A streaming before implementation.
Keep native thread/run/message/tool IDs separate from server invocation identity,
isolate caller/tenant/history/events, treat frontend messages/state/tools as
untrusted, gate output/events before release and expose no secrets/raw private
reasoning. Detailed cancellation translation/buffering/backpressure/shutdown
remain open; tracing/accounting/enforcement work independently. See
`docs/design/streaming.md` and `docs/api/streaming.md`.

## Documentation maintenance

Read `README.md` and `docs/README.md` first. Keep `docs/roadmap.md`, the
guides (`docs/guides/annotations.md`, `agent-examples.md`, `limits.md`,
`authorization.md`, `development.md`) and the design docs
(`docs/design/base-classes.md`, `tracing.md`, `mcp-skills.md`,
`prompt-safeguarding.md`, `discovery.md`, `streaming.md`, `evaluation.md`)
consistent, including discovery/streaming API pages and navigation. Preserve the distinction between
confirmed product direction and illustrative API details.

Maintain `docs/api/README.md` and its linked surface reference pages alongside
those design documents. Give every proposed decorator, class, and known public
method its own heading with purpose, signature status, parameter/field table
(type, required/default status, meaning, constraints/precedence), small example
with imports, behavior/failures, and related rationale links. Document constructor
parameters under the class; do not invent methods for interfaces still open.
Clearly distinguish decisions, illustrative shapes, and unresolved fields/types/
defaults. For an unspecified interface, use typed dependency examples rather
than fabricate constructors, callbacks, exception names, or protocol fields.
Keep ordinary Python/Pydantic contracts separate from AgentFrame decorators.
Explain application-supplied placeholders; no example claims an installed SDK.
Keep guides and rationale in place and preserve links; update root/docs navigation
when public reference pages change. Check local links/anchors, Python example
syntax where applicable, and diff whitespace without importing an absent SDK.

When implementation starts, select compatible Python/dependency versions and
add focused validation. Until then, do not invent build/test instructions or
claim that documentation snippets run against an existing AgentFrame package.
