# Orchestration: remote skill catalogs, remote skills as tools and plans

Model-planned orchestration ([M10](../design/orchestration.md)) lets an
orchestrator's model choose which registered agent and skill to call. This
page covers its first pieces, all implemented: the **remote skill
catalog** (M10 [#166](https://github.com/malcmiller/agentframe/issues/166)),
offering its entries to a model as **tools**
([#167](https://github.com/malcmiller/agentframe/issues/167)) and
[permissions and tool checks](#permissions-and-tool-checks) for model-chosen
calls ([#168](https://github.com/malcmiller/agentframe/issues/168)),
typed [plans](#plan) checked by [`SkillCatalog.check`](#skillcatalogcheck)
([#169](https://github.com/malcmiller/agentframe/issues/169)), model-made
plans from [`RunContext.plan`](#runcontextplan)
([#170](https://github.com/malcmiller/agentframe/issues/170)) and the
optional [`default_remote`](#default_remote) identity for registered agents
([#175](https://github.com/malcmiller/agentframe/issues/175)), the
[`ctx.plan`](#runcontextplan) planning call
([#170](https://github.com/malcmiller/agentframe/issues/170)), and
[running a plan](#runcontextrun_plan) with [`PlanResult`](#planresult)
([#171](https://github.com/malcmiller/agentframe/issues/171)). The `continue`
failure policy (#172), per-step logs (#173) and plan checks (#174) are
planned, not implemented.

Plain `await` on declared [`Remote`](discovery.md#remoteliteralname)
parameters stays the default way to call other agents; the catalog is opt-in.

## `RunContext.remote_skills`

**Purpose / signature:** list the remote skills this run may offer a model,
from the Agent Cards of every agent the directory's sources list, or of the
skill's own `Remote` parameters. Implemented (#166).

```python
async def remote_skills(
    self,
    *remotes: Remote[Any],
    agents: Collection[str] | None = None,
    skill: str | None = None,
    tags: Collection[str] = (),
    ask: bool = False,
    max_tokens_per_call: int | None = None,
    permissions: Mapping[str, Collection[str]] | None = None,
) -> SkillCatalog: ...
```

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `*remotes` | `Remote[Any]` | Optional | The running skill's `Remote` parameters. When given, the catalog holds exactly these agents. When omitted, every registered agent is considered. |
| `agents` | `Collection[str] \| None` | Optional; default `None` | Agent names to keep. A name no source lists (or, with `remotes`, that none of them has) raises `DefinitionError`, unless a source failed to list. A single string raises `DefinitionError`. |
| `skill` | `str \| None` | Optional; default `None` | Keep only card skills with this ID. |
| `tags` | `Collection[str]` | Optional; default `()` | Keep only card skills carrying all of these tags, matched with `skill` on the same card skill, as [`AgentDirectory.find`](discovery.md#agentdirectoryfind). A single string raises `DefinitionError`. |
| `ask` | `bool` | Optional; default `False` | Also add a free-text `ask` entry for agents with the skill-call extension. |
| `max_tokens_per_call` | `int \| None` | Optional; default `None` | The most tokens each [model-chosen call](#remote-skills-as-model-tools) to an entry may use, held against the run's `max_total_tokens` and sent to peers that advertise it, as `Remote.call(max_total_tokens=...)`. `None` holds everything the run has left, which is safe because those calls run one at a time. Anything but a whole number above 0 raises `DefinitionError`. Implemented (#167). |
| `permissions` | `Mapping[str, Collection[str]] \| None` | Optional; default `None` | Permissions a caller must hold to use an entry, by entry name: `{"billing-agent.refund": {"refunds:write"}}`. The caller needs all of them, as for `@tool(requires_permissions=...)`; they become the entry's `requires_permissions`, and an agent's `ask` entry also requires every permission given for that agent's skills. Keys are entry names; see [unmatched keys](#unmatched-permissions-keys). Anything but a mapping of strings to collections of strings (a single string included) raises `DefinitionError`. See [permissions and tool checks](#permissions-and-tool-checks). Implemented (#168). |
| Result | [`SkillCatalog`](#skillcatalog) | On success | The entries, in source and card order, plus this run's binding to each agent. May be empty. |

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Orchestrator", description="Delegates to specialists.", version="0.1.0")
class Orchestrator(Agent):
    @skill(id="options", description="List research options.", tags=["orchestration"])
    async def options(self, ctx: RunContext) -> list[str]:
        catalog = await ctx.remote_skills(tags=["research"])
        return [entry.name for entry in catalog]
```

The runtime needs a directory: pass `directory=` to `Runtime` or `AgentApp`,
as for `Remote` parameters.

**The registry is the allowlist.** With no `remotes`, every agent listed by the
directory's sources is considered; there is no second allowlist. An agent is
included only when:

1. its card can be fetched and is valid under the directory's usual checks
   (https or an allowed HTTP host, same-origin interface URL, no redirects,
   size and time bounds, cache and stale rules; at most eight fetches at a
   time), and
2. its card's security is open, or met by its `remotes=` entry (the same check
   `Remote` parameters get at startup), or, for an agent without an entry, by
   [`default_remote`](#default_remote).

Any other agent is **skipped, not fatal**, with one WARNING log naming only the
agent and the reason: `card unavailable`, `card invalid`,
`security not satisfied` or `schema too large`. A source that cannot list its
agents is skipped with a warning naming only its position and class, for
example `source 2 (FoundryAgentRegistry)`; the other sources' agents are still
used. Warnings never include card content or URLs; they carry the run's
correlation fields as `extra`, like other run logs.

**Entries.** An agent whose card declares AgentFrame's skill-call extension
gives one entry per (matching) card skill, with the argument schema from the
extension's `params.skills`. Any other agent, such as a non-AgentFrame A2A
agent, gives one `ask` entry describing the agent, kept when any of its card
skills matches the narrowing; its card skills are descriptive only, because
base A2A has no standard way to select a skill. Entry names become model tool
names and plan references, so an extension card whose skill IDs are not 1-64
letters, digits, `_` or `-`, or that lists an ID twice, counts as
`card invalid`, and entry names must be unique within a catalog.

**Untrusted card text.** Descriptions (including `description` and `title`
strings inside schemas) lose their control and format characters (whitespace
ones become spaces) and are cut to 500 characters; at most ten tags are kept;
card and schema `examples` are not copied. An agent with an argument schema
over 8 KiB (compact JSON, after bounding) is skipped. Bounding text reduces,
but cannot remove, prompt injection through descriptions; pass entries to a
model only as data, never as trusted instructions.

**Size.** More than 20 entries after narrowing and skipping raise
`DefinitionError` with the count and the narrowing options. A catalog is never
truncated.

**Self.** When the run is hosted by an [`AgentApp`](hosting.md#agentapp), an
agent whose validated interface URL equals the app's advertised A2A URL is the
orchestrator itself and is left out without a warning. A local `Runtime` has
no advertised URL, so it leaves nothing out; a registry listing the
orchestrator under another URL is still bounded by `max_remote_hops`.

**Behavior / failures:** building a catalog reads the sources and cards only;
it never calls a model or sends an A2A message.

- `DefinitionError`: no directory, the directory is not open, a name listed by
  more than one source, an unknown name in `agents`, `agents` or `tags` given
  as a single string, a positional argument that is not a `Remote`, credentials
  with a redirect-following HTTP client, two entries with the same name (for
  example agent `a.b`'s `ask` entry and agent `a`'s skill `b`), more than
  20 entries (counted before any caller's permissions hide entries), or an
  invalid `max_tokens_per_call` or `permissions`.
- `RemoteAgentError`: a declared `Remote`'s card cannot be fetched (raised
  while binding the parameter, as in M8+), has an unsafe or repeated skill ID,
  or its argument schema is missing or over 8 KiB.
- `PermissionDeniedError`: an included agent's `remotes=` entry, or the
  `default_remote` it uses, is `OnBehalfOf`, and the run has no signed-in
  user. There is no fallback to another identity or to skipping the agent.

**Related:** [remote skill catalog design](../design/orchestration.md#remote-skill-catalog),
[catalog size](../design/orchestration.md#catalog-size),
[`AgentDirectory`](discovery.md#agentdirectory).

## `default_remote`

**Purpose / signature:** an optional outbound identity for registered agents
that have no `remotes=` entry, so secured specialists join the
[catalog](#runcontextremote_skills) by being registered, without one entry per
agent. Implemented (#175).

```text
Runtime(..., remotes=None, default_remote: Service | OnBehalfOf | None = None)
AgentApp(..., remotes=None, default_remote: Service | OnBehalfOf | None = None)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `default_remote` | [`Service`](authorization.md#service)` \| `[`OnBehalfOf`](authorization.md#onbehalfof)` \| None` | Optional; default `None` | How to authenticate to catalog agents without a `remotes=` entry. On `AgentApp`, only without `runtime=`. Anything else raises `DefinitionError`. |

```python
from azure.identity.aio import ManagedIdentityCredential

from agentframe import (
    AgentApp,
    AgentDirectory,
    APIKeyCredential,
    EntraAppCredential,
    FileAgentRegistry,
    HostConfig,
    Service,
)

# Application-supplied placeholders: the orchestrator agent, its host and keys.
directory = AgentDirectory(sources=[FileAgentRegistry("agents.yaml")])
app = AgentApp(
    agent=orchestrator,
    host=host,
    directory=directory,
    remotes={"partner-agent": Service(credential=APIKeyCredential(partner_key))},
    default_remote=Service(
        credential=EntraAppCredential(
            ManagedIdentityCredential(), scope="api://agents/.default"
        )
    ),
)
```

**Identity order** for each registered agent the catalog considers:

1. its `remotes=` entry, whenever it has one; the default is never sent to it;
2. otherwise `default_remote`, only when the agent's card requires
   credentials and the default meets its security (the same check `remotes=`
   entries get);
3. otherwise no credentials, which works only for open cards. Open agents
   never receive the default.

An agent none of these satisfies is skipped with the usual
`security not satisfied` warning, and the default is never sent to it. Without
`default_remote`, only open agents and agents with `remotes=` entries are
included.

**Scope.** The default applies only to catalog agents (`ctx.remote_skills()`
without `Remote` arguments). Declared [`Remote`](discovery.md#remoteliteralname)
parameters, including ones passed to `remote_skills`, keep the M8+ rules: only
their `remotes=` entry counts, checked at startup.

**Behavior / failures:**

- Credentials go per request only to the card's validated interface origin,
  never across redirects; a redirect-following directory client raises
  `DefinitionError` when the default would be used (#84, #92).
- An `OnBehalfOf` default needs `AgentApp` with
  `auth=EntraIDAuth(client_id=..., client_credential=...)`; any other
  authenticator raises `DefinitionError` at construction, as for `OnBehalfOf`
  entries (#93). A run without a signed-in user (app-only token, API-key or
  anonymous caller, or a local run) raises `PermissionDeniedError` when an
  agent using the default is included, and a failed exchange fails the call
  with `AuthenticationError`; neither falls back.

**Security:** the default goes to **every registered agent whose card asks for
its scheme**. With an Entra default, each such agent receives a token for the
default's audience that it could replay against any other API accepting that
audience; with an `APIKeyCredential` default, each receives the same key and
could use it against any agent accepting it (a confused-deputy risk).
Configure a default only when every registered agent is trusted with that
audience or key, keep the registry narrow, and give sensitive or less-trusted
agents their own `remotes=` entries. See
[cross-agent identity](../guides/authorization.md#cross-agent-identity-propagation-m8).

**Related:** [identity design](../design/orchestration.md#identity),
[`Runtime`](runtime.md#runtime), [`AgentApp`](hosting.md#agentapp).

## `RemoteSkill`

**Purpose / signature:** one option an orchestrator's model may choose: a
remote structured skill or free-text `ask`. A frozen dataclass. Implemented
(#166).

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | `str` | `"agent.skill"` for a structured skill (`"research-agent.find_sources"`), or the agent's name for an `ask` entry. |
| `agent` | `str` | The agent's name in the directory. |
| `skill` | `str \| None` | The card skill ID; `None` for `ask`. |
| `description` | `str` | The card's description of the skill (of the agent, for `ask`), bounded. |
| `tags` | `tuple[str, ...]` | At most ten of the card's tags, bounded. For `ask`, the tags of the agent's card skills. |
| `arguments_schema` | `dict[str, Any] \| None` | The skill's argument JSON Schema from the card's skill-call extension, bounded; `None` for `ask`. Treat it as read-only. |
| `requires_permissions` | `frozenset[str]` | Permissions a caller needs to use the entry, from `remote_skills(permissions=...)`; empty by default. |

```python
from agentframe import RemoteSkill, SkillCatalog


def structured(catalog: SkillCatalog) -> list[RemoteSkill]:
    return [entry for entry in catalog if entry.skill is not None]
```

Every text field is another party's content: bounded, but untrusted.

**Related:** [untrusted card text](../design/orchestration.md#untrusted-card-text).

## `SkillCatalog`

**Purpose / signature:** an immutable list of [`RemoteSkill`](#remoteskill)
entries plus the run's binding to each entry's agent. Returned by
[`RunContext.remote_skills`](#runcontextremote_skills); not constructed by
application code. Implemented (#166).

| Member | Type | Meaning |
| --- | --- | --- |
| `entries` | `tuple[RemoteSkill, ...]` | The entries, in source and card order. |
| `max_tokens_per_call` | `int \| None` | The tokens each model-chosen call to an entry may use; `None` means everything the run has left. |
| `get(name)` | `RemoteSkill \| None` | The entry named `name`, or `None`. |
| `check(plan, *, max_steps=8)` | `tuple[tuple[PlanStep, ...], ...]` | Checks a [`Plan`](#plan) without I/O; see [`SkillCatalog.check`](#skillcatalogcheck). |
| `iter(catalog)` | `Iterator[RemoteSkill]` | Iterates the entries. |
| `len(catalog)` | `int` | The number of entries, at most 20, including entries hidden from callers without their permissions. |

```python
from agentframe import RunContext


async def option_names(ctx: RunContext) -> list[str]:
    catalog = await ctx.remote_skills(agents=["research-agent", "writer-agent"])
    return [entry.name for entry in catalog]
```

A catalog belongs to the run that built it: each binding carries that run's
deadline, hop depth, token budget, identity and correlation, exactly like a
`Remote` parameter. Its entries are called by a model through
[`ctx.generate(tools=[...])`](#remote-skills-as-model-tools) (#167); plans
are [checked](#skillcatalogcheck) against it (#169) and
[run](#runcontextrun_plan) with it (#171).

**Related:** [M10 overview](https://github.com/malcmiller/agentframe/issues/177).

## `Plan`

**Purpose / signature:** an execution plan: a flat list of
[`PlanStep`](#planstep)s whose order comes from their dependencies, forming a
directed acyclic graph. A frozen Pydantic model, public so tests and reviewed
plans can construct one; there is no builder DSL, loops or conditions.
Implemented (#169); [`ctx.plan`](#runcontextplan) makes one (#170) and
[`ctx.run_plan`](#runcontextrun_plan) runs one (#171).

| Field | Type | Required / default | Meaning |
| --- | --- | --- | --- |
| `steps` | `list[PlanStep]` | Required | The steps, in any order. |
| `result` | `str` | Required | The ID of the step whose value is the plan's value. |

```python
from agentframe import Plan, PlanStep

plan = Plan(
    steps=[
        PlanStep(id="tides", use="research-agent.find_sources", arguments={"topic": "tides"}),
        PlanStep(id="waves", use="research-agent.find_sources", arguments={"topic": "waves"}),
        PlanStep(
            id="brief",
            use="research-agent.brief",
            arguments={"sources": [{"$ref": "tides"}, {"$ref": "waves"}]},
        ),
        PlanStep(id="post", use="writer-agent", text="Write a post from this brief: {brief}"),
    ],
    result="post",
)
```

Unknown fields and non-JSON argument values fail with Pydantic's
`ValidationError` when the plan is constructed. Everything else is checked by
[`SkillCatalog.check`](#skillcatalogcheck).

**Related:** [plan shape](../design/orchestration.md#plan-shape).

## `PlanStep`

**Purpose / signature:** one call in a [`Plan`](#plan): a catalog entry plus
its `arguments` (structured entries) or `text` (`ask` entries). A frozen
Pydantic model. Implemented (#169).

| Field | Type | Required / default | Meaning |
| --- | --- | --- | --- |
| `id` | `str` | Required | Unique in the plan, matching `^[a-z][a-z0-9_]{0,31}$`. |
| `use` | `str` | Required | A catalog [entry name](#remoteskill), for example `"research-agent.find_sources"`, or an agent's name for its `ask` entry. |
| `arguments` | `dict[str, JsonValue]` | `{}` | JSON arguments for a structured entry; only structured entries take them. |
| `text` | `str \| None` | `None` | The message for an `ask` entry; required for `ask` entries and refused for structured ones. |
| `after` | `list[str]` | `[]` | Step IDs that must complete first although no data flows from them. |

References copy values from earlier steps; there are no expressions,
indexing, filters or functions:

- In `arguments`, any value (at any depth) `{"$ref": "brief"}` stands for that
  step's whole result and `{"$ref": "brief.summary"}` for one top-level field
  of it. The object must have no other key. Types are preserved.
- In `text`, `{brief}` or `{brief.summary}` stands for the value as text;
  `{{` and `}}` are literal braces. Conversions (`{brief!r}`) and formats
  (`{brief:>9}`) are refused.
- An `ask` step's value is text, so `.field` of an `ask` step is refused.

A step depends on every step it references and every step in `after`.
Values are substituted just before the step runs
([`ctx.run_plan`](#runcontextrun_plan), #171).

**Related:** [data between steps](../design/orchestration.md#data-between-steps).

## `SkillCatalog.check`

**Purpose / signature:** `catalog.check(plan, *, max_steps=8) ->
tuple[tuple[PlanStep, ...], ...]`. Checks that a [`Plan`](#plan) only calls the
catalog's entries, correctly, without any network or model call. Implemented
(#169); [`ctx.plan`](#runcontextplan) and [`ctx.run_plan`](#runcontextrun_plan) run it too.

| Parameter | Type | Required / default | Meaning |
| --- | --- | --- | --- |
| `plan` | `Plan` | Required | The plan to check. |
| `max_steps` | `int` | `8` | The most steps the plan may have, a whole number above 0. The author may raise it, but the limit never exceeds the tool calls the run has left (`RunLimits.max_tool_calls`), since each step is one tool call. |

It checks, in order:

1. at most `max_steps` steps and at most 64 KiB serialized as JSON;
2. step IDs well formed and unique;
3. every `use` names a catalog entry; `arguments` only on structured entries;
   `text` on, and only on, `ask` entries;
4. literal arguments against the entry's argument schema's **top level only**:
   every `required` property present (a reference counts) and, when the
   schema has `"additionalProperties": false`, no property it does not list.
   The receiving agent validates everything else when the step runs, as it
   does for every call; there is no JSON Schema validator;
5. every reference, placeholder and `after` names a step of the plan, and is
   well formed;
6. `result` names a step;
7. no dependency cycle, including through `after` and a step referring to
   itself.

It returns the steps in **parallel groups**: each group's steps depend only on
steps in earlier groups, so they can run at once. Groups are a deterministic
topological order, and steps keep their plan order within a group.

```python
from agentframe import Plan, PlanStep, RunContext


async def review(ctx: RunContext, plan: Plan) -> list[list[str]]:
    catalog = await ctx.remote_skills(agents=["research-agent", "writer-agent"])
    groups = catalog.check(plan, max_steps=6)  # raises InputValidationError
    return [[step.id for step in group] for group in groups]
```

For the plan in [`Plan`](#plan) this returns `[["tides", "waves"],
["brief"], ["post"]]`.

**Failures:** an invalid plan raises `InputValidationError` whose message names
the step (by ID, or by position when the ID itself is malformed) and the
field, for example `Plan step 'brief' field 'arguments.sources' refers to no
step of the plan`. Messages never include argument values, unknown entry
names, malformed IDs or reference targets; an argument name is included only
when it is 1-64 letters, digits, `_` or `-`. A `max_steps` that is not a whole
number above 0 raises `DefinitionError`. A deeply nested plan built with
`model_construct` (validation refuses one) raises `InputValidationError`
rather than `RecursionError`. Checking says nothing about permissions:
entries a caller lacks are hidden from [`ctx.plan`](#runcontextplan) and
[`ctx.run_plan`](#runcontextrun_plan), and re-checked, with `tool_checks`,
before each step runs (#171).

**Related:** [validation before execution](../design/orchestration.md#validation-before-execution).

## `RunContext.plan`

**Purpose / signature:** let the agent's model turn a goal into a checked
[`Plan`](#plan) over a catalog, in one call, before anything is sent to
another agent. Implemented (#170).

```python
async def plan(
    self,
    goal: str,
    skills: SkillCatalog,
    *,
    max_steps: int = 8,
    instructions: str | None = None,
) -> Plan
```

| Parameter | Type | Required / default | Meaning |
| --- | --- | --- | --- |
| `goal` | `str` | Required | What the plan should achieve. Untrusted content, such as caller input; sent only as user-message data. |
| `skills` | `SkillCatalog` | Required | A catalog from [`ctx.remote_skills`](#runcontextremote_skills). Anything else raises `DefinitionError`. |
| `max_steps` | `int` | `8` | The most steps, a whole number above 0, never more than the tool calls the run has left (as [`check`](#skillcatalogcheck)). The model is told the effective limit. |
| `instructions` | `str \| None` | `None` | Extra trusted planning instructions, such as domain hints, sent after the library's planning instructions. Never put untrusted content here. |

```python
from typing import Literal

from agentframe import Agent, Plan, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]
type Writer = Remote[Literal["writer-agent"]]


@agent(name="Orchestrator", version="0.1.0")
class Orchestrator(Agent):
    """Plans goals over two specialists."""

    @skill(id="propose", tags=["orchestration"])
    async def propose(
        self, goal: str, research: Research, writer: Writer, ctx: RunContext
    ) -> Plan:
        """Return a checked plan for review; nothing runs yet."""
        skills = await ctx.remote_skills(research, writer)
        return await ctx.plan(goal, skills, max_steps=6)
```

**Behavior:**

- **One structured-output call** with the agent's model, like
  `ctx.generate(output=...)`: it counts toward `max_model_calls` and the
  run's tokens, uses the agent's `model_settings` (including
  `max_output_tokens`, which bounds the reply) and transient-failure retries,
  and the execution policy's `model_request_checks` see it.
- **Trusted and untrusted text stay apart.** The `system` messages are the
  agent's `instructions`, then the library's planning instructions plus
  `instructions=`. The goal and the catalog go together in the one `user`
  message as JSON data: `{"goal": ..., "skills": [...]}`, each skill with
  its `name`, bounded `description` and `tags`, `takes` (`"arguments"` or
  `"text"`) and `arguments_schema`. Card text is never in a `system`
  message. This keeps roles separate; it does not guarantee resistance to
  prompt injection.
- **Permissions.** Only entries the caller holds every
  `requires_permissions` for (see [permissions](#permissions-and-tool-checks))
  are shown to the model or accepted in its plan. A step using a hidden entry
  fails exactly like one using an unknown name, so the error reveals nothing.
- **Model-facing shape.** The reply schema converts for Foundry strict mode
  and Ollama `format`: `{"steps": [{"id", "use", "arguments", "after"}],
  "result", "reason"}`, with `use` an enum of the visible entry names and
  `arguments` a JSON object written as a string. An `ask` entry's arguments
  are `{"text": ...}`, as when it is offered as a tool; that text becomes the
  step's `text`. To decline, the model returns no steps and a short `reason`.
- **Checked, never repaired.** The reply is parsed into a `Plan` and checked
  with [`SkillCatalog.check`](#skillcatalogcheck) against the visible
  entries. It is never repaired or re-requested; to try again (for example
  after a failed run), call `ctx.plan` again, perhaps with a reviewed summary
  in the goal. There is no automatic replanning.

**Failures** (none sends anything to another agent):

| Error | When |
| --- | --- |
| `DefinitionError` | `skills` is not a `SkillCatalog`, or `max_steps` is not a whole number above 0. Before the model call. |
| `InputValidationError` | The catalog has no entry the caller may use (`"No skills are available to plan with"`), before the model call; or the model declined: always `"No plan for this goal with the available skills"`, never the model's reason. |
| `OutputValidationError` | The reply is not the requested JSON, a step's `arguments` is not a JSON object (or not exactly `{"text": string}` for an `ask` entry), or `check` rejects the plan, with `check`'s message. Messages name the step and field, never values. No second model call. |
| `UnsupportedCapabilityError` | The agent has no model, or its model does not support structured output. Before sending. |
| Others | As `ctx.generate`: `LimitExceededError` subclasses, `PermissionDeniedError` or `PolicyUnavailableError` from a model-request check, `ModelRefusalError`, `IncompleteOutputError`, `ProviderError`. |

**Related:** [planning call](../design/orchestration.md#planning-call),
[replanning](../design/orchestration.md#replanning).

## `RunContext.run_plan`

**Purpose / signature:** `await ctx.run_plan(plan, skills, *, max_parallel=4,
max_tokens_per_step=None, max_steps=8) -> PlanResult`. Checks a
[`Plan`](#plan) against the catalog again, then runs its steps as remote calls,
in parallel where their dependencies allow. No model sees any result between
steps. Implemented (M10 #171); the `on_failure="continue"` option (#172) is
planned.

| Parameter | Type | Required / default | Meaning |
| --- | --- | --- | --- |
| `plan` | `Plan` | Required | The plan, from [`ctx.plan`](#runcontextplan), built by the skill or received as an argument. A caller-supplied plan is untrusted and gets every check below. |
| `skills` | `SkillCatalog` | Required | The catalog the plan was made for, built by this run with [`remote_skills`](#runcontextremote_skills); any other raises `DefinitionError`. |
| `max_parallel` | `int` | `4` | The most steps running at once, a whole number above 0. |
| `max_tokens_per_step` | `int \| None` | `None` | The tokens each step holds while it runs. `None` shares the tokens the run has left when the plan starts equally among its steps. Set by trusted code, never by the model. |
| `max_steps` | `int` | `8` | As for [`SkillCatalog.check`](#skillcatalogcheck): the most steps the plan may have, never above the tool calls the run has left. Pass the same value given to `ctx.plan`. |

```python
from agentframe import Agent, Plan, RunContext, agent, skill


@agent(name="Orchestrator", description="Runs reviewed plans.", version="0.1.0")
class Orchestrator(Agent):
    @skill(
        id="execute",
        description="Run a reviewed plan.",
        tags=["orchestration"],
        requires_permissions=["plans:run"],
    )
    async def execute(self, plan: Plan, ctx: RunContext) -> str:
        skills = await ctx.remote_skills(agents=["research-agent", "writer-agent"])
        result = await ctx.run_plan(plan, skills, max_tokens_per_step=2_000)
        return str(result.value)
```

**Order.** The plan is checked first, as `ctx.plan` checks it: with
[`SkillCatalog.check`](#skillcatalogcheck) against only the entries the
caller may use, so a step using a hidden entry fails exactly like one using
an unknown name ("names no entry of the skill catalog"). An invalid plan
sends nothing. Steps then run under one
`asyncio.TaskGroup`: a step starts when every step it references or lists in
`after` has completed, with at most `max_parallel` running. Just before a step
is sent:

1. It counts as one tool call toward `RunLimits.max_tool_calls`.
2. Its references are replaced by deep copies of earlier values:
   `{"$ref": "step"}` by the whole value and `{"$ref": "step.field"}` by one
   top-level field, keeping types. In `ask` text, `{step}` and `{step.field}`
   insert a string value as it is and any other value as compact JSON with
   sorted keys (`{"a":1,"b":[2]}`), never Python's `str()` form. Only values
   change, never argument names, so the plan check's top-level argument check
   still holds.
3. The caller must hold the entry's `requires_permissions`
   ([permissions](#permissions-and-tool-checks)), checked again for every
   step as defense in depth.
4. Every `ExecutionPolicy.tool_checks` check gets the running skill, the
   principal, the **entry name** as `tool_name` and the read-only arguments
   about to be sent, after substitution (`{"text": ...}` for an `ask` entry).
   This is the data-exfiltration gate: a check sees exactly which data goes to
   which agent.

The step is then `Remote.call` (structured entry) or `Remote.ask` (`ask`
entry) on the catalog's binding to its agent, so the run's remaining
deadline, hop depth, identity (`remotes=` entry or `default_remote`), trace
context, correlation and token reconciliation apply as for any remote call.
Each step holds `max_tokens_per_step` (or the equal share) of the run's
token budget; the peer's reported usage replaces the hold, and a peer that
does not report usage holds nothing and makes the step's and the run's usage
unknown, never zero.

**Failures.** The first failing step stops the plan: running steps are
cancelled, each sending its agent a best-effort `CancelTask`, later steps are
never sent, and the failing step's error is raised as itself, never as an
`ExceptionGroup`. No step is retried. Messages name steps, fields and entries,
never argument values or results; nothing is logged with them.

| Situation | Result |
| --- | --- |
| `skills` not built by this run, or `max_parallel`, `max_tokens_per_step` or `max_steps` not a whole number above 0 | `DefinitionError`; nothing is sent |
| The plan is invalid for the entries the caller may use (including a step using a hidden entry), or has more steps than `max_steps` or the tool calls the run has left | `InputValidationError` from the check; nothing is sent |
| A `.field` reference into a value that is not an object or lacks the field (for example a structured skill that returned text) | `InputValidationError`, for example `Plan step 'brief' field 'arguments.sources' refers to field 'summary' of 'tides', whose result has no such field`; that step is not sent |
| The caller lacks a permission the entry requires, or a tool check denies the step | `PermissionDeniedError`; that step is not sent |
| A tool check raises or is still running at the run's deadline | `PolicyUnavailableError`; that step is not sent |
| A step's tokens do not fit what the run has left, or fewer tokens are left than the plan has steps (default share) | `TokenLimitExceededError` before that step (or any step) is sent |
| The run's deadline passes | `TimeoutLimitExceededError`; running steps are cancelled |
| The run is at its `max_remote_hops` depth | `LimitExceededError`; nothing is sent |
| A remote agent fails, refuses or cannot be reached, or asks a question | `RemoteAgentError` or `ClarificationNeededError`, as for [`Remote.call`](discovery.md#remotecall) |

An `OnBehalfOf` identity (per name or as `default_remote`) without a
signed-in user raises `PermissionDeniedError` when the catalog is built, before
any plan runs.

**Related:** [design: running a plan](../design/orchestration.md#running-a-plan),
[limits: cross-agent budgets](../guides/limits.md#cross-agent-budgets-and-depth-m8),
[LLMCompiler](https://arxiv.org/abs/2312.04511).

## `PlanResult`

**Purpose / signature:** what [`ctx.run_plan`](#runcontextrun_plan) returns.
A frozen dataclass. Implemented (#171); `complete` and the plan's total
`usage` are planned with #172 and #173.

| Field | Type | Meaning |
| --- | --- | --- |
| `value` | `Any` | The value of the plan's `result` step. |
| `steps` | `Mapping[str, StepResult]` | Every step's [`StepResult`](#stepresult), by step ID, in the order the steps were checked to run. |

## `StepResult`

**Purpose / signature:** one step's result in a [`PlanResult`](#planresult).
A frozen dataclass. Implemented (#171).

| Field | Type | Meaning |
| --- | --- | --- |
| `status` | `Literal["completed"]` | Under the default `stop` policy every step of a returned plan completed. Other statuses come with `on_failure="continue"` (#172). |
| `value` | `Any` | The step's result: the remote skill's decoded JSON result (`str` for a text result), or the `ask` answer's text. Untrusted content from another agent. |
| `usage` | `Usage \| None` | The usage the agent reported for the step; `None` when unknown, for example because the agent does not report usage. |
| `remote_task_id` | `str \| None` | The remote agent's task ID, which ties the step to that agent's logs; `None` when it answered without a task. |

## Remote skills as model tools

**Purpose / signature:** let the orchestrator's model call registered agents'
skills, one at a time, with the existing [tool loop](agents.md#implemented-tool-calling-m5-61).
Pass catalog entries to [`RunContext.generate`](agents.md#runcontextgenerate)
next to, or instead of, `@tool` functions. Implemented (M10 #167).

```python
from agentframe import Agent, RunContext, agent, skill


@agent(name="Orchestrator", description="Delegates to specialists.", version="0.1.0")
class Orchestrator(Agent):
    @skill(id="answer", description="Answer a goal.", tags=["orchestration"])
    async def answer(self, goal: str, ctx: RunContext) -> str:
        skills = await ctx.remote_skills(tags=["research"], max_tokens_per_call=5000)
        return await ctx.generate(goal, tools=[*skills])
```

Run it with `Runtime(directory=...)` or `AgentApp(directory=...)`, and a model
that supports tool calling.

**The tools.** Each entry becomes one model tool:

| Tool field | Value |
| --- | --- |
| Name | `entry.name` with every character other than letters, digits, `_` and `-` replaced by `_`: `"research-agent.find_sources"` becomes `"research-agent_find_sources"`. Providers such as Foundry accept no `.`. A name over 64 characters raises `DefinitionError`. |
| Description | The entry's bounded card description. |
| Parameters | A copy of the entry's `arguments_schema`; `{"text": string}` for an `ask` entry. Foundry converts it to its strict form like any tool's. |

Card text goes only into these tool definitions, never into trusted `system`
instructions. The model's tool-call name is looked up in this mapping; a name
that is not in it runs nothing (`{"error": "Unknown tool. ..."}`).

**A call.** A structured entry runs `Remote.call(skill, **arguments)` and an
`ask` entry `Remote.ask(text)` on the catalog's binding (a declared `Remote`
or a registry agent), so the run's deadline, hop depth, identity, trace
context, correlation and token reconciliation apply exactly as for a declared
`Remote`. Each call holds the catalog's `max_tokens_per_call`, or everything
the run has left; a peer's reported usage replaces the hold once, and a peer
that does not report usage makes the run's usage unknown, never zero. Calls
run one at a time in the model's order, each counts toward `max_tool_calls`,
and none is ever retried, including when the next model call is retried after
a transient failure: remote side effects are unknown.

| Situation | Result |
| --- | --- |
| The call succeeds | The result goes back to the model as untrusted tool output: the `ask` answer as text, a structured result as JSON |
| Arguments are not a JSON object, or an `ask` call's are not exactly one string `text` | Nothing is sent; the model gets `{"error": "Invalid arguments: ..."}`. Other argument checks are the remote agent's |
| `RemoteAgentError` (unreachable, refused, failed, oversized reply) or `ClarificationNeededError` | The model gets `{"error": "The remote call failed: <class name>"}` and can try something else; one WARNING log names the entry, the class and its safe message |
| `max_tool_calls` reached | `ToolCallLimitExceededError` before the call |
| `max_tokens_per_call` (or any token) does not fit what the run has left | `TokenLimitExceededError`; nothing is sent |
| The caller lacks the entry's `requires_permissions`, or a tool check denies the call | `PermissionDeniedError`; nothing is sent ([permissions and tool checks](#permissions-and-tool-checks)) |
| A tool check raises or is still running at the run's deadline | `PolicyUnavailableError`; nothing is sent |
| Any other error: limits (deadline, hop depth), `AuthenticationError`, `PermissionDeniedError` from the remote agent | Fails the run; the model is never told |

**Refused before any model call** (`DefinitionError`): an entry whose tool name
matches a selected `@tool` or another entry's, an entry selected twice, or an
entry that is not from a catalog this run built (matched by identity, so one
from another run, or constructed by hand, is refused). An argument schema the
model's provider cannot use, such as a free-form object for Foundry's strict
mode, raises `UnsupportedCapabilityError` before sending.

`@tool` functions still cannot take `Remote` parameters (#87): catalog entries
are the supported way to give a model remote agents.

**Related:** [design: remote skills as model tools](../design/orchestration.md#remote-skills-as-model-tools),
[tool calling](agents.md#implemented-tool-calling-m5-61),
[`Remote`](discovery.md#remoteliteralname).

## Permissions and tool checks

**Purpose / signature:** check every model-chosen remote call against the
caller's permissions and the application's policy, so a model, or a prompt
injection, cannot make the orchestrator act beyond the caller's authority.
Configured with `remote_skills(permissions=...)` and the existing
[`ExecutionPolicy.tool_checks`](authorization.md#executionpolicy); no new
policy protocol. Implemented (M10 #168).

```python
from collections.abc import Mapping
from typing import Any

from agentframe import (
    Agent,
    ExecutionPolicy,
    Principal,
    RunContext,
    SkillDefinition,
    agent,
    skill,
)


@agent(name="Orchestrator", description="Delegates to specialists.", version="0.1.0")
class Orchestrator(Agent):
    @skill(id="answer", description="Answer a goal.", tags=["orchestration"])
    async def answer(self, goal: str, ctx: RunContext) -> str:
        skills = await ctx.remote_skills(
            agents=["research-agent", "billing-agent"],
            permissions={"billing-agent.refund": {"refunds:write"}},
        )
        return await ctx.generate(goal, tools=[*skills])


async def no_refunds_over_100(
    skill: SkillDefinition,
    principal: Principal | None,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> bool:
    return tool_name != "billing-agent.refund" or arguments.get("amount", 0) <= 100


policy = ExecutionPolicy(tool_checks=[no_refunds_over_100])
```

Pass `policy=policy` to `AgentApp` (or `Runtime`) next to `directory=`.

The same rules as for [`@tool` functions](authorization.md#toolauthorizationpolicy):

1. **Hidden.** An entry whose `requires_permissions` the caller (the run's
   verified `principal`) does not all hold is left out of the tools sent to
   the model. A caller with no principal holds no permissions. The model, and
   anyone reading its request, sees no hint of what other callers can do.
   Model-request checks see the request without it.
2. **Re-checked before every call.** A model-chosen call naming a hidden entry
   (as a scripted or injected call could) fails the run with
   `PermissionDeniedError`. The permissions are checked before the arguments,
   so a malformed call to a hidden entry fails the run too, and the model
   learns nothing about the entry.
3. **Tool checks.** After the arguments are parsed, and immediately before
   sending, every `ExecutionPolicy.tool_checks` check runs, in order, with:
   the running skill's `SkillDefinition` (the same object `@tool` calls pass),
   the principal, `tool_name` set to the **entry name** (`"agent.skill"` or,
   for `ask`, `"agent"`; never the provider-safe tool name) and the read-only
   arguments about to be sent (`{"text": ...}` for `ask`). Only an explicit
   `True` sends the call. Anything else raises `PermissionDeniedError`; a
   check that raises or is still running at the run's deadline raises
   `PolicyUnavailableError`. Either way nothing is sent, the run fails and the
   model is never told.

Checks run for every model-chosen call, each of which counts toward
`max_tool_calls` as before. A denied call is never turned into a tool error,
since that would let the model probe the policy. Errors name only the entry,
never the arguments; nothing is logged with arguments or results. The
runtime's `ToolAuthorizationPolicy` is for `@tool` functions only, because its
`check_tool` receives a `ToolDefinition`; use tool checks for remote entries.

**`ask` entries inherit their agent's skill permissions.** Free text can ask
an agent for anything its skills do, so an agent's `ask` entry (from
`ask=True`, or the only entry of an agent without the skill-call extension)
requires the union of the permissions given for that agent's skills, plus any
given for the `ask` entry itself, even for skills that `skill=`/`tags=` left
out. Protecting `"billing-agent.refund"` therefore protects free text to
`billing-agent` too.

### Unmatched `permissions` keys

A key that protects nothing would leave the entry it meant callable by anyone,
so mistakes are reported:

| Key | Result |
| --- | --- |
| An entry's provider-safe tool name instead of its name (`"billing-agent_refund"`) | `DefinitionError` naming the entry name to use |
| With declared `Remote` parameters: the agent's name, or its name followed by `.`, that names no entry of its card (a typo, or the agent's name without `ask=True`) | `DefinitionError`. Entries `skill=`/`tags=` left out still count as entries, so they are not errors |
| Any other key matching no entry in this run's catalog | One WARNING log per key, naming only the key: `remote_skills permissions= key '<key>' matched no catalog entry` |

In registry mode keys are matched only against the entries present in this
run's catalog: an agent that is down or skipped, or a registry that changed,
legitimately removes entries, so a typo there is a warning, not an error. For
consequential entries add an `ExecutionPolicy` tool check on the entry name
as defense in depth: it runs on every call whatever the catalog holds.

**The 20-entry limit counts hidden entries.** The catalog's size is checked
when it is built, before any caller's permissions hide entries, so whether a
catalog is too large never depends on who calls.

**Remote agents still decide.** Each remote agent authenticates and authorizes
the request itself. With `OnBehalfOf` it sees the original user; with
`Service` it sees the orchestrator. With `Service` identity, any caller who
reaches the orchestrator could otherwise make its model call any catalog entry
with the orchestrator's identity (a confused deputy): give consequential
entries `permissions=`, add tool checks, narrow the catalog, or prefer
`OnBehalfOf`. See the
[authorization guide](../guides/authorization.md#model-chosen-remote-calls).

**Related:** [design: authorization and approval](../design/orchestration.md#authorization-and-approval),
[`ExecutionPolicy`](authorization.md#executionpolicy),
[OWASP LLM06 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/).
