# Orchestration: remote skill catalogs and remote skills as tools

Model-planned orchestration ([M10](../design/orchestration.md)) lets an
orchestrator's model choose which registered agent and skill to call. This
page covers its first pieces, all implemented: the **remote skill
catalog** (M10 [#166](https://github.com/malcmiller/agentframe/issues/166)),
offering its entries to a model as **tools**
([#167](https://github.com/malcmiller/agentframe/issues/167)) and
[permissions and tool checks](#permissions-and-tool-checks) for model-chosen
calls ([#168](https://github.com/malcmiller/agentframe/issues/168)). Plans
(#169-#174) and the optional `default_remote` identity (#175) are planned, not
implemented.

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
   `Remote` parameters get at startup).

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
- `PermissionDeniedError`: an included agent's `remotes=` entry is
  `OnBehalfOf`, and the run has no signed-in user.

**Related:** [remote skill catalog design](../design/orchestration.md#remote-skill-catalog),
[catalog size](../design/orchestration.md#catalog-size),
[`AgentDirectory`](discovery.md#agentdirectory).

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
(#171) are planned.

**Related:** [M10 overview](https://github.com/malcmiller/agentframe/issues/177).

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
