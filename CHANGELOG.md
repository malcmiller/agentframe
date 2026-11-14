# Changelog

All notable changes to AgentFrame are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Pre-1.0: public APIs may change in minor releases; changes are listed in the
changelog.

## [Unreleased]

### Added

- **Evaluation (M9):** `agentframe eval module:function [--json PATH]
  [--min-pass-rate RATE] [--max-errors N] [--max-total-tokens N]
  [--allow-not-run] [--allow-over-budget] [--capture-content]` runs the
  `Evaluation` a no-argument function returns, prints the content-free summary,
  writes the JSON report before checking thresholds and passes the threshold
  flags straight to `EvalReport.require`. Exits 0 when they are met, 1 when one
  is missed and 2 on a configuration error (one `agentframe: error:` line). An
  evaluation with `capture_content=True` is refused unless `--capture-content`
  is passed; the command never turns capture on. New read-only
  `Evaluation.capture_content`. No new dependency (#162).
- **Evaluation (M9):** `Judge(..., votes=1)` takes the majority of up to
  `votes` verdicts (an odd number from 1 to 15, else `DefinitionError`), run
  one at a time and stopping once the majority is certain. Each vote is a
  separate judge run with its own budget hold and usage; a vote that errors or
  that the budget cannot fit errors the case rather than scoring it on fewer
  votes. With `votes` above 1 the score's `value` is the fraction of votes
  that passed, so a split vote is visible in reports (#161).
- **Orchestration (M10):** `await ctx.run_plan(plan, skills, max_parallel=4,
  max_tokens_per_step=None, max_steps=8)` checks a `Plan` again against the
  catalog entries the caller may use, as `ctx.plan` does, then runs its steps as
  remote calls under one `asyncio.TaskGroup`: a step starts when its
  dependencies have completed, with at most `max_parallel` running. Before each
  step is sent it counts as one tool call, its `{"$ref": ...}` and
  `{step.field}` references are replaced by copies of earlier values (other than
  strings, inserted into `ask` text as compact JSON with sorted keys), the
  caller's permissions for the entry are re-checked and
  `ExecutionPolicy.tool_checks` see the entry name and the substituted
  arguments. Each step holds `max_tokens_per_step` (default an equal share of
  the tokens left) within the run's deadline, depth and token budget. The first
  failure cancels running steps (best-effort `CancelTask`) and is raised as
  itself; nothing is retried. Returns `PlanResult` with the `result` step's
  value and a `StepResult` per step (`status`, `value`, `usage`,
  `remote_task_id`) (#171).
- **Evaluation (M9):** `Judge(model, rubric=..., options=None)`, a scorer
  (named `judge` in reports) that asks any AgentFrame `Model` with structured
  output for a binary `Verdict(passed, reason)` through an ordinary `Runtime`
  run. The rubric is a trusted instruction; the case input, output and
  `expected` reference (when set) are one JSON user message. Each judge run
  holds its own `token_ceiling` against the evaluation's `max_total_tokens`
  just before it starts, after the case's hold has ended, and errors the case
  with `TokenLimitExceededError` when it cannot fit. Judge usage is reported
  apart from the target's in `CaseResult.judge_usage` and
  `EvalReport.total_judge_usage` (both in `write_json`, unknown as `null`),
  and counts toward `over_budget`, `budget_fully_enforced` and
  `require(max_total_tokens=...)`; the verdict's reason is kept only with
  `capture_content=True` (#160).
- **Evaluation (M9):** `A2ATarget` is proven against agents not built with
  AgentFrame: CI evaluates a plain official-SDK A2A agent with free-text cases
  (usage reported as unknown, never 0; a `skill=` case refused before
  sending; a question back errors the case with `ClarificationNeededError`),
  and an opt-in `foundry` test evaluates a real Foundry A2A agent through
  `FoundryAgentRegistry` and `EntraAppCredential`. The evaluation guide has a
  new "Agents not built with AgentFrame" section (#159).
- **Evaluation (M9):** `EvalReport.write_json(path)` writes a versioned JSON
  document (`"schema": "agentframe.evaluation.report/1"`) with the AgentFrame
  version, target, scorer names, every aggregate (including `not_run_count`,
  `flaky_cases`, `over_budget` and `budget_fully_enforced`) and one entry per
  attempt; unknown token counts are `null`, never 0. Case content, score
  reasons and error messages are left out unless `capture_content=True`
  (a `not_run` attempt's fixed reason is kept). `EvalReport.require(
  min_pass_rate=None, max_errors=0, max_total_tokens=None,
  allow_not_run=False, allow_over_budget=False)` raises `AssertionError`
  listing each missed threshold above the summary; unknown usage never passes a
  token threshold and a pass-rate threshold fails when nothing ran. New
  `EvalReport.target`, `scorers` and `capture_content` and
  `Target.description` (`LocalSkill` and `A2ATarget`, names only, never URLs
  or credentials). No pytest plugin; see the new
  [evaluation guide](docs/guides/evaluation.md) (#156).
- **Orchestration (M10):** typed execution plans. `Plan(steps, result)` and
  `PlanStep(id, use, arguments, text, after)` are frozen Pydantic values: a
  flat directed acyclic graph whose dependencies come from references
  (`{"$ref": "step"}` / `{"$ref": "step.field"}` in arguments, `{step}` /
  `{step.field}` in `ask` text) plus `after`. `SkillCatalog.check(plan,
  max_steps=8)` validates a plan without any network or model call (unique,
  well-formed step IDs; catalog entries; arguments versus text by entry kind;
  top-level required and unknown arguments; references; no cycles; at most
  `max_steps`, never above the run's remaining `max_tool_calls`, and 64 KiB)
  and returns the steps in parallel groups in a deterministic topological
  order. Failures raise `InputValidationError` naming the step and field,
  never values. New `SkillCatalog.get(name)` looks an entry up by name. No
  plan runs yet (#169).
- **Orchestration (M10):** model-made plans. `await ctx.plan(goal, skills,
  max_steps=8, instructions=None)` makes one structured-output call with the
  agent's model (counted toward the run's model calls and tokens, seen by
  `model_request_checks`) and returns a `Plan` checked with
  `SkillCatalog.check`. The goal and the catalog are JSON data in the user
  message, never in system instructions; only entries the caller may use are
  shown or accepted, and a hidden one fails like an unknown name. The model
  sees entry names as an enum and each step's arguments as a JSON string
  (`{"text": ...}` for `ask` entries), which converts for Foundry strict mode
  and Ollama `format`. A declined goal raises `InputValidationError("No plan
  for this goal with the available skills")` without the model's reason; an
  invalid plan raises `OutputValidationError` and is never repaired or
  re-requested; an empty catalog raises `InputValidationError` before any
  model call. `SkillCatalog.check` now raises `InputValidationError` instead
  of `RecursionError` for a too-deeply nested plan built with
  `model_construct`. Nothing runs yet (#170).
- **Evaluation (M9):** `Evaluation(concurrency=4, repeats=1,
  max_total_tokens=None, timeout_seconds=None)`. Up to `concurrency` attempts
  run at once, started and reported in case order. `repeats` runs each case
  several times: every `CaseResult` has an `attempt`, rates count attempts and
  `EvalReport.flaky_cases` lists cases whose attempts ended differently. With
  a token budget, each attempt holds its full per-run ceiling
  (`LocalSkill.token_ceiling`) before starting, so holds never exceed the
  budget; reported usage then replaces the hold (unknown usage keeps it), and
  attempts that cannot fit are `not_run`. `EvalReport.over_budget` flags
  reported overruns. When the timeout expires, running attempts are cancelled
  and `errored` with `TimeoutLimitExceededError` and the rest are `not_run`.
  New `CaseStatus.NOT_RUN` and `EvalReport.not_run_count`; `not_run` attempts
  are left out of every other aggregate, and the summary shows them, the
  budget and flaky cases (#157).
- **Evaluation (M9):** `agentframe.evaluation` with `Case`, one fixed input
  (exactly one of skill `arguments` or free `text`, optional `expected` and
  `tags`), and `load_cases`, a strict JSONL loader (UTF-8, at most 10 MiB,
  `json.loads` only) whose `DefinitionError`s name the file and line but never
  the case content. `import agentframe` does not load the package, and it adds
  no dependency.
- **Evaluation (M9):** `EvalReport` aggregates: `passed_count`,
  `failed_count` and `errored_count`, `pass_rate`, per-scorer and per-tag pass
  rates, `total_usage` (`UsageTotals`: sums only when every case reported
  usage, otherwise `None` plus the reported lower bound and the number of
  unknown cases), `latency` (`Latency`: p50, p95 and max) and
  `errors_by_class`. `str(report)` is a plain-text summary table, failing
  cases first, with no case content unless `capture_content=True` (#155).
- **Evaluation (M9):** `A2ATarget(directory, name, skill=None, output=None,
  identity=None, options=None)` evaluates a deployed A2A agent: `call` with
  each case's `arguments` when `skill` is set, otherwise `ask` with its
  `text`, through the same binding a skill's `Remote` uses. Each case is its
  own root run with limits from `options.limits`, then the defaults (120 s
  deadline, 100 000-token ceiling, 5 hops); usage comes from the peer's task,
  unknown when it does not advertise it; a slow peer is cut off and cancelled
  best effort. Service identity only: `OnBehalfOf` raises `DefinitionError`.
  `Evaluation(target=...)` now takes any `Target` (`token_ceiling`,
  `check_case`, `run`), and
  targets return a `TargetResult`, whose call counts and finish reason are
  `None` for remote agents (#158).
- **Multi-agent:** `AgentDirectory(max_reply_bytes=8_388_608)` bounds the
  A2A replies `Remote.ask` and `Remote.call` read (`SendMessage`, `GetTask`,
  `CancelTask`). Bodies are streamed and cut off at the limit, with or without
  a `Content-Length`; a larger reply fails the call with a safe
  `RemoteAgentError` that holds none of the reply (#150).
- **Orchestration (M10):** `ctx.remote_skills(...)` returns a `SkillCatalog` of
  `RemoteSkill` entries built from Agent Cards: one entry per skill (with its
  argument schema) for agents with AgentFrame's skill-call extension, one
  free-text `ask` entry for other A2A agents. With no arguments every agent the
  directory's sources list is considered (the registry is the allowlist);
  agents whose card is unreachable or invalid, whose security cannot be met or
  whose schema is over 8 KiB, and sources that fail to list, are skipped with
  one warning each. Declared `Remote` parameters restrict the catalog and
  raise on failure. `agents=`, `skill=` and `tags=` narrow it; more than 20
  entries raise `DefinitionError`, never truncated. Card text is bounded (500
  characters, 10 tags, no control characters or `examples`), skill IDs must be
  safe and unique, entry names must be unique, and a hosted
  orchestrator leaves itself out. No model is called (#166).
- **Orchestration (M10):** `ctx.generate(goal, tools=[*skills])` offers
  `RemoteSkill` catalog entries to the model as tools, next to `@tool`
  functions. Each becomes a tool with a provider-safe name (`.` and other
  characters become `_`), its bounded card description and a copy of its
  argument schema (`{"text": string}` for `ask`). A call runs `Remote.call`
  or `Remote.ask` on the catalog's per-run binding, so deadlines, hop depth,
  identity, correlation and token reconciliation apply; it counts toward
  `max_tool_calls`, is never retried and holds `remote_skills(...,
  max_tokens_per_call=)` tokens, or everything the run has left. Results go
  back as untrusted tool output; `RemoteAgentError` and
  `ClarificationNeededError` become a tool error naming only the class, while
  limits, authentication and authorization fail the run. Tool-name
  collisions, repeated entries and entries from another run raise
  `DefinitionError` before any model call (#167).
- **Orchestration (M10):** permissions and tool checks for model-chosen
  remote calls. `remote_skills(..., permissions={"billing-agent.refund":
  {"refunds:write"}})` sets an entry's `requires_permissions`: entries the
  caller lacks are never offered to the model, and a call naming one fails the
  run with `PermissionDeniedError` before sending. Every remote call then
  passes the existing `ExecutionPolicy.tool_checks` with `tool_name` set to the
  entry name (`"agent.skill"` or `"agent"`), the arguments about to be sent and
  the same skill object as `@tool` calls; denial raises `PermissionDeniedError`,
  a raising or late check `PolicyUnavailableError`, nothing is sent and the
  model is never told. An agent's `ask` entry also requires every permission
  given for its skills. A tool name as a key, or a declared remote's key that
  names none of its card's entries, raises `DefinitionError`; other keys
  matching no entry log one WARNING. The 20-entry limit counts entries
  before hiding. Docs
  warn that `Service` identity makes the orchestrator a confused deputy
  without permissions (#168).
- **Orchestration (M10):** `default_remote=Service(...) | OnBehalfOf(...)` on
  `Runtime` and `AgentApp` authenticates skill-catalog agents that have no
  `remotes=` entry. A per-name entry always wins; the default is sent only to
  agents whose card requires credentials it meets (never to open agents), and
  any other secured agent is skipped with a warning. Declared `Remote`
  parameters never use it. An `OnBehalfOf` default needs `EntraIDAuth` with a
  client credential (else `DefinitionError` at construction) and a signed-in
  user (else `PermissionDeniedError`, no fallback). Docs warn that the
  default's token audience or shared API key reaches every registered agent
  that asks for it (#175).
- **Evaluation (M9):** `Evaluation(cases=..., target=LocalSkill(agent,
  skill=...), scorers=[...])` runs each case's arguments through
  `Runtime.run` (validation, permissions, policies, limits; a protected skill
  needs an explicit `principal=`) and scores the output with plain sync or
  async functions returning `bool` or `Score`. Each `CaseResult` is `passed`,
  `failed` or `errored` (target or scorer raised, or a scorer returned a bare
  number) with the error class and safe message and the run's metadata; errors
  never stop the run. Duplicate case IDs, `text` cases and `expected` values
  that are not valid skill results fail before any case runs. Results hold no
  inputs, outputs or reasons unless `capture_content=True`; the harness logs
  only case IDs, statuses and invocation IDs, plus operator-log tracebacks
  (with messages) of non-AgentFrame exceptions.
- **Evaluation (M9):** built-in scorers `equals` (output equals the case's
  `expected`, Pydantic models field by field), `contains(text=None,
  case_sensitive=False)` (text output contains `text` or the case's
  `expected`, ignoring case by default) and `valid(T)` (output validates as
  `T` with Pydantic, string output also tried as JSON; the failing reason
  names problem locations only). A case without the `expected` value a scorer
  needs, or with non-text output or `expected` for `contains`, is `errored`
  with a message naming the case ID and type, never the content (#154).

### Fixed

- `load_cases` accepts a JSONL file that starts with a UTF-8 byte order mark.

### Fixed

- **Hosting:** a run that finishes while `AgentApp` is shutting down now always
  has its final task state stored before the app closes its task queues. It
  could stay `WORKING` before, because a run frees its slot just before the
  SDK saves that state, which matters for a persistent task store. Shutdown is
  still bounded by `shutdown_grace_seconds` plus the cancel wait (#128).

## [0.2.0] - 2026-11-14

The multi-agent release (M8+): independently deployed AgentFrame agents find
each other through registries and an `AgentDirectory`, and orchestrator skills
call them over A2A with `Remote.call` and `Remote.ask`, carrying deadlines,
token budgets, hop limits, correlation and caller identity across hops.
`agentframe dev` and `agentframe compose` run several agents locally or in
containers. Calls interoperate with non-AgentFrame A2A agents, including
Microsoft Foundry agents.

### Added

- **Multi-agent (M8+):** `examples/multi_agent`, a planner orchestrator and a
  research agent with an `agentframe.yaml`. The planner finds the research
  agent with `directory.find`, then fans out a structured `call` and a
  free-text `ask` per topic in one `asyncio.TaskGroup`, each with its own
  `max_total_tokens`. The release acceptance test runs both as separate
  `agentframe run` processes; the examples smoke runner checks the example.
- **Multi-agent (M8+):** `agentframe compose` writes a development
  `compose.yaml`, one container per agent of `agentframe.yaml`, and, if
  missing, a `Dockerfile` template (`python:3.13-slim` pinned by digest,
  non-root, installs the project's wheels from `dist/`). Services bind
  `0.0.0.0` inside their container, publish on `127.0.0.1` only, check
  `/ready` and get the `agentframe dev` environment with service-name URLs
  and the explicit `AGENTFRAME_DEV=1` / `AGENTFRAME_ALLOW_HTTP_HOSTS` opt-in.
  Keys appear only as `${VAR}` references to a `.env` file the user creates.
  Existing files are never overwritten without `--force`, and a `Dockerfile`
  never. `dev_settings()` now also reads an `AGENTFRAME_AGENTS` registry, and
  `agentframe dev` refuses an `AGENTFRAME_DEV_CLIENT_KEY` containing `,` or
  `=` without showing it.
- **Multi-agent (M8+):** `agentframe dev` runs every agent listed in
  `agentframe.yaml` (`agents: {name: {app: module:create_app, port: int}}`)
  as its own `agentframe run` process on `127.0.0.1`, wired together. It
  checks ports first, starts agents in file order once each is ready, writes
  `.agentframe/agents.yaml` with local card URLs, and generates one random
  API key per agent per session, passed only through the agents'
  environment, never written or logged. Every agent also accepts an optional
  `AGENTFRAME_DEV_CLIENT_KEY` as caller `developer`. Output is combined with
  an `[agent-name]` prefix, with each run's invocation IDs. Failures stop
  every agent and exit 1; Ctrl+C stops them gracefully. App factories read the
  wiring with the new `dev_settings()`, which refuses to run without
  `AGENTFRAME_DEV=1`. `python -m agentframe` now runs the command line.
- **Multi-agent (M8+):** cross-agent token budgets. `Remote.ask` and
  `Remote.call` take `max_total_tokens=` (default: everything the run has
  left), held atomically against the run's `RunLimits.max_total_tokens`
  before sending and sent as `agentframe.max_total_tokens` to peers whose
  card lists it in `params.context` (now `deadline_ms`, `hops`,
  `max_total_tokens`). Receivers use the smaller of it and their own limit.
  The usage the peer's task reports is charged to the run once, so
  `RunResult.usage` includes every hop; peers that do not list the field hold
  nothing and leave usage unknown (`None`), never zero. A ceiling over what is
  left raises `TokenLimitExceededError` before sending.
- **Multi-agent (M8+):** `OnBehalfOf(scope=...)` in `remotes=` calls a remote
  agent on behalf of the signed-in user through the Microsoft Entra
  on-behalf-of flow; the incoming token is never forwarded. `EntraIDAuth`
  gains `client_id=`, `client_credential=` and `token_transport=`, and keeps a
  delegated user token privately as `VerifiedCaller.user_assertion`. Startup
  refuses `OnBehalfOf` behind non-Entra hosts or without a client credential;
  app-only callers and local runs fail with `PermissionDeniedError`, failed
  exchanges with `AuthenticationError`, never falling back to `Service`. The
  `entra` extra now pins `azure-identity==1.27.0` and `aiohttp==3.14.5`.
- **Multi-agent (M8+):** cross-agent correlation. Hosted runs log `Run
  started` / `Run completed` at `INFO`, and their log records carry `agent`,
  `invocation_id`, `root_invocation_id` and `parent_invocation_id` in
  `logging` `extra` (AgentFrame sets no format). `Remote.ask` and `call` send
  `agentframe.root_invocation_id` and `agentframe.parent_invocation_id` to
  peers whose card lists them in `params.context`, so every agent of one
  request logs the same root. Values that are not lowercase UUID strings are
  ignored (the run becomes a new root), never echoed or trusted. A valid W3C
  `traceparent` (and valid `tracestate`) is forwarded unchanged to every peer;
  invalid ones are dropped. None of it authenticates or grants access, and
  task metadata is unchanged.
- **Multi-agent (M8+):** `Remote.ask` sends the run's remaining time
  (`agentframe.deadline_ms`) and depth (`agentframe.hops`) to peers whose card
  lists them in the skill-call extension's new `params.context`; others get
  plain A2A. Receivers cap their timeout by the caller's deadline and reject
  requests deeper than the new `RunLimits.max_remote_hops` (default 5); a run
  at that depth fails before sending. New `RunContext.hops`. With the
  extension activated, a message without `agentframe.skill` is now free text
  instead of an `InvalidParams` error.
- **Discovery (M8+):** read-only `AgentRegistry` base with `AgentReference`
  (agent name and Agent Card URL) and `AgentQuery` (name filter), plus
  `FileAgentRegistry` (YAML `name`/`url` list) and `EnvironmentAgentRegistry`
  (comma-separated `name=url` pairs). PyYAML is a new core dependency.
- **Discovery (M8+):** `AgentDirectory(sources=[...])` returns
  `DiscoveredAgent` results with validated official SDK Agent Cards, cached
  for 300 seconds, with `allow_stale`, `refresh`, `allow_http_hosts` and an
  optional caller-owned `http_client`. New `RemoteAgentError` for failures
  caused by another agent. `httpx` is now an exactly pinned core dependency.
- **Discovery (M8+):** `AgentDirectory.find(skill=..., tags=...)` returns the
  Agent Cards of listed agents with a skill matching the ID and every tag,
  in source order. It fetches at most eight cards at a time through the cache
  and raises on any source or card failure instead of returning fewer cards.
- **Discovery (M8+):** `FoundryAgentRegistry(project_endpoint=...,
  credential=...)` lists a Foundry project's A2A-enabled agents, mapped to
  their A2A 1.0 Agent Card URLs. New optional `AgentRegistry.card_headers`
  lets a source send its own credentials with its card requests; Foundry
  sends a bearer token from its credential. The `foundry` extra now pins
  `azure-ai-projects==2.11.0`.
- **Remote agents (M8+):** skills declare another A2A agent as a
  `Remote[Literal["name"]]` parameter (excluded from arguments, the Agent Card
  and schemas), found through `Runtime(directory=...)` or
  `AgentApp(directory=...)`; server startup checks every name with
  `Runtime.validate_remotes`. `await remote.ask(text)` sends plain A2A text and
  returns the text answer within the run's deadline, cancelling the remote
  task on deadline or cancellation. New `ClarificationNeededError` when the
  remote agent asks a question.
- **Remote agents (M8+):** `await remote.call("skill", output=Type, **arguments)`
  runs a remote AgentFrame agent's skill by ID through the skill-call
  extension and validates its result into `output` (`OutputValidationError`
  on mismatch). A peer whose card lacks the extension or the skill raises
  `RemoteAgentError` before anything is sent, with no free-text fallback.
  `output` and `max_total_tokens` cannot be remote argument names.
- **Outbound identity (M8+):** `remotes={name: Service(credential=...)}` on
  `Runtime` and `AgentApp` authenticates remote calls with the calling
  agent's own identity: `APIKeyCredential(key, header="X-API-Key")` or
  `EntraAppCredential(credential, scope=...)` (any `azure.core` async token
  credential, sent as a bearer token), or any `OutboundCredential`.
  Credentials go per request only to the card's validated interface origin,
  never with redirects, and incoming caller credentials are never forwarded.
  Agents whose card declares security, including Foundry A2A agents, can now
  be called; startup fails with `DefinitionError` when the configured
  credential cannot satisfy the card's security.

### Fixed

- `agentframe dev` no longer passes an exported `AGENTFRAME_AGENTS` to its
  agents, which made `dev_settings()` list every agent twice and fail startup
  with a duplicate-name `DefinitionError`.
- URLs with a non-numeric or out-of-range port are refused as
  `HostConfig.public_url` and registry card URLs.
- `FileAgentRegistry` strips whitespace around names and URLs, like
  `EnvironmentAgentRegistry`.
- Credential headers (`Authorization`, `Proxy-Authorization`, `Cookie`,
  `X-API-Key`) are dropped from the A2A SDK's call context after
  authentication, so the SDK's debug logs no longer contain bearer tokens or
  API keys.
- `FoundryAgentRegistry` finds A2A-enabled agents again: the live Foundry API
  lists them as `agent_endpoint.protocols: ["a2a", ...]`, which
  `azure-ai-projects` 2.11.0 does not model, and its list response omits
  `agent_endpoint` entirely, so every agent was skipped and lookups raised.
  `list` now reads each queried agent's details (one call per agent), and the
  `protocol_configuration.a2a` shape is still accepted.

## [0.1.0] - 2026-11-13

The first usable single-agent release, distributed on PyPI as `agentframe-py`
and imported as `agentframe`.

### Added

- **Authoring:** `Agent` subclasses with `@agent` identity and one async
  `@skill` method per advertised skill; Python types and Pydantic models as
  validated input and output contracts; `Inject[T]` services registered with
  `Services.add` / `add_scoped`; per-invocation `RunContext`.
- **Local runtime:** `Runtime.invoke` returns a validated value and
  `Runtime.run` a `RunResult` with invocation ID, usage, model and tool calls,
  duration and finish reason. `RunLimits`, `ModelSettings` and `RunOptions`
  resolve field by field from library to agent, skill and run.
- **A2A 1.0 hosting:** `AgentApp` serves a generated native Agent Card and a
  JSON-RPC endpoint through the official `a2a-sdk`. Structured requests name a
  skill through the Agent Card's skill-call extension; free text uses a custom
  `SkillRouter` or the opt-in `ModelRouter`, which asks back with
  `TASK_STATE_INPUT_REQUIRED` when a request is ambiguous.
  `agentframe run module:create_app` serves an application (`server` extra).
- **Models:** `OllamaModel` (local Llama, `ollama` extra) and `FoundryModel`
  (Microsoft Foundry GPT deployments, `foundry` extra), switched by
  configuration only. One `ctx.generate` returns text, a validated Pydantic
  instance with `output=`, or runs registered `@tool` functions with
  `tools=[...]` within `max_tool_calls`. Refusal, truncation and unsupported
  capabilities raise typed errors. `ScriptedModel` in `agentframe.testing`
  scripts model replies for tests.
- **Security:** `HostConfig(auth=...)` with `APIKeyAuth`, `EntraIDAuth`
  (`entra` extra), explicit `AllowAnonymous` or a custom `Authenticator`;
  skill `requires_permissions`, role mapping and an injected
  `AuthorizationPolicy`, failing closed; optional `ExecutionPolicy` input,
  model-request, tool and output checks.
- **Hardening:** host ceilings for concurrent runs, request and response size,
  and graceful shutdown; `/health` and `/ready`; A2A message deduplication by
  caller and message ID; safe, correlated errors with redacted operator logs.
- **Examples:** runnable examples in `examples/`, each checked against the
  installed wheel by `python -m examples.smoke`.

[0.2.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.2.0
[0.1.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.1.0
