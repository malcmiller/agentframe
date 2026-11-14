# Orchestration: remote skill catalogs

Model-planned orchestration ([M10](../design/orchestration.md)) lets an
orchestrator's model choose which registered agent and skill to call. This
page covers its first piece, the **remote skill catalog**: implemented (M10
[#166](https://github.com/malcmiller/agentframe/issues/166)). Offering
catalog entries to a model as tools (#167), plans (#169-#174) and the
optional `default_remote` identity (#175) are planned, not implemented.

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
) -> SkillCatalog: ...
```

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `*remotes` | `Remote[Any]` | Optional | The running skill's `Remote` parameters. When given, the catalog holds exactly these agents. When omitted, every registered agent is considered. |
| `agents` | `Collection[str] \| None` | Optional; default `None` | Agent names to keep. A name no source lists (or, with `remotes`, that none of them has) raises `DefinitionError`, unless a source failed to list. A single string raises `DefinitionError`. |
| `skill` | `str \| None` | Optional; default `None` | Keep only card skills with this ID. |
| `tags` | `Collection[str]` | Optional; default `()` | Keep only card skills carrying all of these tags, matched with `skill` on the same card skill, as [`AgentDirectory.find`](discovery.md#agentdirectoryfind). A single string raises `DefinitionError`. |
| `ask` | `bool` | Optional; default `False` | Also add a free-text `ask` entry for agents with the skill-call extension. |
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
base A2A has no standard way to select a skill.

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
  with a redirect-following HTTP client, or more than 20 entries.
- `RemoteAgentError`: a declared `Remote`'s card cannot be fetched (raised
  while binding the parameter, as in M8+), or its argument schema is missing or
  over 8 KiB.
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
| `requires_permissions` | `frozenset[str]` | Permissions a caller needs to use the entry. Empty until `permissions=` arrives with #168. |

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
| `iter(catalog)` | `Iterator[RemoteSkill]` | Iterates the entries. |
| `len(catalog)` | `int` | The number of entries, at most 20. |

```python
from agentframe import RunContext


async def option_names(ctx: RunContext) -> list[str]:
    catalog = await ctx.remote_skills(agents=["research-agent", "writer-agent"])
    return [entry.name for entry in catalog]
```

A catalog belongs to the run that built it: each binding carries that run's
deadline, hop depth, token budget, identity and correlation, exactly like a
`Remote` parameter. Calling entries through the catalog arrives with #167
(model tools) and #171 (plans).

**Related:** [M10 overview](https://github.com/malcmiller/agentframe/issues/177).
