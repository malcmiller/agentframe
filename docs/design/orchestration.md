# Model-planned orchestration

**Status: planned (M10), not implemented.** Nothing on this page exists in
`agentframe` yet. Every name, default and signature below is a proposal; the
[open questions](#open-questions) await the maintainer's confirmation and are
tracked in the [M10 overview](https://github.com/malcmiller/agentframe/issues/177).

Since M8+, an orchestrator is an ordinary agent whose skill calls specialists it
declared as `Remote[Literal["name"]]` parameters, using plain `await` and
`asyncio.TaskGroup` ([discovery](discovery.md#agent-injection-and-separate-invocation)).
The author decides in code which agent and skill run, in what order. That is
the right default, but it means a model cannot decide which agent to use, and
an agent found with `directory.find(...)` cannot be called at all.

This design lets an orchestrator:

1. build a **catalog of callable remote skills** from its agents' Agent Cards
   (skills, argument schemas from AgentFrame's skill-call extension, free-text
   `ask` for other A2A agents, descriptions and tags), and
2. let its **model choose**: one call at a time through the existing tool loop,
   or a whole **execution plan** (which agents and skills, in what order, in
   parallel where possible, with results flowing between steps) that AgentFrame
   checks and runs with its existing guarantees.

## Goals

- **Owned, small API.** Catalog, plan and run in the same style as the rest of
  AgentFrame: typed values, one `RunContext` method per action, plain Python
  around it. Not a wrapper around LangGraph, Semantic Kernel/Microsoft Agent
  Framework, AutoGen or CrewAI
  ([dependency boundaries](../roadmap.md#independence-and-dependency-boundaries)).
- **Cards are the source.** The catalog comes from native A2A 1.0 Agent Cards,
  so AgentFrame and non-AgentFrame agents (including Foundry agents) both work.
- **Reuse every guarantee.** Remote steps go through the existing `Remote`
  binding: deadline, hop depth, outbound identity, trace/correlation, token
  holds and usage reconciliation stay exactly as in M8+. Nothing a plan does can
  exceed a host ceiling.
- **Control flow is checked before it runs.** A plan is a typed value that is
  validated against the catalog, limits and policy before anything is sent.
- **Opt-in and incremental.** Plain `await` stays the default. Each story is
  usable on its own; the first usable step is remote skills as model tools.

## Non-goals

- A workflow or graph engine: no loops, conditionals, author-facing graph DSL,
  durable or checkpointed plans, pause/resume or crash recovery. Durable
  workflows stay a [revisit after real usage](../roadmap.md#reviewed-release-decisions)
  in a separate optional package (candidate `agentframe-workflows`).
- Automatic replanning, automatic retries of remote steps or built-in human
  approval.
- New A2A wire fields. Plans run as ordinary `call`/`ask` requests.
- Tool retrieval or ranking over large catalogs (hundreds of agents). The
  catalog is small and curated, like `find`'s sources.
- MCP tool catalogs. MCP stays an optional adapter
  ([#18](https://github.com/malcmiller/agentframe/issues/18)).

## Revisiting the M8+ scope cuts

The [M8+ overview (#17)](https://github.com/malcmiller/agentframe/issues/17)
listed as out of scope "orchestration helpers (plain `await` and
`asyncio.TaskGroup` only)" and "dynamic invocation of agents found at run
time"; #85 (decision 2) deferred calling found agents "to a later story driven
by real usage"; #87 refused `Remote` parameters on `@tool` functions; and the
roadmap says orchestrators are "not a workflow/graph engine", with helpers
"later only after repeated usage". Those were first-release cuts, not
permanent rules.

M10 deliberately revisits two of them, as opt-in features:

| M8+ cut | M10 proposal | Why now |
| --- | --- | --- |
| No orchestration helpers | `ctx.remote_skills`, `ctx.generate(tools=[*skills])`, `ctx.plan`, `ctx.run_plan`. Plain `await`/`TaskGroup` remain the default and are still documented first. | The multi-agent example (#97) shows the pattern: every new specialist needs hand-written glue, and the orchestrator cannot let its model choose. |
| No dynamic invocation of found agents | `remote_skills(find=...)` only with an application allowlist (`discoverable=`) and per-name identity ([#175](https://github.com/malcmiller/agentframe/issues/175)). | Lets new specialists join without redeploying, without trusting every listed agent. |
| `@tool` cannot take `Remote` | Kept. Catalog entries are the supported way to expose remotes to a model. | One path keeps identity and limits in one place. |

Kept unchanged: no durable workflows, pause/resume or built-in approval; no
typed `Remote[ResearchAgent]` proxy; no registration writes or deployment; no
federation. A plan is a value inside one skill run, executed and discarded with
that run. It is not a workflow definition that authors program against.

## What current practice shows

The design borrows ideas, not code or dependencies:

| Source | Shape | What AgentFrame takes from it |
| --- | --- | --- |
| [ReAct (Yao et al., 2022)](https://arxiv.org/abs/2210.03629) | Interleave reasoning and one tool call at a time. | Already AgentFrame's M5 tool loop; remote skills as tools reuse it. Adaptive, but every step re-reads the context and untrusted results steer the next call. |
| [ReWOO (Xu et al., 2023)](https://arxiv.org/abs/2305.18323) | Planner writes all steps with evidence variables (`#E1`), workers fill them, a solver answers. Reports large token savings over ReAct. | Plan once, reference earlier results by name, no model between steps. |
| [Plan-and-Solve (Wang et al., 2023)](https://arxiv.org/abs/2305.04091) | Ask the model to devise a plan before solving. | One planning call before execution. |
| [LLMCompiler (Kim et al., 2023)](https://arxiv.org/abs/2312.04511) | Planner emits a DAG of tasks with `$1` placeholders; an executor runs independent tasks in parallel; a joiner may replan. | Flat DAG, placeholder references, parallel execution of ready steps. AgentFrame leaves replanning to the author. |
| [Magentic-One (Fourney et al., 2024)](https://arxiv.org/abs/2411.04468) | An orchestrator keeps a task ledger and progress ledger, delegates to specialist agents and replans when stuck. | Per-step status and usage ("ledger") for observability; replanning only as an explicit author step. |
| [Anthropic, *Building effective agents*](https://www.anthropic.com/engineering/building-effective-agents) | Distinguishes workflows (predefined paths) from agents; recommends the simplest pattern that works; "orchestrator-workers" for unpredictable subtasks. | Keep plain code as the default; offer orchestrator-workers as an opt-in. |
| [A2A agent discovery](https://a2a-protocol.org/latest/topics/agent-discovery/) | Well-known cards, curated registries, direct configuration; selection by skills, interfaces and security. | Catalog from cards in AgentFrame's existing curated directory; security requirements decide eligibility. |
| [OpenAI function calling](https://platform.openai.com/docs/guides/function-calling) | Advises fewer than 20 functions at a time for accuracy; tool schemas count as input tokens. | At most 20 catalog entries, bounded descriptions and schemas. |
| [RAG-MCP (Gan & Sun, 2025)](https://arxiv.org/abs/2505.03275) | Selection accuracy drops sharply as tool count grows ("prompt bloat"); retrieves a few tools first. | Keep catalogs small and narrowable (`only=`); retrieval is out of scope. |
| [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) | Tool descriptions and annotations are untrusted unless from a trusted server; hosts should confirm sensitive calls. | Card text is untrusted data; permissions and checks before calls. |

Security research shapes the boundaries:

- [Beurer-Kellner et al., *Design Patterns for Securing LLM Agents against
  Prompt Injections* (2025)](https://arxiv.org/abs/2506.08837) describe
  **plan-then-execute**: fix the actions before the agent reads untrusted
  content, so injected text can change data but not which actions run.
  [CaMeL (Debenedetti et al., 2025)](https://arxiv.org/abs/2503.18813) goes
  further, separating control flow from data flow with capabilities on values.
  AgentFrame plans follow the first pattern: no model reads step results
  between steps, and references copy values without interpreting them.
- [Tool poisoning](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks)
  hides instructions in tool descriptions. Agent Card descriptions are the same
  risk, so card text is bounded and passed as data, never as instructions.
- [The lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)
  (private data, untrusted content, a way to send data out) applies to any plan
  that moves results from one agent to another. Plan checks can inspect every
  data reference before execution.
- OWASP's [LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
  and [LLM10 Unbounded Consumption](https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/)
  motivate narrow catalogs, per-call permissions and hard step, parallelism and
  token limits. With service identity, a model choosing calls is a classic
  confused deputy: the orchestrator's authority used on a caller's behalf.

## API sketch

Illustrative until built. Public names are importable from `agentframe`.

```python
# proposed, not implemented
from typing import Literal

from agentframe import Agent, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]
type Writer = Remote[Literal["writer-agent"]]
type Legacy = Remote[Literal["legacy-agent"]]  # a non-AgentFrame A2A agent


@agent(name="Orchestrator", version="0.1.0")
class Orchestrator(Agent):
    """Answers goals by delegating to specialist agents."""

    @skill(id="answer", tags=["orchestration"])
    async def answer(
        self, goal: str, research: Research, writer: Writer, legacy: Legacy, ctx: RunContext
    ) -> str:
        """Let the model call specialists one at a time."""
        skills = await ctx.remote_skills(research, writer, legacy)
        return await ctx.generate(goal, tools=[*skills])

    @skill(id="report", tags=["orchestration"])
    async def report(
        self, goal: str, research: Research, writer: Writer, ctx: RunContext
    ) -> str:
        """Plan the whole job first, then run it with parallel steps."""
        skills = await ctx.remote_skills(research, writer)
        plan = await ctx.plan(goal, skills, max_steps=6)
        result = await ctx.run_plan(plan, skills, max_parallel=4)
        return str(result.value)
```

A plan the model might produce for "write a post about tides and waves":

```json
{
  "steps": [
    {"id": "tides", "use": "research-agent.find_sources", "arguments": {"topic": "tides"}},
    {"id": "waves", "use": "research-agent.find_sources", "arguments": {"topic": "waves"}},
    {"id": "brief", "use": "research-agent.brief",
     "arguments": {"sources": {"$ref": "tides"}, "more_sources": {"$ref": "waves"}}},
    {"id": "post", "use": "writer-agent", "text": "Write a short post from this brief: {brief}"}
  ],
  "result": "post"
}
```

`tides` and `waves` run in parallel, `brief` waits for both, and `post` asks the
writer in free text with the brief inserted.

```mermaid
flowchart LR
  tides[tides: research-agent.find_sources] --> brief[brief: research-agent.brief]
  waves[waves: research-agent.find_sources] --> brief
  brief --> post[post: ask writer-agent]
```

## Remote skill catalog

`await ctx.remote_skills(*remotes, only=None, ask=False, permissions=None)`
returns a `SkillCatalog` ([#166](https://github.com/malcmiller/agentframe/issues/166)):
an immutable list of `RemoteSkill` entries plus the run's bindings.

| `RemoteSkill` field | Meaning |
| --- | --- |
| `name` | `"agent.skill"` for a structured skill, `"agent"` for a free-text `ask` entry. Used in plans, logs and policy checks. |
| `agent` | The directory name. |
| `skill` | The card skill ID, or `None` for `ask`. |
| `description`, `tags` | From the card, bounded (see below). |
| `arguments_schema` | The JSON Schema from the card's skill-call extension (`params.skills[id]`), or `None` for `ask`. |
| `requires_permissions` | Permissions the caller must hold; empty unless the author sets `permissions=` ([#168](https://github.com/malcmiller/agentframe/issues/168)). |

An agent whose card declares AgentFrame's skill-call extension contributes one
entry per card skill. An agent without it (any non-AgentFrame agent) contributes
one `ask` entry describing the agent; its card skills are descriptive only,
because base A2A has no standard way to select a skill. `ask=True` adds `ask`
entries for extension agents too. Cards carry argument schemas but not result
schemas, so results are untyped JSON or text until the author validates them.

The name "capability" is avoided because A2A already uses `AgentCapabilities`
for streaming and extensions on the card.

### Eligible agents

- **Declared remotes (first).** Only `Remote` parameters the skill declares.
  They are validated at startup, their `remotes=` identity is configured, and
  their names are visible in the skill's signature, so a reviewer sees every
  agent the model could reach.
- **Discovered agents (later, opt-in).** `remote_skills(find=Find(tags=[...]))`
  runs `directory.find` and keeps only agents an application allowlist accepts:
  `AgentApp(discoverable=AllowAgents(names=[...], tags=[...]))`
  ([#175](https://github.com/malcmiller/agentframe/issues/175)). Without
  `discoverable=`, `find=` is a `DefinitionError`. Being listed in a directory
  is never permission to be called.

### Identity

Every entry uses its agent's existing outbound identity: the `remotes=` entry
(`Service(credential=...)` or `OnBehalfOf(scope=...)`), or no credentials for an
open card. For discovered agents, identity is per name only: no wildcard or
default credential, and a secured card without its own entry is left out of the
catalog with a warning. A default credential would give the orchestrator's
identity to any agent that later appears in a source.

### Untrusted card text

Card names, descriptions, tags and schemas come from other parties. The catalog:

- cuts descriptions to 500 characters, keeps at most 10 tags, removes control
  characters and does not copy card `examples`;
- refuses more than 20 entries or a schema over 8 KiB with `DefinitionError`
  (narrow with `only=`), keeping tool-selection accuracy and prompt cost
  bounded;
- is shown to the model only as quoted data in the user message or as tool
  definitions, never inside trusted system instructions.

Bounding text reduces, but cannot remove, injection through descriptions; the
catalog's real protection is that the model can only choose among entries the
author or application allowed.

## Remote skills as model tools

The first usable step ([#167](https://github.com/malcmiller/agentframe/issues/167)):
`ctx.generate(goal, tools=[*skills])` offers catalog entries next to ordinary
`@tool` functions. Each entry becomes one model tool with a provider-safe name,
its bounded description and its argument schema (`{"text": string}` for `ask`).

- A tool call runs `Remote.call` or `Remote.ask` on the binding, so deadline,
  depth, identity, correlation and token reconciliation are unchanged. Calls
  stay sequential as in M5, each counts toward `max_tool_calls`, and none is
  retried automatically: remote side effects are unknown.
- Each call holds `max_tokens_per_call=` tokens when the author set it on the
  catalog, otherwise everything the run has left, which is safe because calls
  are sequential.
- Results return to the model as untrusted tool output. A `RemoteAgentError` or
  `ClarificationNeededError` becomes a safe tool error naming only the class, so
  the model can try something else; limits, authorization and policy fail the run.

This is ReAct over remote agents: adaptive, but every result is read by the
model before it chooses the next call, so injected content in one agent's
answer can steer later calls. Use plans when the job can be laid out up front.

## Plans

### Plan shape

A plan is a frozen Pydantic value
([#169](https://github.com/malcmiller/agentframe/issues/169)):

| Field | Meaning |
| --- | --- |
| `Plan.steps` | Steps in any order; execution order comes from dependencies. |
| `Plan.result` | The step whose value is the plan's value. |
| `PlanStep.id` | `^[a-z][a-z0-9_]{0,31}$`, unique. |
| `PlanStep.use` | A catalog entry name. |
| `PlanStep.arguments` | JSON arguments for a structured entry, possibly with references. |
| `PlanStep.text` | The message for an `ask` entry, possibly with placeholders. |
| `PlanStep.after` | Extra ordering without data, for example "notify after saving". |

A plan is a directed acyclic graph: no loops, conditionals or nested plans.
That is the smallest shape a model emits reliably and a person can review at a
glance. `Plan` is public so tests and reviewed plans can construct one, but
there is no builder DSL.

### Data between steps

- In `arguments`, a value `{"$ref": "brief"}` is replaced by that step's whole
  result and `{"$ref": "brief.summary"}` by one top-level field of it. Types are
  preserved.
- In `ask` text, `{brief}` or `{brief.summary}` is replaced by the value as
  text (JSON for non-strings); `{{` and `}}` are literal braces.
- No expressions, indexing, filters or functions. References copy values; no
  model reads them between steps.

### Validation before execution

`SkillCatalog.check(plan, max_steps=8)` runs without I/O, and `ctx.run_plan`
runs it again:

- step IDs unique and well formed; every `use` in the catalog; `arguments` only
  on structured entries and `text` only on `ask` entries;
- every reference and `after` names an existing step; no cycles; `result` exists;
- at most `max_steps` steps (default 8, never above the run's remaining
  `max_tool_calls`) and at most 64 KiB serialized;
- literal arguments checked against the card schema's top level only: required
  properties present (or referenced) and no unknown properties when the schema
  forbids them. The receiving agent validates fully, as it already does.

Failures raise `InputValidationError` naming the step and field, never values.

### Planning call

`await ctx.plan(goal, skills, max_steps=8, instructions=None)`
([#170](https://github.com/malcmiller/agentframe/issues/170)) makes one
structured-output call with the agent's model, charged to the run's model calls
and tokens, with `model_request_checks` applied:

- Library-owned planning instructions (plus optional trusted author
  `instructions=`) are system messages. The goal and the catalog go in the user
  message as quoted data.
- The model-facing schema lists entry names as an enum and carries each step's
  arguments as a JSON string, so it converts for Foundry strict mode and Ollama
  `format`, the same technique as M6 routing. The reply is parsed, then checked.
- The model may decline with a reason; `ctx.plan` then raises
  `InputValidationError("No plan for this goal with the available skills")`
  without the model's text. An invalid plan raises `OutputValidationError` and
  is never repaired or re-requested, like `output=`.

## Running a plan

`await ctx.run_plan(plan, skills, max_parallel=4, max_tokens_per_step=None,
on_failure="stop")` ([#171](https://github.com/malcmiller/agentframe/issues/171))
checks the plan, then runs it under one `asyncio.TaskGroup`: a step starts when
its dependencies have completed, with at most `max_parallel` at once. References
are resolved just before a step starts and the top-level argument check runs
again. It returns a `PlanResult`:

| Field | Meaning |
| --- | --- |
| `value` | The `result` step's value, or `None` when it did not complete. |
| `complete` | Whether every step completed. |
| `steps` | Per step: `status` (`completed`, `failed`, `skipped`, `cancelled`, `input_required`), `value`, `error` (class and safe message), `usage` (`Usage \| None`), `remote_task_id`, `duration`. |
| `usage` | The plan's total; unknown if any step's usage is unknown. |

### Budgets, deadlines and depth

- The whole plan runs within the run's deadline; each step sends the remaining
  time, as every remote call does (#89).
- Each step is one call at the next hop; `max_remote_hops` is unchanged.
- Each step counts as one tool call toward `max_tool_calls`, an existing host
  ceiling for model-chosen actions.
- Token holds: `max_tokens_per_step` when given, otherwise the run's remaining
  tokens at start divided by the number of steps. Parallel steps need separate
  holds (#90), and budgets come from trusted code, never from the model. The
  peer's reported usage replaces each hold; a peer that does not report makes
  usage unknown, never zero.

### Failures and partial results

- `on_failure="stop"` (default): the first failure cancels running steps, which
  sends best-effort `CancelTask` through the existing binding, and raises the
  original error, never an `ExceptionGroup`.
- `on_failure="continue"` ([#172](https://github.com/malcmiller/agentframe/issues/172)):
  dependents of a failed step are `skipped` and never sent, independent steps
  still run, and the partial `PlanResult` is returned. A remote question marks
  its step `input_required`; nothing answers it automatically.
- Limits, authorization, policy and cancellation always stop the plan,
  whatever `on_failure` says.
- No step is retried automatically. Cards do not say whether a skill has side
  effects, so every remote step is treated as one.

### Replanning

Not built in. Automatic replanning, as in LLMCompiler's joiner or Magentic-One's
outer loop, feeds step results back into the planner and so lets untrusted
content change which actions run next. An author who wants it calls `ctx.plan`
again with an explicit, reviewed summary of what happened; the extra call is
bounded by `max_model_calls` like any other.

## Observability

([#173](https://github.com/malcmiller/agentframe/issues/173))

- `Plan created` (step count and entry names), `Plan step started` and
  `Plan step completed` INFO records with `extra`: `agent`, `invocation_id`,
  `root_invocation_id`, `plan_step`, `use`, `status`, `remote_task_id`,
  `duration_ms`, `total_tokens` (or `None`). No arguments, text or results.
- Peers see the orchestrator invocation as their parent and the same root
  ([tracing](tracing.md)); the remote task ID ties a step to the peer's logs, so
  no new wire field is needed.
- `RunResult.usage` already includes every step; `PlanResult` breaks it down.

## Safety

### Authorization and approval

- **Per-entry permissions** ([#168](https://github.com/malcmiller/agentframe/issues/168)):
  `remote_skills(..., permissions={"billing-agent.refund": {"refunds:write"}})`.
  Entries the caller lacks are hidden from the model and refused if named.
- **Per-call checks:** existing `ExecutionPolicy.tool_checks` run before every
  remote call with `tool_name` set to the entry name, for tools and plan steps.
- **Plan checks** ([#174](https://github.com/malcmiller/agentframe/issues/174)):
  new `ExecutionPolicy.plan_checks`, async `(skill, principal, plan) -> bool`,
  run before the first step with the usual fail-closed rules. They see every
  step and reference, so they can refuse plans that move internal results to an
  external agent.
- **Human approval stays application-owned.** The documented pattern is two
  skills: one returns `ctx.plan(...)`, another accepts a `Plan` argument and
  calls `ctx.run_plan`. A caller-supplied plan is untrusted input and is checked
  against the catalog and the caller's permissions again. There is no built-in
  pause/resume.
- **Remote agents still decide.** Each agent authenticates and authorizes the
  request itself. With `OnBehalfOf` it sees the user; with `Service` it sees the
  orchestrator, which is why consequential entries need `permissions=`.

### Prompt injection and data flow

- The model chooses only among catalog entries; plan validation enforces that.
- Card text and the goal are data in the user message, never instructions.
- In plans, no model reads results between steps, so injected content in one
  agent's answer can change the data a later step receives but not which steps
  run (plan-then-execute). Remote skills as tools do not have this property.
- Data moving between agents is explicit in the plan and visible to plan checks.
  There is no taint or label system yet; CaMeL-style capabilities are a larger
  design to revisit with real usage.
- Fan-out and cost are bounded by `max_steps`, `max_parallel`,
  `max_tool_calls`, per-step token holds, the run's token budget and deadline,
  and each peer's own admission limits.

## Evaluation (M9)

[M9](evaluation.md) evaluates whole skills, so an orchestrator is already an
evaluation target. Plan quality (did the model choose the right agents and
skills) is a narrower question: M9 excludes trajectory scoring because there is
no trajectory contract. `PlanResult` and the returned `Plan` provide one for
orchestrators: a scorer can compare the entries used against a case's expected
entries. The acceptance story
([#176](https://github.com/malcmiller/agentframe/issues/176)) runs a small planner
evaluation with M9's harness, so only that story depends on M9 (#153, #156).

## Packaging

No new dependency: plans use Pydantic, the existing A2A client and
`asyncio.TaskGroup`. The code lives in `src/agentframe/orchestration/` in the
main wheel, with public names exported from `agentframe` because they appear on
`RunContext`. No extra.

## Open questions

Each has a recommendation and a one-line reason; none is decided yet.

1. **Revisit #17's cuts.** Recommendation: lift "orchestration helpers" and
   "dynamic invocation" as opt-in M10 features and keep every other cut. Real
   usage needs model-chosen calls, and plain `await` stays the default.
2. **Eligible agents.** Recommendation: declared `Remote` parameters first;
   discovered agents later behind an application allowlist. Declared remotes
   are startup-validated and already have identity.
3. **Identity for discovered agents.** Recommendation: per-name `remotes=`
   entries only, no default credential; secured agents without one are
   excluded. Prevents handing credentials to any newly listed agent.
4. **Simpler first step.** Recommendation: ship remote skills as model tools
   before plans. Reuses the M5 loop and is useful alone.
5. **Plan-then-execute, tool loop, or both.** Recommendation: both; the tool
   loop for adaptive work, plans for parallelism, cost and injection
   resistance. Plans fix control flow before untrusted results arrive.
6. **Graph representation.** Recommendation: flat list of steps, dependencies
   from references plus `after`, DAG only, public `Plan` but no builder DSL.
   Smallest shape models emit reliably and people can review.
7. **Plan limits.** Recommendation: 8 steps by default (never above remaining
   `max_tool_calls`), 64 KiB, `max_parallel=4`, each step one tool call.
   Reuses existing ceilings instead of new `RunLimits` fields.
8. **Data passing.** Recommendation: `{"$ref": "step.field"}` in arguments,
   `{step.field}` in `ask` text, copied values, no expressions. No model sees
   data between steps.
9. **Argument checking.** Recommendation: local top-level required/unknown
   checks and full validation by the receiver; no JSON Schema validator
   dependency. The receiver already validates and rejects invalid arguments.
10. **Per-step token budget.** Recommendation: author-set `max_tokens_per_step`,
    default an equal share of the remaining budget. Parallel steps need separate
    holds, and models must not choose budgets.
11. **Failure policy.** Recommendation: `stop` by default, opt-in `continue`
    with partial results; limits, authorization, policy and cancellation always
    stop; no retries. Fail loudly by default, as everywhere in AgentFrame.
12. **Replanning.** Recommendation: none built in; authors may call `ctx.plan`
    again explicitly. Automatic replanning feeds untrusted results back into
    control flow.
13. **Approval hook.** Recommendation: `ExecutionPolicy.plan_checks` plus a
    documented propose/execute two-skill pattern, no pause/resume. Approval
    workflows stay application-owned.
14. **Authorization of model-chosen calls.** Recommendation: per-entry
    `permissions=` plus existing `tool_checks` with the entry name, hiding
    entries the caller lacks. Same fail-closed hooks as M5 and M7.
15. **Untrusted card text.** Recommendation: 500-character descriptions,
    10 tags, no examples, at most 20 entries and 8 KiB per schema, card text only
    in the user message. Cards are input from other parties.
16. **Planning errors.** Recommendation: reuse `InputValidationError`
    (unplannable goal, invalid caller plan) and `OutputValidationError` (invalid
    model plan), no repair. Fewer error classes to learn.
17. **Package placement.** Recommendation: main wheel, public names from
    `agentframe`, code in `src/agentframe/orchestration/`, no extra. No new
    dependency is needed.
18. **Milestone placement.** Recommendation: M10 after the M8+ release, built in
    parallel with M9; only the acceptance story waits for M9's harness. Plans
    should ship measured, but building them need not wait for evaluation.
