# Registries and managed discovery

[Reference status conventions](README.md). The registry entries
([`AgentRegistry`](#agentregistry) through [`FileAgentRegistry`](#fileagentregistry))
are implemented (M8+ #83), and [`AgentDirectory`](#agentdirectory) with
[`get`](#agentdirectoryget), [`refresh`](#agentdirectoryrefresh), its async
context manager and [`DiscoveredAgent`](#discoveredagent) (#84),
[`find`](#agentdirectoryfind) (#85), and
[`FoundryAgentRegistry`](#foundryagentregistry) with
[`AgentRegistry.card_headers`](#agentregistrycard_headers) (#86); every other API
here is unimplemented, and its
confirmed names/defaults are decisions while unspecified signatures remain
illustrative. Discovery/registry and remote A2A invocation are core
scope for the next multi-agent release (M8+), after the M7 single-agent release,
not optional add-ons. Orchestrators are ordinary AgentFrame skills that call
independently deployed specialists over A2A, not a workflow/graph engine.
A2A is not a registry protocol.
See the [design and lifecycle guide](../design/discovery.md) for composition examples.

## `AgentRegistry`

**Purpose / decision:** small read-only abstract base users extend for their own
source of agent references. Unlike optional structural dependencies elsewhere,
this extension point explicitly uses inheritance. It is not a global plugin
registry.

**Signature status:** implemented in M8+ #83. The base takes no constructor
parameters. Subclasses implement async [`get`](#agentregistryget) and
[`list`](#agentregistrylist) and may override
[`card_headers`](#agentregistrycard_headers) (#86); no write or lifecycle
methods exist.

| Contract | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `get` implementation | `async (agent_id: str) -> AgentReference` | Required abstract method | Return one agent's reference; raise when the source fails or does not list it. Do not fetch or cache cards. |
| `list` implementation | `async (query: AgentQuery) -> list[AgentReference]` | Required abstract method | Return matching references; empty only when the source authoritatively lists none. |
| `card_headers` override | `async (reference: AgentReference) -> Mapping[str, str]` | Optional; default returns `{}` | Headers, such as the source's own bearer token, to send with that agent's card request. |

```python
from agentframe import AgentQuery, AgentReference, AgentRegistry, DefinitionError


class InMemoryAgentRegistry(AgentRegistry):
    def __init__(self, references: list[AgentReference]) -> None:
        self._references = references

    async def get(self, agent_id: str) -> AgentReference:
        for reference in self._references:
            if reference.name == agent_id:
                return reference
        raise DefinitionError(f"No agent named '{agent_id}'.")

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        if query.names is None:
            return list(self._references)
        return [ref for ref in self._references if ref.name in query.names]
```

**Behavior / failures:** do no I/O at import or construction. A missing or
unreadable source, malformed references and an unknown agent are explicit
failures, never empty results. The supplied registries raise `DefinitionError`
naming the source and agent; custom registries should do the same for
configuration problems. A subclass missing either method cannot be instantiated
(`TypeError`). No `register`, deployment, HTTP, polling or card refresh.

**Related:** [extension guide](../design/discovery.md#extending-the-read-base),
[`AgentDirectory`](#agentdirectory).

## `AgentRegistry.get`

**Purpose / signature:** read one agent's reference by name. Implemented (#83).

```python
async def get(self, agent_id: str) -> AgentReference: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent_id` | `str` | Required | The agent's name in this registry; not a URL and not proof of authorization. |

```python
from agentframe import AgentReference, AgentRegistry


async def lookup(registry: AgentRegistry) -> AgentReference:
    return await registry.get("research-agent")
```

**Behavior / failures:** returns a validated reference, not a card or an
invocation. An unknown name raises `DefinitionError` naming the source and the
agent, for example `Environment variable AGENTFRAME_AGENTS does not list an
agent named 'writer'.`; a source read failure raises before any lookup.

**Related:** [`AgentReference`](#agentreference),
[failure boundaries](../design/discovery.md#failure-trust-and-isolation-boundaries).

## `AgentRegistry.list`

**Purpose / signature:** read the references matching a query. Implemented (#83).

```python
async def list(self, query: AgentQuery) -> list[AgentReference]: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `query` | `AgentQuery` | Required | `AgentQuery()` returns every agent in the source; `AgentQuery(names=[...])` keeps only those names. |

```python
from agentframe import AgentQuery, AgentReference, AgentRegistry


async def reviewers(registry: AgentRegistry) -> list[AgentReference]:
    return await registry.list(AgentQuery(names=["review-agent"]))
```

**Behavior / failures:** the supplied registries return references in source
order; names in the query that the source does not list are ignored. An
authoritative empty result is valid; a source failure raises instead. Skill and
tag matching happens on cards in the directory
([`find`](#agentdirectoryfind)), not here. Matching is not access permission.
A paged source such as [`FoundryAgentRegistry`](#foundryagentregistry) reads
every page and raises if any page fails, so a result is never a partial first
page. `AgentDirectory.get` lists with `AgentQuery(names=[name])` on every call,
so `list` must honor `names`.

**Related:** [`AgentQuery`](#agentquery),
[query placement](../design/discovery.md#query-matching-and-card-enrichment).

## `AgentRegistry.card_headers`

**Purpose / decision:** let a source send its own credentials when the
directory fetches one of its agents' cards. Implemented (#86; decision
confirmed 2026-11-12). Not abstract: the default returns no headers.

```python
async def card_headers(self, reference: AgentReference) -> Mapping[str, str]: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `reference` | `AgentReference` | Required | An agent this registry listed. |

```python
from collections.abc import Mapping

from agentframe import AgentReference, FileAgentRegistry


class TokenFileRegistry(FileAgentRegistry):
    def __init__(self, path: str, token: str) -> None:
        super().__init__(path)
        self._token = token  # application-supplied placeholder

    async def card_headers(self, reference: AgentReference) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self._token}"}
```

**Behavior / failures:** [`AgentDirectory`](#agentdirectory) calls it only when
it fetches a card, and sends the headers only on that one request to the
registry's `card_url`: never on redirects (which are refused), other agents'
cards, later A2A calls or the client's defaults. The current caller's identity
is never added, so the cached card is the same for every caller. A raised
`DefinitionError` propagates from `get`/`refresh`. Keep the headers to your own
source's origin; `FoundryAgentRegistry` refuses card URLs outside its project.

**Related:** [`AgentDirectory`](#agentdirectory),
[`FoundryAgentRegistry`](#foundryagentregistry).

## `AgentReference`

**Purpose / decision:** one agent a registry lists: its name and where its Agent
Card is. Implemented (#83) as a frozen dataclass.

```python
@dataclass(frozen=True)
class AgentReference:
    name: str
    card_url: str
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `name` | `str` | Required | Non-empty; unique within a source. Not an authorization grant. |
| `card_url` | `str` | Required | Absolute `http` or `https` URL of the agent's **Agent Card** (not its A2A endpoint), without credentials, query or fragment. |

```python
from agentframe import AgentReference

research = AgentReference(
    "research-agent", "https://research.example.com/.well-known/agent-card.json"
)
```

**Behavior / failures:** construction raises `DefinitionError` for an empty name
or an unsafe URL; the message names the agent but never echoes the URL. The
check is the one `HostConfig.public_url` uses. The registry accepts plain
`http`; the directory (#84) refuses it except for loopback hosts or an explicit
`allow_http_hosts` opt-in. A valid reference is not trusted, healthy,
authorized or guaranteed invocable. It holds no secrets, transport, skills or
tags.

**Related:** [source format](../design/discovery.md#sources-and-reference-format),
[`AgentDirectory`](#agentdirectory).

## `AgentQuery`

**Purpose / decision:** which agents to list from a registry. Implemented (#83)
as a frozen keyword-only dataclass.

```python
@dataclass(frozen=True, kw_only=True)
class AgentQuery:
    names: Collection[str] | None = None
```

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `names` | `Collection[str] \| None` | Optional; default `None` | `None` means every agent in the source; otherwise only the named agents. Stored as a `frozenset`; a single string raises `DefinitionError`. |

```python
from agentframe import AgentQuery

everyone = AgentQuery()
only_research = AgentQuery(names=["research-agent"])
```

**Behavior / failures:** there are no skill or tag fields; those match on cards
through [`AgentDirectory.find`](#agentdirectoryfind). Query matching cannot
grant access. Paged sources read every page; result ordering follows the source.

**Related:** [`AgentRegistry.list`](#agentregistrylist),
[enrichment boundary](../design/discovery.md#query-matching-and-card-enrichment).

## `EnvironmentAgentRegistry`

**Purpose / decision:** agents listed in an explicitly named environment
variable. Implemented (#83).

```text
EnvironmentAgentRegistry(variable: str)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `variable` | `str` | Required | The variable's name, for example `"AGENTFRAME_AGENTS"`. No implicit search. |

The value is comma-separated `name=url` pairs, where each URL is an Agent Card
URL; whitespace around entries is ignored and an empty value lists no agents:

```shell
AGENTFRAME_AGENTS="research-agent=https://research.example.com/.well-known/agent-card.json,review-agent=http://127.0.0.1:8002/.well-known/agent-card.json"
```

```python
from agentframe import AgentQuery, EnvironmentAgentRegistry


async def main() -> None:
    registry = EnvironmentAgentRegistry("AGENTFRAME_AGENTS")
    print(await registry.list(AgentQuery()))
```

**Behavior / failures:** construction does not read the variable; every `get`
and `list` reads it again. An unset variable, an entry without `=`, an empty
name, an unsafe URL and a duplicate name each raise `DefinitionError` naming
the variable (and the agent where there is one). No card fetching or polling.

**Related:** [source formats](../design/discovery.md#sources-and-reference-format),
[`AgentRegistry`](#agentregistry).

## `FileAgentRegistry`

**Purpose / decision:** agents listed in a YAML file of names and Agent Card
URLs. Implemented (#83).

```text
FileAgentRegistry(path: str | os.PathLike[str])
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `path` | `str \| os.PathLike[str]` | Required | The YAML file. No automatic search. |

The file is a list of entries with exactly the keys `name` and `url` (an Agent
Card URL); `[]` lists no agents:

```yaml
- name: research-agent
  url: https://research.example.com/.well-known/agent-card.json
- name: review-agent
  url: http://127.0.0.1:8002/.well-known/agent-card.json
```

```python
from agentframe import FileAgentRegistry


async def research_card_url() -> str:
    registry = FileAgentRegistry("agents.yaml")
    return (await registry.get("research-agent")).card_url
```

**Behavior / failures:** construction does not touch the file. Every `get` and
`list` reads it again in a worker thread (`asyncio.to_thread`), so edits apply
without a restart; the directory caches cards, not files. Reads are capped at
1 MiB and parsed with `yaml.safe_load`, so executable YAML tags are refused.
A missing or unreadable file, a file over 1 MiB, non-UTF-8 text, malformed YAML
(only the line number is reported), a top level that is not a list, an entry
without exactly `name` and `url` strings, an empty name, an unsafe URL and a
duplicate name each raise `DefinitionError` naming the file. PyYAML is a core
dependency.

**Related:** [source format](../design/discovery.md#sources-and-reference-format),
[`AgentDirectory`](#agentdirectory).

## `FoundryAgentRegistry`

**Purpose / decision:** the agents in a Microsoft Foundry project that have
incoming A2A enabled, read through the Foundry project agents API with the
official `azure-ai-projects` package. Implemented (#86) and proven offline
against a fake of the API; the opt-in real-project test is below. Read-only:
it never enables A2A, publishes or deploys. Needs the `foundry` extra
(`pip install 'agentframe-py[foundry]'`, which pins `azure-ai-projects==2.11.0`);
imported lazily, so `import agentframe` works without it.

```text
FoundryAgentRegistry(
    *,
    project_endpoint: str,
    credential: AsyncTokenCredential,
    transport: AsyncHttpTransport | None = None,
)
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `project_endpoint` | `str` | Required | `https://{account}.services.ai.azure.com/api/projects/{project}`. Absolute https without credentials, query or fragment; a trailing `/` is ignored. Not a model inference URL. |
| `credential` | `azure.core.credentials_async.AsyncTokenCredential` | Required | Entra credential for listing agents and fetching their cards, for example `azure.identity.aio.DefaultAzureCredential()`. Stays yours: never closed by the registry. |
| `transport` | `azure.core.pipeline.transport.AsyncHttpTransport \| None` | Optional; default `azure-core`'s aiohttp transport | For a test double or an outbound proxy. |

```python
from azure.identity.aio import DefaultAzureCredential

from agentframe import AgentDirectory, FoundryAgentRegistry


async def foundry_card_name(agent_name: str) -> str:
    async with DefaultAzureCredential() as credential:
        source = FoundryAgentRegistry(
            project_endpoint=(
                "https://contoso.services.ai.azure.com/api/projects/research"
            ),
            credential=credential,
        )
        async with AgentDirectory(sources=[source]) as agents:
            return (await agents.get(agent_name)).card.name
```

**Behavior / failures:**

- Construction does no I/O. Each `get`/`list` creates and closes its own async
  `AIProjectClient`, so there is nothing to close.
- `list` follows every page of the project's agents (100 per page) and keeps
  agents whose `agent_endpoint.protocol_configuration.a2a` is set, the setting
  Foundry's [incoming A2A guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
  enables. `get` uses the single-agent call and raises `DefinitionError` for an
  unknown agent or one without incoming A2A.
- Each agent maps to `AgentReference(name, card_url)` with
  `card_url = {project_endpoint}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0`,
  Foundry's GA A2A 1.0 card. In `azure-ai-projects` 2.11.0 the A2A
  configuration has no version field: Foundry serves 1.0 and the 0.3 preview
  from the same setting, and the registry never selects 0.3. The card's
  interface (`.../endpoint/protocols/a2a`) has a different path but the same
  origin, which the directory requires.
- Any failed page, HTTP error or credential failure raises `DefinitionError`
  with only the project, status or error class, never a partial list or `[]`.
- [`card_headers`](#agentregistrycard_headers) returns
  `Authorization: Bearer <token>` for `https://ai.azure.com/.default` from
  `credential`, only for card URLs inside this project. Foundry cards require
  Entra ID (no anonymous access), so the directory sends this source identity's
  token, never the caller's; every caller sees the same card. Remote calls
  never reuse it: configure their identity with `remotes=` (#92), for example
  [`EntraAppCredential`](authorization.md#entraappcredential) with this same
  scope.
- Preview hosted-agent A2A passthrough and Foundry's
  [outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)
  are not read. A model deployment is not an agent endpoint; see
  [`FoundryModel`](models.md#foundrymodel) for inference.

**Roles** (from Microsoft's [Foundry RBAC](https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry)
documentation and built-in role definitions, checked 2026-11-13):

| Who | Needs | Role |
| --- | --- | --- |
| Whoever enables incoming A2A on an agent | Agent update on the project | **Foundry User** or higher. AgentFrame never does this. |
| The registry's `credential`, to list agents | Data action `Microsoft.CognitiveServices/accounts/AIServices/agents/read` | **Foundry User** on the project: the least-privileged built-in role with it. Foundry Agent Consumer does not have it. A custom role with only this data action and the one below is narrower. |
| The registry's `credential`, to fetch cards | Data action `Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action` | **Foundry Agent Consumer** (or Foundry User) on the project or agent. |
| Identities that later call the agent over A2A | Endpoint access | **Foundry Agent Consumer** or another endpoint-access role. |

The registry-read role is the expected one from the documented role
definitions; verify it with the opt-in test below before relying on it.

**Opt-in test** (`tests/integration/test_foundry_agents.py`, marked `foundry`,
skipped unless enabled). It only reads. One-time setup, outside AgentFrame:
create a prompt agent in a Foundry project, enable incoming A2A as the guide
shows, and give your `az login` identity Foundry User on the project (or the
custom role above). Then:

```bash
AGENTFRAME_TEST_FOUNDRY_AGENTS=1 \
  AGENTFRAME_FOUNDRY_PROJECT_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project> \
  AGENTFRAME_FOUNDRY_A2A_AGENT=<agent-name> uv run pytest -m foundry
```

**Related:** [chosen source and mapping boundaries](../design/discovery.md#sources-and-reference-format),
[`AgentDirectory`](#agentdirectory).

## `AgentDirectory`

**Purpose / decision:** one directory that combines agent sources and returns
validated native Agent Cards, so applications never write card-fetching,
caching or refresh code. Implemented (M8+ #84), with
[`find`](#agentdirectoryfind) (#85). The card fetcher and cache are internal: there are no public
`AgentCardResolver` or `CardCacheConfig` classes (decided 2026-11-12; keyword
arguments are enough until a second resolver is needed).

```text
AgentDirectory(
    *, sources: Sequence[AgentRegistry], card_ttl_seconds: float = 300,
    allow_stale: bool = False, allow_http_hosts: Collection[str] = (),
    http_client: httpx.AsyncClient | None = None,
)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `sources` | `Sequence[AgentRegistry]` | Required, non-empty | Registries read on every `get`/`refresh`. A name listed by more than one source raises `DefinitionError` naming both; there is no precedence. |
| `card_ttl_seconds` | `float` | Optional; default `300` | How long a fetched card stays fresh; finite, `0` or more (`0` refetches on every `get`). Not health checking or background polling. |
| `allow_stale` | `bool` | Optional; default `False` | When a stale card's refresh fails, serve the previous card with `stale=True` and log a warning instead of raising. Does not apply to `refresh` or to source failures. |
| `allow_http_hosts` | `Collection[str]` | Optional; default empty | Non-loopback host names whose cards may be fetched over plain `http`, for development. Case-insensitive; a single string raises `DefinitionError`. |
| `http_client` | `httpx.AsyncClient \| None` | Optional; default `None` | Caller-owned client used for card fetches and remote calls; never closed by the directory. It must not carry credentials in default headers or cookies, and must not follow redirects when any remote has credentials (startup refuses it). When omitted, the directory creates a client on `async with` and closes it on exit. Also lets tests use in-process ASGI apps. |

```python
from agentframe import AgentDirectory, EnvironmentAgentRegistry, FileAgentRegistry


async def research_card_name() -> str:
    directory = AgentDirectory(
        sources=[
            FileAgentRegistry("agents.yaml"),
            EnvironmentAgentRegistry("AGENTFRAME_AGENTS"),
        ],
        card_ttl_seconds=300,
        allow_stale=False,
    )
    async with directory:
        found = await directory.get("research-agent")
        return found.card.name
```

**Behavior / failures:** construction reads nothing, fetches nothing and starts
no background task. Sources are read on every call, so edits apply at once;
cards are cached per card URL. Every card fetch:

- goes only to `https`, except loopback hosts (`localhost`, `127.0.0.0/8`,
  `::1`) and `allow_http_hosts`; refused before any request is sent;
- carries only the source's own [`card_headers`](#agentregistrycard_headers)
  (none for file and environment sources): the client's `auth` is disabled and
  nothing about the current caller is added, so every caller sees the same
  cached card (one card variant; per-caller or authenticated extended cards
  are out of scope);
- is bounded to 10 seconds (and never past the caller's deadline, since a run's
  deadline cancels it), reads at most 1 MiB and does not follow redirects;
- is parsed by the official SDK into `a2a.types.AgentCard`, which must have a
  name and a JSON-RPC A2A 1.0 interface whose URL has the same origin (scheme,
  host and port) as the registry's card URL. The registry's `card_url` is the
  only trusted origin; outbound credentials ([`Service`](authorization.md#service),
  #92) go only there.

Failures caused by the other agent (unreachable, non-success HTTP status,
redirect, timeout, oversized body, invalid JSON, a body that is not an Agent
Card, no JSON-RPC 1.0 interface, a different-origin interface, refused plain
`http`) raise [`RemoteAgentError`](runtime.md#remoteagenterror) with a safe
message naming the agent, never the response body. Configuration mistakes
(unknown name, the same name in two sources, an unreadable source) raise
`DefinitionError`. A source failure is never treated as removal: the cached
card stays, and the call raises. Concurrent calls for one stale card share one
fetch (one lock per card URL). No deploy/start/stop/delete, registration writes,
automatic delegation or A2A task lifecycle.

Register a shared directory with `Services.add(AgentDirectory, directory)` and
inject `Inject[AgentDirectory]` into skills; the application opens and closes
it. For `Remote` parameters, pass it as `Runtime(directory=...)` or
`AgentApp(directory=...)` instead (see [`Remote`](#remoteliteralname)); the
application still opens and closes it.

**Related:** [`get`](#agentdirectoryget), [`refresh`](#agentdirectoryrefresh),
[`DiscoveredAgent`](#discoveredagent),
[managed lifecycle](../design/discovery.md#managed-directory-lifecycle),
[failure and isolation boundaries](../design/discovery.md#failure-trust-and-isolation-boundaries).

## `DiscoveredAgent`

**Purpose / decision:** what [`AgentDirectory.get`](#agentdirectoryget) returns.
Implemented (#84) as a frozen dataclass.

| Field | Type | Meaning |
| --- | --- | --- |
| `reference` | `AgentReference` | The source entry the agent was found through. |
| `card` | `a2a.types.AgentCard` | The validated official SDK card. |
| `interface_url` | `str` | The card's JSON-RPC A2A 1.0 interface URL, always on the card URL's origin. `Remote.ask` and `Remote.call` send only here. |
| `stale` | `bool` | `True` only when `allow_stale=True` served an old card after a failed refresh. |

```python
from agentframe import AgentDirectory


async def research_endpoint(directory: AgentDirectory) -> str:
    found = await directory.get("research-agent")
    return found.interface_url
```

**Behavior / failures:** a discovered agent is not proof of health, trust or
permission to call it.

**Related:** [`AgentDirectory`](#agentdirectory).

## `AgentDirectory.get`

**Purpose / signature:** return an agent and its validated card, from the cache
while fresh. Implemented (#84).

```python
async def get(self, name: str) -> DiscoveredAgent: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `name` | `str` | Required | The agent's name in exactly one source. |

```python
from agentframe import AgentDirectory


async def research_skills(directory: AgentDirectory) -> list[str]:
    found = await directory.get("research-agent")
    return [skill.id for skill in found.card.skills]
```

**Behavior / failures:** reads the sources, then returns the cached card if it
is younger than `card_ttl_seconds`; otherwise fetches it again. A failed refresh
raises `RemoteAgentError` unless `allow_stale=True`, which returns the previous
card with `stale=True` and logs a warning; a failed refresh never resets
freshness, so the next call tries again. Without `http_client`, calling before
`async with` raises `DefinitionError`. Retrieval does not invoke the agent.

**Related:** [`DiscoveredAgent`](#discoveredagent),
[failure/isolation constraints](../design/discovery.md#failure-trust-and-isolation-boundaries).

## `AgentDirectory.find`

**Purpose / signature:** find the native Agent Cards of listed agents that
advertise a matching skill, using validated card metadata rather than fields on
source references. Implemented (M8+ #85).

```python
async def find(
    self, *, skill: str | None = None, tags: Collection[str] = ()
) -> list[AgentCard]: ...
```

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `skill` | `str \| None` | Optional; default `None` | A card matches only if one of its advertised skills has this ID. |
| `tags` | `Collection[str]` | Optional; default `()` | That same skill must carry every one of these tags (all-of). A single string raises `DefinitionError`. |
| Result | `list[AgentCard]` | On success | The matching official SDK cards, in source order. Eligible cards, not invocation permission or Python methods. |

At least one of `skill` and `tags` is required. When both are given they must
match on the same advertised skill (decided 2026-11-12).

```python
from agentframe import AgentDirectory


async def research_count(directory: AgentDirectory) -> int:
    cards = await directory.find(skill="find_sources", tags=["research"])
    return len(cards)
```

**Behavior / failures:** lists every agent of every source, then resolves each
card exactly as [`get`](#agentdirectoryget) does (cached while fresh, refetched
when stale), with at most eight card fetches at a time. Any failure raises
instead of returning a shorter list: an unreadable source, a name listed by
more than one source, or no filter raises `DefinitionError`; an unreachable,
invalid or refused card raises `RemoteAgentError` unless `allow_stale=True`
can serve its previous card. There is no pagination: `find` reads every listed
agent, so it is meant for small curated sources. A found card cannot be called
yet; orchestrators call configured remotes, and dynamic invocation of found
agents is deferred to a later story (decided 2026-11-12). Matching is not
authorization.

**Related:** [`AgentDirectory`](#agentdirectory),
[search boundaries](../design/discovery.md#query-matching-and-card-enrichment).

## `AgentDirectory.refresh`

**Purpose / signature:** fetch an agent's card again, even if the cached one is
fresh. Implemented (#84).

```python
async def refresh(self, name: str) -> DiscoveredAgent: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `name` | `str` | Required | The agent's name in exactly one source. |

```python
from agentframe import AgentDirectory


async def reload_research(directory: AgentDirectory) -> str:
    return (await directory.refresh("research-agent")).card.version
```

**Behavior / failures:** raises as [`get`](#agentdirectoryget) does, except that
a failed refresh always raises `RemoteAgentError` (`allow_stale` does not
apply) and leaves the cached card and its freshness unchanged. It shares the
per-card-URL lock with `get`. Source-wide refresh and background refresh are
not provided.

**Related:** [managed lifecycle](../design/discovery.md#managed-directory-lifecycle),
[`AgentDirectory.get`](#agentdirectoryget).

## `AgentDirectory.__aenter__`

**Purpose / signature:** `async with directory` opens it. Implemented (#84);
returns the directory.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Context entry | Python async context-manager protocol | No user arguments | Creates an `httpx.AsyncClient` (no redirects) when no `http_client` was supplied. Reads no source and fetches no card. |

```python
from agentframe import AgentDirectory


async def inspect_scoped(directory: AgentDirectory) -> str:
    async with directory:
        found = await directory.get("research-agent")
        return found.card.name
```

**Behavior / failures:** entering a directory that is already open with its own
client raises `DefinitionError`. After exit it can be entered again; the card
cache is kept.

**Related:** [`AgentDirectory`](#agentdirectory),
[ownership guide](../design/discovery.md#managed-directory-lifecycle).

## `AgentDirectory.__aexit__`

**Purpose / signature:** leaving `async with` closes what the directory owns.
Implemented (#84).

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Exception context | Python context-manager protocol values | Supplied by Python | Never suppressed. |
| Owned resources | The client created on entry | Closed on exit | A caller-supplied `http_client` is never closed. |

```python
from agentframe import AgentDirectory


async def read_and_close(directory: AgentDirectory) -> None:
    async with directory:
        await directory.get("research-agent")
```

**Behavior / failures:** closes only the client the directory created; it does
not cancel remote A2A tasks or close sources.

**Related:** [`AgentDirectory.__aenter__`](#agentdirectory__aenter__).

## `RemoteAgentClient`

**Purpose / direction:** core M8+ native A2A client for invoking discovered
references/cards after application selection/authorization. Enables ordinary
orchestrator skills to call AgentFrame or other frameworks' A2A agents.

**Signature status:** underlying/explicit client construction remains open.
The decided authoring surface is [`Remote[Literal["name"]]`](#remoteliteralname), with
[`call`](#remotecall) and [`ask`](#remoteask), not an invented constructor here.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Discovered destination/card | Validated reference/native card; client interface open | Needed for selected interaction | Re-check destinations, trust, binding and current authorization, not just cache freshness. |
| Outbound identity | `remotes={name: Service(credential=...)}` (#92); `OnBehalfOf` is #93 | OBO recommended for Entra ingress; service explicit | Exchange target-scoped tokens; never forward incoming token or fall back on exchange failure. API-key/anonymous ingress supports only service. See [`Service`](authorization.md#service). |
| Deadline/budget/depth | Shared AgentFrame extension; wire schema open | Remaining deadline/budget, default max 5 hops | Receiver lowers ceilings, reports actual usage; caller enforces locally. Other peers' usage unknown, never zero. No hard billing promise. |
| Typed proxy | Later optional `Remote[ResearchAgent]` | Not first multi-agent release | Method syntax could use same structured request; no proprietary RPC. |

```python
from agentframe import AgentDirectory, RemoteAgentClient


async def inspect_before_invocation(
    directory: AgentDirectory, client: RemoteAgentClient,
) -> tuple[str, str]:
    discovered = await directory.get("research-agent")
    return type(discovered.card).__name__, type(client).__name__
```

The caller supplies an entered scoped directory and trusted client. Underlying
client syntax stays open; skills use the decided remote annotation below.

**Behavior / failures:** use native A2A messages/tasks/artifacts and explicit
failure semantics, not automatic skill-as-tool exposure. Models/routers cannot
grant authority. Account for discovery/routing/downstream time and telemetry;
remote services enforce their own limits. No orchestration helpers in the first
multi-agent release: plain `await`, `asyncio.TaskGroup`, and directory search.
Revisit durable workflows after M8+ usage, in an optional separate package if
added; long-running A2A tasks/persistent task stores are not workflow recovery.
No claim of a tested Foundry registry/client integration.

**Related:** [discovery to invocation](../design/discovery.md#agent-injection-and-separate-invocation),
[`SkillRouter`](hosting.md#skillrouter), [limits](../guides/limits.md),
[structured extension and prompt request styles](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary),
[multi-agent release acceptance and deployment direction](../design/discovery.md#phasing-and-open-decisions).

## `Remote[Literal["name"]]`

**Purpose / decision:** inject a binding to another A2A agent into a skill,
looked up by name in the runtime's [`AgentDirectory`](#agentdirectory).
Implemented (#87) as `class Remote[N: str]`; AgentFrame reads the name from
the `Literal`. The string form `Remote["name"]` is not used, because
`mypy --strict` reads a string subscript as a forward reference.

**Signature status:** the annotation, [`ask`](#remoteask), [`call`](#remotecall)
(#88), the `directory=` parameters and service-mode outbound identity
([`remotes=`](authorization.md#service), #92) are implemented; deadline/hop
propagation (#89), token budgets (#90) and Entra on-behalf-of (#93) are later
stories. `Remote[ResearchAgent]` typed proxies are later only.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Name | One string in a `Literal` | Required | The agent's name in the directory; never a caller-supplied URL. Anything else (bare `Remote`, `Remote[str]`, two names) raises `DefinitionError` when the class is defined. |
| Injection | Library-managed binding | New per run | Excluded from caller arguments, the Agent Card and every schema, like `Inject[T]`. A caller sending an argument with the parameter's name is rejected as an unknown argument. Tools cannot take `Remote` parameters. |
| Directory | `Runtime(directory=...)` / `AgentApp(directory=...)` | Required when a skill takes `Remote` | Same rule as `services=`: pass it to `AgentApp` or to its `Runtime`, not both. The application opens and closes it. |
| Card security | The card's `security_schemes`/`security_requirements` | Checked at startup and each run | A card that declares no security is called without credentials. One that declares security needs a `remotes=` entry whose credential satisfies one of its requirements, otherwise `DefinitionError` naming the remote. Foundry A2A agents need [`EntraAppCredential`](authorization.md#entraappcredential) with `https://ai.azure.com/.default`. A source's `card_headers` are used only to fetch cards, never to call agents. |
| Identity mode | [`Service(credential=...)`](authorization.md#service) in `remotes=` (#92); `OnBehalfOf` later (#93) | Per remote; no implicit mode | No token forwarding, authority amplification or stronger-identity fallback. |
| Hop limit (later, #89) | Counter/setting representation open | Maximum 5 remote hops by default | Exceeding depth fails, including recursive loops; no guessed wire field. |

Define a `type` alias per remote to keep signatures short:

```python
from typing import Literal

from agentframe import Agent, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]


@agent(name="Planner", description="Requests research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", name="Plan", description="Research a goal.", tags=["planning"])
    async def plan(self, goal: str, research: Research, ctx: RunContext) -> str:
        return await research.ask("Research " + goal)
```

Host it with a directory the application opened:

```python
from agentframe import AgentApp, AgentDirectory, AllowAnonymous, HostConfig


def planner_app(directory: AgentDirectory) -> AgentApp:
    return AgentApp(
        agent=Planner(),
        # Development placeholder: choose real authentication for a network host.
        host=HostConfig(public_url="http://127.0.0.1:8000", auth=AllowAnonymous()),
        directory=directory,
    )
```

**Behavior / failures:** a skill with a `Remote` parameter and no directory
fails with `DefinitionError` when the `AgentApp` is constructed (locally, at
`Runtime.validate` or the first run). At server startup,
[`Runtime.validate_remotes`](runtime.md#runtimevalidate_remotes) looks up every
name: an unknown name or a card that requires credentials its `remotes=`
entry cannot provide raises `DefinitionError`, a card that cannot be fetched or validated (including an
interface URL on another origin) raises `RemoteAgentError`, and startup fails.
Every run also looks its names up at its start, inside the run's deadline, and
fails the same way before the skill runs. Each run gets a new binding that
knows the run's deadline, principal and invocation ID. Injection is not
delegation authorization; no automatic multi-agent loop or clarification
conversation.

**Related:** [`AgentDirectory`](#agentdirectory), [`Remote.ask`](#remoteask),
[identity](../guides/authorization.md#cross-agent-identity-propagation-m8).

## `Remote.call`

**Purpose / signature:** run one of a remote AgentFrame agent's skills by ID
with typed arguments, through the structured
[skill-call extension](hosting.md#structured-skill-call-wire-format), and
validate its result. Implemented (#88).

```python
@overload
async def call[T](self, skill: str, /, *, output: type[T], **arguments: Any) -> T: ...
@overload
async def call(self, skill: str, /, **arguments: Any) -> Any: ...
```

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `skill` | `str` | Required, positional only | A skill ID listed on the peer's card. |
| `**arguments` | Keyword values | As the remote skill requires | Sent as one JSON object data part in Pydantic JSON mode, so Pydantic models, dates and enums work. The peer validates them. `output` and `max_total_tokens` are reserved and cannot be remote argument names. |
| `output` | A type such as `list[Source]` or `Summary` | Optional | Validates the result with Pydantic's `TypeAdapter`. Without it the result is returned as decoded JSON (numbers come back as `float`), or as `str` for a text result. |
| `max_total_tokens` | `int` | Not implemented yet (#90) | Reserved for a per-call token ceiling; passing it raises `DefinitionError` and sends nothing. |
| Return | `T`, or the JSON value | After the task completes | The skill's result. Untrusted remote data until validated. |

```python
from typing import Literal

from pydantic import BaseModel

from agentframe import Agent, Remote, RunContext, agent, skill

type Research = Remote[Literal["research-agent"]]


class Topic(BaseModel):
    name: str


class Source(BaseModel):
    title: str
    url: str


@agent(name="Planner", description="Plans with research.", version="0.1.0")
class Planner(Agent):
    @skill(id="sources", description="Find sources for a goal.", tags=["planning"])
    async def sources(
        self, goal: str, research: Research, ctx: RunContext
    ) -> list[Source]:
        return await research.call(
            "find_sources", topic=Topic(name=goal), output=list[Source]
        )
```

**Behavior / failures:** before sending, the peer's card must declare the
skill-call extension and list `skill`; otherwise `call` raises
[`RemoteAgentError`](runtime.md#remoteagenterror) naming the missing extension
or skill, sends nothing, and never falls back to free text (use
[`ask`](#remoteask) for agents without the extension). The request activates
the extension with the `A2A-Extensions` header on that request only, names the
skill in message metadata `agentframe.skill` and carries the arguments in one
data part, exactly the M2 wire format. Sending, polling, deadline,
cancellation and question handling are those of [`ask`](#remoteask).

- Completed: the result part is decoded as an AgentFrame host sends it (one
  text part for a `str`, otherwise one data part `{"value": ...}`). Any other
  result raises `RemoteAgentError`.
- The result does not match `output`: raises `OutputValidationError` without
  the remote data or the validation details.
- The peer rejects the call (unknown skill, invalid arguments, permission
  denied): raises `RemoteAgentError` with task state `TASK_STATE_REJECTED`;
  the peer's message is not repeated.
- An argument that cannot be represented as JSON raises Pydantic's
  `PydanticSerializationError` before anything is sent.

Deadline, usage and hop propagation to AgentFrame peers is #89 and #90.

**Related:** [`ClarificationNeededError`](#clarificationneedederror),
[request styles](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary),
[budgets](../guides/limits.md#cross-agent-budgets-and-depth-m8).

## `Remote.ask`

**Purpose / signature:** send plain A2A text to any compatible A2A 1.0 agent,
without AgentFrame's structured skill selection. Implemented (#87).

```python
async def ask(self, text: str) -> str: ...
```

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `text` | `str` | Required | Sent as one native text part. Not identity or trusted instructions for the peer. |
| Return | `str` | After the task completes | The text parts of the task's artifacts (or of its final message when it has none), joined by newlines. Untrusted remote content. |

```python
from typing import Literal

from agentframe import Remote

type Research = Remote[Literal["research-agent"]]


async def notes(research: Research, goal: str) -> str:
    return await research.ask("Summarize recent findings on " + goal)
```

**Behavior / failures:** sends `SendMessage` with the official SDK's JSON-RPC
client transport, using the directory's HTTP client, only to the card's
validated interface URL (same origin as the registry's card URL). The request
asks to return immediately, so the remote task ID is known at once; an
unfinished task is then polled with `GetTask` (after 0.25 s, 0.5 s, then every
1 s). Every request carries `A2A-Version: 1.0`, the remote's configured
credential headers (if any; see [`Service`](authorization.md#service)) and a
timeout of the run's remaining time; headers go per request, never into the
shared client's defaults, and redirects are never followed.

- Completed: the joined text. A result with a non-text part raises
  `RemoteAgentError`.
- Input required: raises [`ClarificationNeededError`](#clarificationneedederror)
  with the question. Nothing answers it automatically.
- The configured credential fails to produce its headers: raises
  `AuthenticationError` before anything is sent.
- Failed, rejected, cancelled or auth required, a task that disappears, or an
  unreachable, refusing or redirecting peer: raises
  [`RemoteAgentError`](runtime.md#remoteagenterror) with the remote task ID and
  state when known. The remote status text is never put in the message.
- The run's deadline passes: the remote task gets a best-effort `CancelTask`,
  then the run raises `TimeoutLimitExceededError`.
- The run is cancelled (hosted `CancelTask`, shutdown or local cancellation):
  the remote task gets a best-effort `CancelTask` and the cancellation goes on.
  If the peer has not replied with the task ID yet, the send finishes in the
  background (up to 2 seconds) to learn it. The cancel request is bounded to
  2 seconds and is still sent if the run is cancelled again.

An uncaught `ClarificationNeededError` in a hosted skill fails the
orchestrator's own task (`FAILED`) with its safe message; it is not turned into
the orchestrator's input-required state, and there is no resume. Deadline,
usage and hop propagation to AgentFrame peers is #89; there is no implicit
structured fallback.

**Related:** [`Remote.call`](#remotecall), [request styles](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary).

## `ClarificationNeededError`

**Purpose / decision:** surface a remote input-required question to application
code, without automated back-and-forth or durable workflow resumption.
Implemented (#87) as an `AgentFrameError` subclass.

| Field | Type | Meaning and constraints |
| --- | --- | --- |
| `question` | `str` | The remote agent's question (its text parts, joined by newlines). Untrusted remote content: check it before showing it to anyone. |
| `task_id` | `str` | The remote task waiting for the answer. |
| `context_id` | `str` | The remote task's context ID. |
| `message` | `str` | Safe message naming the agent; never contains the question. |
| `invocation_id` | `str \| None` | The run that asked the remote agent. |

```python
from agentframe import ClarificationNeededError


def clarification_reference(error: ClarificationNeededError) -> tuple[str, str]:
    return error.task_id, error.question
```

**Behavior / failures:** [`ask`](#remoteask) raises it on native
input-required; the application decides what to do next. The remote task is
left waiting, not cancelled. An API to answer it is deferred.

**Related:** [`AgentFrameError`](runtime.md#agentframeerror),
[remote calls](../design/discovery.md#planned-remote-calls-and-cross-agent-context).
