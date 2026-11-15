# Model-planned orchestration

**Status: M10 complete, in 0.3.0.** Every story of the
[M10 overview](https://github.com/malcmiller/agentframe/issues/177)
(#166-#176) is implemented, and version 0.3.0 is the release that adds it. Reference:
[orchestration API](../api/orchestration.md); rationale:
[orchestration design](../design/orchestration.md).

An orchestrator is an ordinary agent whose skills call other agents over A2A.
Since M8+ the author writes those calls in Python. M10 adds two opt-in ways to
let the orchestrator's **model** choose instead: calling registered agents'
skills as **tools**, one at a time, or writing a whole **plan** that AgentFrame
checks and runs, in parallel where it can. Every remote call still carries the
run's deadline, hop depth, token budget, identity and correlation.

## Choose a style

| Style | Use it when | How |
| --- | --- | --- |
| Plain `await` and `asyncio.TaskGroup` (default) | You know which agents and skills to call. Cheapest, deterministic, easiest to test. | Declare `Remote[Literal["name"]]` parameters and call `call` / `ask` ([multi-agent composition](agent-examples.md#multi-agent-composition)). |
| Remote skills as model tools | The next call depends on the previous answer: adaptive, step-by-step work. | `skills = await ctx.remote_skills(...)`, then `await ctx.generate(goal, tools=[*skills])`. Each call is one model turn. |
| Plans | The steps are known up front but chosen by the model: parallel work, one model call, cost you can check before anything runs. | `plan = await ctx.plan(goal, skills)`, then `await ctx.run_plan(plan, skills)`. |

Start with plain Python. Reach for a model only when the choice of agent or
skill genuinely depends on the request.

## The example

[`examples/multi_agent`](../../examples/multi_agent) shows hand-written and
model-planned orchestration side by side. It has three agents:

- `research.py`: `find_sources` (model-free) and `brief` (Ollama);
- `writer.py`: `write_post(title, sources, notes)`, model-free;
- `planner.py`: the orchestrator, with three skills:
  - `plan`: hand-written fan-out with `asyncio.TaskGroup`;
  - `propose`: the model plans a goal and returns the plan; nothing is sent;
  - `planned`: the model plans a goal, then the plan runs.

```python
SPECIALISTS = ["research-agent", "writer-agent"]
MAX_STEPS = 4


@agent(name="Planner", version="0.1.0")
class Planner(Agent):
    # Open-proxy warning: the model calls other agents as this agent.
    @skill(id="planned", tags=["planning"], requires_permissions=["plans:run"])
    async def planned(self, goal: str, ctx: RunContext) -> PlanOutcome:
        """Plan a goal over the research and writer agents, then run the plan."""
        skills = await specialists(ctx)
        plan = await ctx.plan(goal, skills, max_steps=MAX_STEPS)
        result = await ctx.run_plan(
            plan,
            skills,
            max_steps=MAX_STEPS,
            max_tokens_per_step=BRIEF_TOKENS,
            on_failure="continue",
        )
        return PlanOutcome(
            value=result.value,
            complete=result.complete,
            steps={step_id: step.status for step_id, step in result.steps.items()},
        )


async def specialists(ctx: RunContext) -> SkillCatalog:
    """Return the skills of the two named agents, never every registered agent."""
    return await ctx.remote_skills(
        agents=SPECIALISTS,
        permissions={"writer-agent.write_post": ["posts:write"]},
    )
```

For the goal "Write a post titled Tides from sources about tides and a short
brief on them", a good plan finds sources and writes the brief at the same
time, then passes both to the writer with references:

```python
Plan(
    steps=[
        PlanStep(id="tides", use="research-agent.find_sources", arguments={"topic": "tides"}),
        PlanStep(id="brief", use="research-agent.brief", arguments={"question": "Tides?"}),
        PlanStep(
            id="post",
            use="writer-agent.write_post",
            arguments={
                "title": "Tides",
                "sources": {"$ref": "tides"},
                "notes": {"$ref": "brief"},
            },
        ),
    ],
    result="post",
)
```

The example's app adds a plan check and a tool check:

```python
async def at_most_one_brief(
    skill: SkillDefinition, principal: Principal | None, plan: Plan
) -> bool:
    """Allow at most one brief per plan: each one uses the research agent's model."""
    return sum(step.use == "research-agent.brief" for step in plan.steps) <= 1


async def public_sources_only(
    skill: SkillDefinition,
    principal: Principal | None,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    """Posts are public, so send the writer only sources from the public library."""
    if tool_name != "writer-agent.write_post":
        return True
    sources = arguments.get("sources")
    return isinstance(sources, list) and all(
        isinstance(source, dict)
        and str(source.get("url", "")).startswith("https://example.org/")
        for source in sources
    )


policy = ExecutionPolicy(
    plan_checks=[at_most_one_brief], tool_checks=[public_sources_only]
)
```

Run all three with `AGENTFRAME_DEV_CLIENT_KEY=dev-only-key agentframe dev`
from `examples/multi_agent` (Ollama plans and writes briefs), then call
`planned` on `http://127.0.0.1:8002` with `X-API-Key: dev-only-key`. The
planner's development placeholder `DeveloperRoles` gives the `developer`
caller the `planner` role, which grants `plans:run` and `posts:write`; a
production host gets roles from its identity provider instead.
[`examples/plan_approval.py`](../../examples/plan_approval.py) shows the
other shape: `propose` and a separate, permission-protected `execute` with a
person reviewing the plan in between
([propose, review, execute](../api/orchestration.md#propose-review-execute)).

## The catalog

`ctx.remote_skills(...)` builds the options a model may choose from the Agent
Cards of the agents registered in the directory's sources. **The registry is
the allowlist**: there is no second list. An agent is included only when its
card can be fetched and validated and its security can be satisfied (its
`remotes=` entry, else [`default_remote`](../api/orchestration.md#default_remote),
else an open card). Any other agent is skipped with one warning naming it and
the reason (`card unavailable`, `security not satisfied`, ...), and the rest
of the catalog is still used.

Narrow it with `agents=`, `skill=` and `tags=` (or pass declared `Remote`
parameters): at most 20 entries are allowed after narrowing, and more fails
rather than truncating. AgentFrame agents give one entry per skill
(`"research-agent.find_sources"`); other A2A agents give one free-text `ask`
entry (`"partner-agent"`). Card text is another party's content: bounded,
offered to the model only as data or tool definitions, never as trusted
instructions.

## Remote skills as tools

```python
skills = await ctx.remote_skills(agents=SPECIALISTS, max_tokens_per_call=2_000)
answer = await ctx.generate(goal, tools=[*skills])
```

The model calls entries through the existing tool loop. Each call counts
toward `max_tool_calls`, holds `max_tokens_per_call` of the run's tokens, is
never retried, and passes the same permission and tool checks as a plan step.
A remote failure goes back to the model as a short error it can work around.

## Plans

`ctx.plan(goal, skills)` makes **one** structured model call and returns a
checked [`Plan`](../api/orchestration.md#plan): a flat list of steps, each one
call, with dependencies from `{"$ref": "step"}` / `{"$ref": "step.field"}`
argument values, `{step}` placeholders in `ask` text, and `after`. It never
repairs or re-requests an invalid plan; call it again to retry.

`ctx.run_plan(plan, skills)` checks the plan again (a plan from a caller is
untrusted), runs the plan checks, then runs ready steps at once, up to
`max_parallel=4`, substituting earlier results just before each step is sent.
Every step counts as a tool call and holds `max_tokens_per_step` (or an equal
share of what the run has left). By default the first failing step stops the
plan; `on_failure="continue"` keeps a remote agent's failure or question as
that step's result, skips the steps that depend on it and returns a partial
[`PlanResult`](../api/orchestration.md#planresult) with `complete=False`.
Limits, authorization, policy and cancellation always stop the plan.

References are copied values, never expressions. A placeholder such as
`"{brief}"` inside a structured argument is just text: only `ask` text
substitutes placeholders. Real models sometimes write one anyway; see
[measure the planner](#measure-the-planner).

## Safety checklist

A model choosing calls, from a goal a caller wrote, can be steered by prompt
injection in that goal or in another agent's results. Treat every plan, and
every model-chosen call, as untrusted:

1. **Narrow the catalog.** Name the agents (`agents=`) or tags a skill really
   needs; never offer every registered agent by default.
2. **Protect the orchestrating skill** with `requires_permissions` (the
   example's `plans:run`), granted only to callers trusted to make the
   orchestrator act.
3. **Protect consequential entries** with `remote_skills(permissions=...)`.
   Callers without them never see the entry, and every call to it is checked
   again. An agent's `ask` entry inherits its skills' permissions.
4. **Check whole plans** with `ExecutionPolicy(plan_checks=[...])`: which
   agents may be combined, how many costly steps, whether data from one agent
   may reach another. Nothing is sent unless every check returns `True`.
5. **Check every step** with `ExecutionPolicy(tool_checks=[...])`: it gets
   the entry name and the exact arguments about to be sent, after
   substitution. This is the gate that stops data leaving.
6. **Prefer `OnBehalfOf`** with Entra callers for consequential agents, so
   every agent authorizes the real user and the orchestrator can never exceed
   that user's authority.

**Open-proxy warning.** With `Service` identity the orchestrator calls other
agents as itself. A skill that runs caller-supplied plans, or lets its model
plan from caller-written goals, lets anyone who can call it make the
orchestrator call any catalog entry with the orchestrator's credentials (a
confused deputy). Apply the checklist above, all of it, before exposing such
a skill.

**`default_remote` warning.** A `default_remote` goes to every registered
agent whose card asks for its scheme. With an Entra default, each such agent
receives a token for the default's audience that it could replay against any
other API accepting that audience; with an API-key default, each receives the
same shared key and could call any agent that accepts it. Configure one only
when every registered agent is trusted with that audience or key, keep the
registry narrow, and give sensitive agents their own `remotes=` entries.

AgentFrame has no built-in human approval or pause/resume. Approval is two
ordinary skills, propose and execute, owned by the application.

## Logs and usage

Plans log `Plan created`, `Plan step started` and `Plan step completed` at
INFO, with the run's `invocation_id` and `root_invocation_id`, each step's
entry, status, duration, remote task ID and tokens, and never arguments,
results, goals or questions
([plan logs and usage](../api/orchestration.md#plan-logs-and-usage)). Every
agent a plan calls logs the same `root_invocation_id`. `StepResult.usage` and
`PlanResult.usage` break down what the run already charged once; usage a peer
does not report is unknown (`None`), never zero.

## Measure the planner

Plan quality is a property of the model, so measure it with an
[evaluation](evaluation.md).
[`examples/multi_agent/planner_evaluation.py`](../../examples/multi_agent/planner_evaluation.py)
evaluates the planner's `propose` skill on six goals. Each case's `expected`
value is a reference plan, and two plain scorers compare only its
**structure**: `same_entries` (the same entries, as often) and
`same_dependencies` (the same entries waiting for the same entries). Step IDs,
argument values and wording are never compared. An invalid plan errors the
case. The report keeps no goals, plans or arguments unless
`capture_content=True`, and plan logs hold only entry names.

```python
from agentframe import Runtime
from examples.multi_agent.planner_evaluation import create_planner_evaluation

runtime = Runtime(directory=directory, remotes=remotes)  # reaches both specialists
report = await create_planner_evaluation(model, runtime).run()
report.require(min_pass_rate=0.5)
```

Measured on 2026-11-14 (planning is nondeterministic; your results will differ):

| Planner | Structurally correct | Valid plans | Notes |
| --- | --- | --- | --- |
| Foundry `gpt-5.4` | 10 of 12 cases (two runs: 4/6, 6/6) | 12 of 12 | Once wrote `"notes": "{brief}"`, a placeholder in a structured argument, which is not substituted. |
| Ollama `llama3.2:3b` | 1 of 18 cases (three runs) | 13 of 18 | Adds unrequested steps, uses `after` instead of `$ref`, invents sources (refused by the example's tool check), malformed step IDs. |

A small local model is fine for development and for the tool loop on narrow
tasks, but use a stronger model for planning, and gate on an evaluation
before trusting a new model or prompt.

## Release acceptance

- `tests/integration/test_orchestration_release.py` runs the research agent,
  the writer, the plain A2A peer and two orchestrators as separate processes,
  with a scripted planning model: a parallel-plus-dependent plan with
  references, usage and per-step logs, the same root invocation ID in every
  agent, the caller's deadline and token budget, a plan check and a tool check
  refusing before sending, a caller without `plans:run`, the hop limit, a
  registry-wide catalog skipping an unreachable and an unsatisfiable agent,
  remote skills (including the peer) as model tools, `on_failure="continue"`,
  and the planner evaluation in CI.
- `examples/smoke.py` runs `plan` and `planned` against the installed wheel.
- Opt-in, with a real model ([real-provider tests](../../README.md#real-provider-tests)):
  `test_a_real_planner_plans_and_runs_the_example_post` and
  `test_a_real_planner_passes_its_evaluation` in `test_providers.py`, run with
  the `ollama` or `foundry` marker.
