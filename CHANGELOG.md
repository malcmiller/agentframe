# Changelog

All notable changes to AgentFrame are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Pre-1.0: public APIs may change in minor releases; changes are listed in the
changelog. From the release after 0.3.0, a release that changes public API or
behavior starts with an **Upgrading** section; see the
[stability, deprecation and upgrade policy](docs/guides/stability.md).

## [Unreleased]

### Upgrading

- The default in-memory A2A task store now deletes finished tasks after
  `A2AConfig.task_retention_seconds` (default 1 hour) or past
  `max_retained_tasks` (default 10,000); `GetTask` and `CancelTask` then
  treat them as unknown. Callers that read finished tasks later should raise
  those settings or pass a persistent `A2AConfig(task_store=...)` (#208).
- `ctx.remote_skills()` with no `Remote` parameters, `agents=`, `skill=` or
  `tags=` now raises `DefinitionError`; narrow the catalog, or pass
  `all_registered=True` to offer every registered agent deliberately (#263).
- In a run hosted by an `AgentApp`, a skill without `requires_permissions`
  whose model may choose remote calls made with the agent's own credentials
  (a `Service` in `remotes=` or as `default_remote`) through
  `ctx.generate(tools=...)` or `ctx.run_plan` is now refused with
  `PermissionDeniedError` before anything is sent. Add `requires_permissions`
  to the skill, call the agents `OnBehalfOf` the user, or pass
  `allow_any_caller=True` to `remote_skills` when any caller may use the
  agent's credentials (#263).
- A skill method can no longer be named like one of `Agent`'s own attributes,
  such as `ask`, `invoke`, `router` or `definition`; `@agent` raises
  `DefinitionError`. Rename the method and keep its ID, for example
  `@skill("ask") async def answer(...)` (#280).
- The routing module moved from `agentframe.hosting.routing` to
  `agentframe.runtime.routing`. Imports from `agentframe` (`SkillRouter`,
  `SkillCall`, `Clarification`, `RoutingContext`) are unchanged (#280).
- `FoundryModel` error messages no longer say "Foundry" (see **Changed**);
  code that matches their text should match the new wording (#259).
- The Agent Card now advertises `capabilities.streaming`, so official A2A SDK
  clients use `SendStreamingMessage` by default. Their final task is the same
  as before; clients that need a single response can turn streaming off in
  their client configuration (#257).

### Added

- **Development:** `agentframe new agent NAME` and `agentframe new fleet NAME
  [--planner] [--agents research,writer]` create a working project from
  templates shipped in the wheel: one agent, or specialists with skills
  tagged `content` plus an optional tag-discovering planner, with
  `create_app()` factories, `agentframe.yaml`, `agents.yaml`, tests, a
  `pyproject.toml` with Ruff, mypy and pytest configured, and a README. The
  generated code passes those checks, uses a development API key,
  `model_from_env()` and `resources=`, and is yours to edit; nothing is
  regenerated. Existing files are never overwritten without `--force`
  (#277).
- **Examples:** [`examples/dynamic_agents`](examples/dynamic_agents), a
  planner that names no agent. Its `run_goal` skill plans over every
  registered skill tagged `content` (`ctx.remote_skills(tags=["content"])`),
  runs the plan with `ctx.run_plan(..., on_failure="continue")` and reports
  each step's status; registering a third tagged agent needs no code change.
  It calls the model-free research and writer specialists with its own
  development key through `default_remote=` (the docstring explains
  `OnBehalfOf` for production), requires `plans:run` and refuses plans of
  more than three steps with an `ExecutionPolicy` plan check. Planning uses
  Ollama through `AGENTFRAME_OLLAMA_MODEL`; without it the agent starts and
  `run_goal` fails clearly. Run it with `agentframe dev`; `agents.yaml` serves
  agents started by hand (#245).
- **Discovery:** `FoundryA2AToolRegistry(project_endpoint=..., credential=...)`
  lists a Microsoft Foundry project's A2A connections (type `RemoteA2A`) as
  agent references, following every page: each is named after its connection,
  with its target plus the connection's `AgentCardPath` metadata (else
  `/.well-known/agent-card.json`, or a Foundry agent's `/agentCard/v1.0`) as
  its card URL. Read-only; connection credentials are never requested, and the
  registry's own token is sent only to cards inside its project. Needs the
  `foundry` extra (#244).
- **Authoring:** `await agent.invoke("skill", **arguments)` runs one skill
  locally and returns its validated value, and `await agent.ask(prompt)`
  answers a plain prompt: the agent's only skill taking a single `str` gets
  the prompt with no model call; otherwise a router (`router=` or
  `Agent(router=...)`) chooses, with its model calls counted toward the run's
  limits and a question raised as `ClarificationNeededError`; otherwise
  `DefinitionError` lists the skills. Both accept `runtime=`, `principal=` and
  `options=` and run the full `Runtime` pipeline (#280).
- **Documentation:** a
  [stability, deprecation and upgrade policy](docs/guides/stability.md): what
  the public API is, what may change before 1.0 (minor releases may break,
  patch releases do not), how deprecations are signposted (a
  `DeprecationWarning` for at least one minor release, **Deprecated** and
  **Removed** changelog entries), **Upgrading** notes in this changelog and the
  criteria for 1.0 (#251).
- **Documentation:** `docs/api/hosting.md` and `AgentApp`'s docstring no
  longer say tasks are always lost on restart, since `A2AConfig(task_store=...)`
  can keep them, and `docs/api/decorators.md` no longer says tools are not
  registered or executed (#251).
- **Hosting:** `AgentApp(default_skill="answer")` sends free-text A2A messages to that
  skill with no router and no model call, so plain A2A clients can call a
  text agent. The skill's only business parameter must be a `str`, checked
  at construction (`DefinitionError` otherwise); the message's text parts
  become that argument and a message without text is `REJECTED`. Validation,
  permissions and policies still apply, and a `router` decides instead when
  given (#270).
- **Evaluation:** an HTML report. `EvalReport.write_html(path, *, baseline=None)`
  and `agentframe eval --html PATH` write one self-contained page (inline CSS
  and a small sort/filter script, no network fetches, hash-locked
  Content-Security-Policy, every value HTML-escaped) from the same data as the
  JSON report, following `capture_content`. `baseline=` / `--baseline OLD.json`
  compares with an earlier JSON report: newly failing and newly passing cases
  plus pass-rate and latency changes (#276).
- `agentframe.model_from_env(prefix="AGENTFRAME_", *, credential=None)`
  chooses `OllamaModel` or `FoundryModel` from `MODEL_PROVIDER`,
  `OLLAMA_MODEL`, `OLLAMA_ENDPOINT`, `FOUNDRY_ENDPOINT`, `FOUNDRY_DEPLOYMENT`
  and an optional `MODEL_CONTEXT_WINDOW` (Ollama defaults to 8192), importing
  only the selected provider. Errors name the missing or invalid variable,
  never its value. Without `credential=`, Foundry signs in with a
  `DefaultAzureCredential` that the model closes with itself. The examples
  use it instead of their copied `create_model()` helper (#265).
- **Hosting:** `agentframe run --log-level {warning,info,debug}` and
  `--log-format {text,json}` for production logs. `info` shows each run's
  `Run started` / `Run completed` records; `json` writes one JSON object per
  line (standard library only), Uvicorn's records included, with the run's
  `agent`, `invocation_id`, `root_invocation_id` and `parent_invocation_id`
  as fields. Defaults are unchanged: `text`, and `warning` except `info`
  under `agentframe dev` (#271).
- **Performance baseline:** opt-in benchmarks in `tests/benchmarks/`
  (`AGENTFRAME_BENCHMARK=1`, marker `benchmark`) measure, with
  `ScriptedModel`, the overhead of a local run (about 0.05 ms), a hosted A2A
  call (about 5 ms on localhost), an A2A hop through `Remote.call` (about
  265 ms, almost all the first 0.25-second task poll) and throughput at the
  admission limit. The numbers and their caveats are in
  [the performance baseline](docs/guides/performance.md); a new non-gating
  `benchmarks` CI job reruns them and fails only past generous ceilings
  (#256).
- **Models:** `AnthropicModel` calls Claude models through the Anthropic
  Messages API with the official `anthropic` client, installed by the new
  `anthropic` extra (`pip install "agentframe-py[anthropic]"`). It supports
  text, structured output, tool calling and usage like `FoundryModel` and
  `OllamaModel`, reads the context window and structured-output support from
  the Models API, and is covered by opt-in real-provider tests (marker
  `anthropic`) (#262).
- **Hosting:** `AgentApp(tool_authorization=...)` checks every tool call a
  model asks for, like `Runtime(tool_authorization=...)`, so adding the policy
  no longer means switching to `runtime=` and moving `services=` and the other
  settings onto it. Passing it together with `runtime=` raises
  `DefinitionError`; `examples/orders.py` uses it (#266).
- **Foundry hosted tools:** `ctx.generate(..., tools=[...])` accepts
  `FoundryCodeInterpreter`, `FoundryWebSearch(allowed_domains=...)` and
  `FoundryFileSearch(vector_store_ids=...)`, which `FoundryModel` sends as the
  Responses API's built-in tools. They are opt-in per call and
  provider-scoped: other models (`OllamaModel`, or any model not listing the
  type in the new `ModelCapabilities.hosted_tools`) raise
  `UnsupportedCapabilityError` before sending. Hosted calls count toward
  `RunLimits.max_tool_calls` and `RunResult.tool_calls`; each request caps the
  provider at the run's remaining tool calls (`max_tool_calls`) and is refused
  when none remain. New `HostedTool` base class, `ModelRequest.hosted_tools` /
  `max_hosted_tool_calls`, `ModelResponse.hosted_tool_calls` and
  `ModelError.hosted_tool_calls` (calls a failed response ran, also counted).
  Web search sends data outside Azure's compliance boundary and is billed
  separately (#261).
- **Models:** plain OpenAI through `FoundryModel(client=openai.AsyncOpenAI(),
  deployment="<model>")`. Plain OpenAI serves the same Responses API, so no new
  class or alias is needed; `docs/api/models.md#plain-openai` documents it, and
  opt-in `openai` real-provider tests (`AGENTFRAME_TEST_OPENAI=1`) run the
  shared provider tests against it (#259).
- **Streaming (#257):** a skill opts in per call with
  `ctx.generate(..., stream=True)`, and its answer text is passed on as it is
  generated: over A2A `SendStreamingMessage` (the Agent Card now advertises
  `capabilities.streaming`) as artifact text deltas (combined into a bounded
  number of updates per task) followed by the whole
  validated answer, and locally through `Runtime.run(..., on_text=...)`.
  `OllamaModel`, `FoundryModel` and `ScriptedModel` implement the new optional
  `StreamingModel.stream`; other models answer whole. Output checks turn
  streaming off, so only checked answers are released; every run limit and
  `max_response_bytes` still apply, and a model call that already sent text is
  never retried. Official SDK clients now stream by default against
  AgentFrame agents, since the card advertises it; pass
  `ClientConfig(streaming=False)` to keep using `SendMessage`.

- **Conversation history:** `ctx.generate(history=...)` sends earlier
  `user` and `assistant` messages, keeping the newest that fit the first
  request's input, context-window and remaining-token limits.
  `ctx.history()` and `ctx.save_turn()` read and add to the caller's
  conversation in the runtime's `StateStore`, a structural protocol
  (`load`, `append`) set with `Runtime(state_store=...)`. The default
  `InMemoryStateStore` is bounded (1000 conversations, 100000 characters
  each, one hour idle). Conversations are keyed by `ConversationKey`: the
  agent, the verified caller and the A2A `contextId` (or `conversation_id=`
  on `Runtime.invoke` / `run`), so a context ID alone never reaches another
  caller's history; anonymous hosted callers get none (#252).
- **MCP client:** the optional `mcp` extra (official MCP Python SDK 2.x)
  adds `MCPClient`, an application-configured MCP server over stdio or
  streamable HTTP injected as a service. A skill selects tools by name with
  `await client.tools(...)` and passes the `MCPTool` entries to
  `ctx.generate(tools=[...])`; calls count toward `max_tool_calls`, honor
  per-tool `permissions=` and execution policy tool checks, and are bounded
  by `call_timeout_seconds` and `max_result_bytes`. Nothing is offered
  implicitly; a broken connection raises `MCPServerError` (#253).
- **Hosting:** shared deduplication and per-caller rate limiting (#255).
  `RedisMessageDedupStore(client, key_prefix="agentframe:dedup:")` is a
  `MessageDedupStore` over an application-owned `redis.asyncio.Redis` client,
  claiming each `messageId` atomically with one `SET NX GET PX` (Redis 7.0+),
  so instances sharing Redis and a task store return a retry's original task;
  install the new `redis` extra (`redis>=8.1.0,<9`). The in-memory store stays
  the default. `HostConfig(rate_limit=RateLimit(requests=..., per_seconds=60))`
  gives each verified caller a token bucket; a `SendMessage` beyond it gets
  HTTP 429 with `Retry-After` after authentication and before deduplication
  and admission, with no task. Anonymous callers share one allowance; the count
  is per instance, so gateway rate limiting (for example Azure API Management)
  is documented for limits across instances. Off by default.
- **Evaluation:** judge vote counts. `Score` and `ScoreResult` gain
  `votes_cast` and `votes_passed`; every `Judge` score sets them to the judge
  runs actually made, so an early stop shows (a unanimous `votes=3` is `2/2`,
  a unanimous `votes=5` is `3/3`, though both have a `value` of 1). The
  console summary shows them as `judge=pass(0.666667, 2/3 votes)` and the JSON
  report's `scores` as `votes_cast` / `votes_passed` (`null` unless the
  scorer sets them), kept without `capture_content`; the schema stays
  `agentframe.evaluation.report/1` (#213).
- `A2AConfig(task_store=...)` accepts any `a2a-sdk` `TaskStore`, for example
  a database store, instead of the default in-memory one; shutdown still waits
  for final states to be saved (#208).
- `AgentApp` construction logs a warning, without failing, when `HostConfig`
  is legal but insecure: a plain `http://` `public_url` on a non-loopback
  host with credential-bearing authentication, or `AllowAnonymous()` on a
  non-loopback `public_url`. Loopback hosts and `AGENTFRAME_DEV=1` (set by
  `agentframe dev` and `agentframe compose`) never warn (#267).
- `APIKeyAuth(header=...)` accepts the key in another request header, for
  example `Ocp-Apim-Subscription-Key` behind Azure API Management, matching
  outbound `APIKeyCredential(header=...)`; the default stays `X-API-Key`. The
  Agent Card's `apiKey` scheme and the 401 `WWW-Authenticate` challenge name
  the configured header, and the host keeps it out of the SDK's debug logs
  like `X-API-Key`. `APIKeyAuth(hashed=True)` takes SHA-256 hex digests as
  the `keys` mapping's keys, so configuration need not hold plaintext keys
  (#269).
- `AnyOf(EntraIDAuth(...), APIKeyAuth(...))` accepts callers that any one of
  several authenticators verifies, chosen by which credential header the
  request carries (the first matching member's answer is final). The Agent
  Card lists one security requirement per member as alternatives, and
  `OnBehalfOf` remotes keep working for Entra users through the member that
  exchanges user tokens (#268).
- `AgentApp(resources=[...])` opens app-owned resources, such as models and
  Azure credentials (async context managers or objects with `aclose()`), at
  server startup before the startup checks and closes them in reverse order
  after shutdown, or when startup fails. Only what is listed is closed. An
  `AgentApp(directory=...)` that is not open yet is now opened and closed the
  same way, so an `agentframe run` factory no longer needs its own
  `http_client=` (#264).

### Changed

- Orchestration fails closed (#263). Skill catalogs must be explicit:
  `ctx.remote_skills()` alone raises `DefinitionError`, and the new
  `all_registered=True` uses the whole registry deliberately. In hosted runs a
  skill without `requires_permissions` cannot let a model choose calls made
  with the agent's own `Service` credentials (the open-proxy guard), unless
  its catalog passes the new `allow_any_caller=True`; `OnBehalfOf` and open
  agents pass. The first catalog built for each hosted skill logs its agents'
  identity mode (`Service`, `OnBehalfOf`, `none` or `mixed`) at INFO.
- **Examples:** `examples/assistant.py`, `orders.py` and `routed_assistant.py`
  no longer cap `max_input_tokens` on the agent class for Foundry's sake; the
  context window is model configuration, so their Foundry instructions set
  `AGENTFRAME_MODEL_CONTEXT_WINDOW` for `model_from_env()`. `examples/echo.py`
  drops its `DevelopmentPolicy`: `authorization=` is for resource checks, and
  authenticated callers can already run skills without `requires_permissions`
  (#272).
- `ClarificationNeededError.task_id` and `context_id` are `None` when
  `Agent.ask`'s router asked the question, since no task runs (#280).
- `Services.add` and `Services.add_scoped` return the `Services` instead of
  `None`, so registrations fit on one line:
  `Runtime(services=Services().add(OrderStore, store))` (#274).
- `@skill`'s `id` is now optional and defaults to the method name, so
  `@skill(tags=["questions"])` on `async def answer` registers the skill
  `answer`. Pass `id=` to keep a published ID when renaming a method; an
  empty `id` is still rejected (#273).
- `FoundryModel` error messages name "the model service" instead of Foundry,
  since the same adapter also calls plain OpenAI (for example "The model
  service rejected the credentials (HTTP 401)"). An unknown deployment or
  model reads "Model or deployment '<name>' was not found", now also when the
  service answers HTTP 400 with code `model_not_found`, as OpenAI may (#259).
- `dev_settings().directory` no longer creates its own never-closed HTTP
  client: `AgentApp(directory=...)` opens it at startup and closes it after
  shutdown. Code using it outside `AgentApp` opens it with `async with` (#264).
- Runtime dependencies and the `server`, `ollama`, `foundry` and `entra`
  extras are now compatible ranges instead of exact pins, from the lowest
  tested version up to the next breaking release (next major, or next minor
  for 0.x packages), for example `a2a-sdk[http-server]>=1.3.0,<2` and
  `httpx>=0.28.1,<0.29`, so AgentFrame installs alongside other packages
  needing newer compatible versions. The Pydantic minimum rises from 2.14 to
  2.14.2, the lowest version the tests pass with. A new `lowest-deps` CI job
  runs the tests on the lowest allowed versions; `uv.lock` stays exact (#219).
- `examples/multi_agent`: the research agent and the planner start without
  Ollama (#218). They use local Llama through Ollama only when
  `AGENTFRAME_OLLAMA_MODEL` names an installed model tag, instead of defaulting
  to `llama3.2:3b`; without it their model-backed skills fail with "This agent
  has no model configured" and the others still work.
- `FoundryAgentRegistry` requests now time out after 10 seconds to connect and
  10 seconds between reads, instead of `azure-core`'s 300-second defaults, and
  `OllamaModel` connects within 10 seconds instead of waiting forever; Ollama
  reads stay bounded by the run's deadline, so long generations are not cut
  short (#220).
- **Documentation:** the README, which is also the PyPI project page, is now a
  short front page: features, installation, a quick start, using a model,
  multi-agent, examples and links. Development setup, checks and pull request
  rules moved to a new `CONTRIBUTING.md`; the real-provider tests and the
  Foundry workflow setup to a new `docs/guides/provider-tests.md`; and the
  success criterion, goals, scope and references to `docs/roadmap.md`. The
  README's code samples are now tested as written.

### Fixed

- The directory from `dev_settings()` no longer leaves connections open when
  it is discarded, which raised `ResourceWarning`s, for example in tests that
  build several development apps in one process. Its HTTP client, open for
  the life of the process, now keeps no idle connections (#221).
- A `Remote` parameter annotated with an alias of an alias
  (`type A = Remote[Literal["x"]]; type B = A`) now resolves its agent name
  instead of failing with "unsupported type annotation". Aliases are read
  through up to 10 levels; a deeper or self-referencing alias raises
  `DefinitionError`, and an invalid `Remote` annotation's error now names the
  parameter (#214).
- The plan executor behind `ctx.run_plan` and the A2A executor check their
  internal invariants (a running skill for policy checks, a step's catalog
  entry and binding, a token limit, the task's IDs and message, a
  `max_remote_hops` limit, a router for free text) with explicit checks that
  raise `RuntimeError`, instead of `assert`, which `python -O` removes. A
  plan whose step group fails without a recorded step failure re-raises the
  exception group instead of dropping it. Normal operation is unchanged
  (#215).
- The default in-memory A2A task store no longer keeps every task until the
  process exits: finished tasks are deleted after
  `A2AConfig.task_retention_seconds` (default 1 hour) or past
  `max_retained_tasks` (default 10,000), oldest first. Unfinished tasks are
  never deleted; `GetTask` and `CancelTask` treat a deleted task as unknown
  (#208).

## [0.3.0] - 2026-11-14

The evaluation and model-planned orchestration release (M9 and M10).
`agentframe.evaluation` runs typed cases against a local skill or any A2A
agent, scores the results with plain functions, built-in scorers or a rubric
judge within an optional evaluation-wide token budget, and writes
private-by-default reports for pytest, CI and `agentframe eval`. Orchestrator
skills can offer registered agents' skills to their model as tools, or have it
make a checked plan with `ctx.plan` and run it with `ctx.run_plan`, under the
existing deadline, hop, token, identity and policy controls; plain `await`
stays the default.

### Added

- **Orchestration (M10):** the [orchestration guide](docs/guides/orchestration.md):
  choosing plain `await`, remote skills as tools or plans, the catalog, a
  safety checklist with the open-proxy and `default_remote` warnings, logs and
  usage, and measured planner rates. `examples/multi_agent` gains a model-free
  writer agent and the planner's `propose` and `planned` skills: the model
  plans a goal over a narrowed catalog of the research and writer agents, and
  `planned` runs it with `on_failure="continue"`, behind `requires_permissions`,
  per-entry `permissions=`, a plan check and a tool check. Its hand-written
  `plan` skill now returns `ResearchPlan` (formerly the example's own `Plan`
  model), since `Plan` is AgentFrame's.
  `examples/multi_agent/planner_evaluation.py` evaluates the planner's plans
  by structure (entries used and dependencies) with M9. The release
  acceptance suite `tests/integration/test_orchestration_release.py` runs the
  agents, a plain A2A peer and two orchestrators as separate processes; opt-in
  Ollama and Foundry planner tests are in `test_providers.py` (#176).
- **Orchestration (M10):** plans are logged at INFO with the run's correlation
  fields (`agent`, `invocation_id`, `root_invocation_id`,
  `parent_invocation_id`) in `extra`: `ctx.plan` logs `Plan created` with
  `step_count` and the entry names (`uses`), and `ctx.run_plan` logs
  `Plan step started` and `Plan step completed` with `plan_step`, `use`,
  `status`, `error_type`, `remote_task_id`, `duration_ms` and `total_tokens`
  (`None` when unknown); a skipped step gets only `Plan step completed`.
  Arguments, text, results, error messages and questions are never logged; no
  new wire field identifies a step. `StepResult` gains `use` and `duration_ms`
  (`None` when skipped), and `PlanResult.usage` adds up the usage of the steps
  that were sent, `None` when any of them is unknown; both are views of usage
  the run charged once (#173).
- **Orchestration (M10):** `ctx.run_plan(..., on_failure="continue")` keeps a
  step's remote `RemoteAgentError` or `ClarificationNeededError` as its result
  instead of raising: `StepResult.status` widens to `"completed"`, `"failed"`,
  `"cancelled"`, `"input_required"` and `"skipped"`, with new `error_type`,
  `error_message` (class name and safe message only) and `question` fields.
  Steps depending on an unfinished step are skipped and never sent; independent
  steps still run. New `PlanResult.complete`; `PlanResult.value` is `None` when
  the `result` step did not complete. Limits, authorization, policy, invalid
  references and cancellation always stop the plan; nothing is retried. The
  default stays `"stop"` (#172).
- **Evaluation (M9):** `examples/evaluation.py` evaluates
  `examples/assistant.py`: `create_evaluation()` checks the model-free
  `count_words` skill (runnable with `agentframe eval`), and
  `create_answer_evaluation(model, judge_model, votes=1)` checks the `answer`
  skill with `contains()` and a rubric `Judge` under an evaluation-wide budget.
  `examples/smoke.py` runs both against the installed wheel. The
  [evaluation guide](docs/guides/evaluation.md) now walks through datasets,
  targets, scorers, the judge and the cost of votes, budgets, reports, CI
  gating with `require` or `agentframe eval`, and privacy;
  `tests/integration/test_evaluation_release.py` is the M9 release
  acceptance suite, with opt-in Ollama and Foundry runs of the example in
  `tests/integration/test_providers.py` (#163).
- **Orchestration (M10):** `ExecutionPolicy(plan_checks=[...])`: async
  `(skill, principal, plan) -> bool` checks (new `PlanCheck` protocol) that
  `ctx.run_plan` runs once, after the catalog and permission re-check and
  before any step counts as a tool call or is sent. Each check gets its own
  deep copy of the plan. Fail-closed like the other checks: anything but
  `True` raises `PermissionDeniedError` ("The plan was blocked by an execution
  policy check"); a raise or the run's deadline `PolicyUnavailableError`.
  Per-step tool checks still run. New `examples/plan_approval.py` and docs
  show the propose/execute approval pattern (one skill returns `ctx.plan`,
  a `requires_permissions`-protected skill runs the plan the caller sends
  back, which is checked again as untrusted input), with the open-proxy
  warning for `Service` identity (#174).
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
  `json.loads` only; a leading byte order mark is accepted) whose
  `DefinitionError`s name the file and line but never the case content.
  `import agentframe` does not load the package, and it adds no dependency
  (#152).
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
  (with messages) of non-AgentFrame exceptions (#153).
- **Evaluation (M9):** built-in scorers `equals` (output equals the case's
  `expected`, Pydantic models field by field), `contains(text=None,
  case_sensitive=False)` (text output contains `text` or the case's
  `expected`, ignoring case by default) and `valid(T)` (output validates as
  `T` with Pydantic, string output also tried as JSON; the failing reason
  names problem locations only). A case without the `expected` value a scorer
  needs, or with non-text output or `expected` for `contains`, is `errored`
  with a message naming the case ID and type, never the content (#154).
- **Development:** a scheduled weekly workflow compares the `python:3.13-slim`
  digest pinned by the `agentframe compose` Dockerfile template with the
  tag's current digest and opens an issue when it moves (#151).

### Fixed

- **Security:** every tool check and the `ToolAuthorizationPolicy` now get
  their own read-only deep copy of a tool call's arguments, for `@tool`
  functions, remote skills offered as tools and plan steps. Before, the
  read-only mapping was shallow, so a check could change a nested value that
  later checks saw and the call used (#223).
- **Packaging:** the README links work on the PyPI project page (they are now
  absolute GitHub URLs), and the project lists its documentation, changelog
  and issues URLs (#223).
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

[0.3.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.3.0
[0.2.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.2.0
[0.1.0]: https://github.com/malcmiller/agentframe/releases/tag/v0.1.0
