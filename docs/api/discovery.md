# Registries and managed discovery

[Reference status conventions](README.md). The registry entries
([`AgentRegistry`](#agentregistry) through [`FileAgentRegistry`](#fileagentregistry))
are implemented (M8+ #83), and [`AgentDirectory`](#agentdirectory) with
[`get`](#agentdirectoryget), [`refresh`](#agentdirectoryrefresh), its async
context manager and [`DiscoveredAgent`](#discoveredagent) (#84); every other API
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
[`list`](#agentregistrylist); no write or lifecycle methods exist.

| Contract | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `get` implementation | `async (agent_id: str) -> AgentReference` | Required abstract method | Return one agent's reference; raise when the source fails or does not list it. Do not fetch or cache cards. |
| `list` implementation | `async (query: AgentQuery) -> list[AgentReference]` | Required abstract method | Return matching references; empty only when the source authoritatively lists none. |

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
Pagination for large sources (such as Foundry) remains open.

**Related:** [`AgentQuery`](#agentquery),
[query placement](../design/discovery.md#query-matching-and-card-enrichment).

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
grant access. Ordering and pagination for paginated sources remain open.

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

**Purpose / decision:** read references through the chosen Foundry project agents
API using official `azure-ai-projects` 2.x. Source selection is confirmed;
the M8+ adapter is not implemented or compatibility-tested.

**Decided constructor fields:** `FoundryAgentRegistry(project_endpoint=..., credential=...)`.
Project API/SDK family is selected; exact compatible dependency pins await
implementation (2.10.0 observed on 2026-11-11).

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `project_endpoint` | `str` | Required | Trusted configured Foundry project endpoint, not model inference URL. |
| `credential` | Injected Entra credential; exact accepted type open | Required | Registry-read access verified separately; never load credentials from source references. |
| API/source selection | Foundry project agents API, `azure-ai-projects` 2.x | Chosen; observed 2.10.0 is not a pin | Project endpoint `https://{account}.services.ai.azure.com/api/projects/{project}`. Model deployments are not agent endpoints; not `FoundryModel` inference. |
| A2A eligibility/card mapping | Listed agent to validated reference/native card; mapping open | Only A2A-enabled agents eligible | Foundry prompt agents can expose incoming A2A with `agent_card` plus protocol configuration. Target GA 1.0, not 0.3 preview; prove mapping to the documented v1.0 card route. |
| Authentication/RBAC | Application-injected Entra identity; exact interface open | Required for protected project/A2A access | Foundry User or higher enables incoming A2A; endpoint callers need Foundry Agent Consumer or another endpoint-access role. Verify registry-read permissions separately. |
| Pagination | SDK page/source to registry query/results; contract open | Required for correct enumeration | Preserve explicit failures and result completeness; do not silently return a first page as all agents. |

```python
from azure.core.credentials_async import AsyncTokenCredential
from agentframe import FoundryAgentRegistry


def foundry_source(
    project_endpoint: str, credential: AsyncTokenCredential,
) -> FoundryAgentRegistry:
    return FoundryAgentRegistry(
        project_endpoint=project_endpoint, credential=credential,
    )
```

**Behavior / failures:** the credential type above is illustrative, not finalized
adapter typing. This is not implemented support or an
executable setup guide. Foundry's
[incoming A2A guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
documents published cards and incoming requests for enabled prompt agents.
Its v1.0 card route ends in
`/agents/{agent}/endpoint/protocols/a2a/agentCard/v1.0`; resolve each eligible
listed agent's endpoint rather than assuming every project agent exposes A2A.
Use Entra ID for card/invocation access, not model API-key credentials.
Unsupported API/non-invocable endpoints, source
outages and auth failures must be explicit, not disguised as working A2A
references or empty results. No deployment/provisioning or A2A-enabling writes.
Foundry's [outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)
are a possible additional reference source, not a chosen contract.
See [observed SDK metadata](https://pypi.org/project/azure-ai-projects/2.10.0/);
this does not mean AgentFrame agents deploy to Foundry.

**Related:** [chosen source and mapping boundaries](../design/discovery.md#sources-and-reference-format),
[`FoundryModel`](models.md#foundrymodel).

## `AgentDirectory`

**Purpose / decision:** one directory that combines agent sources and returns
validated native Agent Cards, so applications never write card-fetching,
caching or refresh code. Implemented (M8+ #84); [`find`](#agentdirectoryfind)
arrives in #85. The card fetcher and cache are internal: there are no public
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
| `http_client` | `httpx.AsyncClient \| None` | Optional; default `None` | Caller-owned client used for card fetches (and, later, remote calls); never closed by the directory. It must not carry credentials in default headers or cookies. When omitted, the directory creates a client on `async with` and closes it on exit. Also lets tests use in-process ASGI apps. |

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
- carries no credentials: the client's `auth` is disabled and nothing about the
  current caller is added, so every caller sees the same cached card (one card
  variant; per-caller or authenticated extended cards are out of scope);
- is bounded to 10 seconds (and never past the caller's deadline, since a run's
  deadline cancels it), reads at most 1 MiB and does not follow redirects;
- is parsed by the official SDK into `a2a.types.AgentCard`, which must have a
  name and a JSON-RPC A2A 1.0 interface whose URL has the same origin (scheme,
  host and port) as the registry's card URL. The registry's `card_url` is the
  only trusted origin; later outbound credentials (#92, #93) go only there.

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
it. `AgentApp(directory=...)` and startup validation of `Remote` bindings are
#87.

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
| `interface_url` | `str` | The card's JSON-RPC A2A 1.0 interface URL, always on the card URL's origin. Remote calls (#87) send only here. |
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

**Purpose / decision:** dynamically find matching native Agent Cards using
validated card metadata, not assumed skill/tag fields on source references.

**Decided calls:** `await directory.find(skill="find_sources")` or
`await directory.find(tags=["research"])`. Exact collection/generic typing,
combined-filter semantics and pagination remain open.

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `skill` | `str` | Optional named filter | Match an advertised skill ID after native card resolution/validation. |
| `tags` | `list[str]` in decided example | Optional named filter | Match advertised skill tags; all/any/combined matching still open. |
| Result | Matching native `AgentCard` collection | On success | Eligible cards, not invocation permission or Python methods; pagination/completeness contract open. |

```python
from agentframe import AgentDirectory


async def research_count(directory: AgentDirectory) -> int:
    cards = await directory.find(tags=["research"])
    return len(cards)
```

**Behavior / failures:** bound source reads/card enrichment, isolate caller
variants, automatically refresh stale cards. Failed refresh fails unless stale
use is explicit; never silently omit failed sources as empty matches. No
automatic remote invocation. Source conflict/result ordering remain open.

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
The decided authoring surface is [`Remote["name"]`](#remotename), with
[`call`](#remotecall) and [`ask`](#remoteask), not an invented constructor here.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Discovered destination/card | Validated reference/native card; client interface open | Needed for selected interaction | Re-check destinations, trust, binding and current authorization, not just cache freshness. |
| Outbound identity | Per-remote `on_behalf_of` / `service`; configuration shape open | OBO default for Entra ingress; service explicit | Exchange target-scoped tokens; never forward incoming token or fall back on exchange failure. API-key/anonymous ingress supports only service. |
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

## `Remote["name"]`

**Purpose / decision:** inject a managed remote-agent binding into a skill,
resolved by configured name through the app's directory.

**Signature status:** string-name annotation and `call` / `ask` are decided;
internal proxy typing, explicit-client construction and per-remote config
carrier remain open. `Remote[ResearchAgent]` typed proxies are later only.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Name | String literal | Required | Validated against configured directory names at startup; not caller-supplied URL. |
| Injection | Library-managed binding | Per managed invocation | Excluded from caller/card/MCP schemas, like `Inject[T]`; preserves caller policy/deadline/ledger. |
| Identity mode | `on_behalf_of` or `service`; config carrier open | OBO for Entra caller, service explicit otherwise | No token forwarding, authority amplification or stronger-identity fallback. |
| Hop limit | Counter/setting representation open | Maximum 5 remote hops by default | Exceeding depth fails, including recursive loops; no guessed wire field. |

```python
from agentframe import Agent, Remote, RunContext, agent, skill


@agent(name="Planner", description="Requests research.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", name="Plan", description="Research a goal.", tags=["planning"])
    async def plan(
        self, goal: str, research: Remote["research-agent"], ctx: RunContext,
    ) -> str:
        return await research.ask("Research " + goal)
```

**Behavior / failures:** unresolved configured name fails startup; refresh,
authentication, permission, deadline and usage failures remain explicit.
Injection is not delegation authorization. No automatic multi-agent loop or
clarification conversation.

**Related:** [`AgentDirectory`](#agentdirectory),
[identity](../guides/authorization.md#cross-agent-identity-propagation-m8).

## `Remote.call`

**Purpose / decision:** select a remote skill with a structured A2A extension
request and optionally validate its typed result.

**Decided call:** `await research.call("find_sources", topic=goal,
output=list[Source], max_total_tokens=20_000)`. Exact overloads and reserved-name
collision handling remain open, not new business-argument envelope syntax.

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Skill ID | `str` | Required positional | Card-declared/caller-activated structured extension; no prompt fallback. |
| Business arguments | Typed keyword values | Selected signature determines requirements | Native data-part JSON; validate remotely, no injected arguments. |
| `output` | Python/Pydantic contract such as `list[Source]` | Optional validation target; omitted result shape open | Validate returned data, not prompt instructions or fabricated output. |
| `max_total_tokens` | `int` | Caller remaining budget by default | Per-call ceiling; receiver can only lower, shared reservations prevent double spend. |
| Return | Validated requested output when specified | After task completion | Within remaining caller deadline; native result/part mapping still open. |

```python
from pydantic import BaseModel
from agentframe import Remote


class Source(BaseModel):
    title: str
    url: str


async def sources(research: Remote["research-agent"], goal: str) -> list[Source]:
    return await research.call(
        "find_sources", topic=goal, output=list[Source], max_total_tokens=20_000,
    )
```

Use the already-managed skill binding, not a manually constructed annotation.

**Behavior / failures:** await task completion, stop waiting at caller deadline,
raise `ClarificationNeededError` on input-required without auto-answering.
Unsupported extension/unknown skill/invalid input/output fail explicitly.
Send deadline/token/depth via the shared extension, reconcile AgentFrame usage
once, preserve other-agent usage as unknown. Cancellation is not rollback.

**Related:** [`ClarificationNeededError`](#clarificationneedederror),
[budgets](../guides/limits.md#cross-agent-budgets-and-depth-m8).

## `Remote.ask`

**Purpose / decision:** send plain A2A text to any compatible A2A agent,
without requiring AgentFrame structured selection.

**Decided call:** `await research.ask(text)`; text result mapping is required,
exact non-text response handling/extra options remain open.

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Text | `str` | Required | Untrusted prompt, not identity or trusted instructions. |
| Return | Text (`str`) | After task completion | Subject to output policy; non-text mappings remain open. |
| Deadline/usage | Shared invocation controls | Automatic | Caller enforces remaining time; AgentFrame extension when supported, otherwise usage unknown. |

```python
from agentframe import Remote


async def notes(research: Remote["research-agent"], goal: str) -> str:
    return await research.ask("Summarize recent findings on " + goal)
```

**Behavior / failures:** uses native messages/tasks and waits within the caller
deadline. Input-required raises `ClarificationNeededError`; never auto-converse.
Identity exchange failure fails even for extension-unaware targets; plain text
is not permission to forward a bearer token. No implicit structured fallback.

**Related:** [`Remote.call`](#remotecall), [request styles](../design/mcp-skills.md#a2a-request-styles-and-extension-boundary).

## `ClarificationNeededError`

**Purpose / decision:** surface a remote input-required question to application
code, without automated back-and-forth or durable workflow resumption.

**Signature status:** `AgentFrameError` subtype with question, invocation ID and
safe message; constructor/question accessor and task-reference fields remain open.

| Field | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Question | Remote question; exact representation/accessor open | Required | Untrusted remote content, policy-filtered before user display. |
| Inherited error contract | Invocation ID, safe message | Required | No private provider payloads or cross-caller task disclosure. |

```python
from agentframe import ClarificationNeededError


def clarification_correlation(error: ClarificationNeededError) -> object:
    return error.invocation_id
```

**Behavior / failures:** `call` and `ask` stop on native input-required.
Application decides subsequent action; native state is not rewritten as remote
failure merely because the local client raises. Follow-up/task-ID API remains open.

**Related:** [`AgentFrameError`](runtime.md#agentframeerror),
[remote calls](../design/discovery.md#planned-remote-calls-and-cross-agent-context).
