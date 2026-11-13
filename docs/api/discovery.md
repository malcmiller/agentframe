# Registries and managed discovery

[Reference status conventions](README.md). The registry entries
([`AgentRegistry`](#agentregistry) through [`FileAgentRegistry`](#fileagentregistry))
are implemented (M8+ #83); every other API here is unimplemented, and its
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

## `AgentCardResolver`

**Purpose / decision:** library-owned fetch/validation machinery for native
official SDK Agent Cards, separate from read adapters.

**Proposed constructor shape:**

```text
AgentCardResolver(*, authentication = ..., destination_policy = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `authentication` | Application outbound-authentication dependency; interface open | Explicit in example; omission/public-card behavior open | Select appropriate trusted credentials/card variant; never load credentials from references. |
| `destination_policy` | Application destination/redirect policy; interface open | Explicit policy required for safe resolution; exact integration open | Approved schemes/addresses/origins, redirects and credential forwarding; applies to fetched and card-advertised URLs. |

```python
from agentframe import AgentCardResolver


def resolver(
    outbound_authentication: object, destination_policy: object,
) -> AgentCardResolver:
    return AgentCardResolver(
        authentication=outbound_authentication,
        destination_policy=destination_policy,
    )
```

`object` marks unresolved application interfaces, not arbitrary-object support.

**Behavior / failures:** no construction I/O. Directory drives bounded fetches,
HTTP deadlines/cancellation and version-compatible native card validation.
Reject unsafe destinations/redirects, malformed cards and unsupported bindings;
auth/network/timeout failures remain explicit. URL/schema validation alone is
not SSRF prevention or endpoint trust. No resolver method signature is settled;
users need not implement card HTTP loops for each backend.

**Related:** [trust boundaries](../design/discovery.md#failure-trust-and-isolation-boundaries),
[`AgentDirectory`](#agentdirectory).

## `CardCacheConfig`

**Purpose / direction:** explicit card freshness/stale-use settings, not health
checking or authorization.

**Constructor shape with decided defaults; exact numeric typing remains open:**

```text
CardCacheConfig(*, ttl_seconds: ... = 300, allow_stale: bool = False)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `ttl_seconds` | Finite numeric seconds; exact type/range open | `300`, configurable | Automatically refresh on stale access; not background polling or source reconciliation. Zero semantics open. |
| `allow_stale` | `bool` | `False`; stale use requires explicit `True` | Failed refresh fails the call otherwise; label stale results and failure. Cannot bypass authorization/removal. Stale-age bounds open. |

```python
from agentframe import CardCacheConfig


cache = CardCacheConfig(ttl_seconds=300, allow_stale=False)
```

**Behavior / failures:** reject invalid settings. Never reset freshness on
failed refresh or silently label stale as current. Isolate caller/tenant/auth/
card variants and source/reference versions, including coalesced fetches.
Cache freshness is neither health, trust nor current permission. Source refresh
and removal semantics cannot be inferred from card TTL alone.

**Related:** [freshness lifecycle](../design/discovery.md#managed-directory-lifecycle),
[`AgentDirectory.refresh`](#agentdirectoryrefresh).

## `AgentDirectory`

**Purpose / decision:** library-managed facade combining registry, resolver,
cache, automatic on-stale refresh, explicit refresh and discovery-resource lifecycle.

**Decided `sources` composition; resolver/cache extension details remain illustrative:**

```text
AgentDirectory(*, sources: list[AgentRegistry], resolver: AgentCardResolver = ..., cache: CardCacheConfig = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `sources` | Collection of `AgentRegistry`; exact container typing open | Required | Explicit file/Foundry/environment/custom sources; conflict/order rules remain open. |
| `resolver` | `AgentCardResolver` | Explicit in shape; default unresolved | Library machinery configured with application outbound auth/destination policy. |
| `cache` | `CardCacheConfig` | 300-second TTL, stale use off by default | Freshness/stale policy plus required isolation; never overrides caller policy. |

```python
from azure.core.credentials_async import AsyncTokenCredential
from agentframe import (
    AgentCardResolver, AgentDirectory, CardCacheConfig, FileAgentRegistry,
    FoundryAgentRegistry,
)


def build_directory(
    project_endpoint: str, credential: AsyncTokenCredential,
    outbound_authentication: object, destination_policy: object,
) -> AgentDirectory:
    return AgentDirectory(
        sources=[
            FileAgentRegistry("agents.yaml"),
            FoundryAgentRegistry(
                project_endpoint=project_endpoint, credential=credential,
            ),
        ],
        resolver=AgentCardResolver(
            authentication=outbound_authentication,
            destination_policy=destination_policy,
        ),
        cache=CardCacheConfig(ttl_seconds=300, allow_stale=False),
    )
```

Application dependencies are placeholders with interfaces still open.

**Behavior / failures:** no I/O/polling during construction/import. Use
`async with directory` for ownership-aware resource lifecycle. Background
refresh is opt-in; its API remains open. Caller-owned injected directories and
clients remain caller-owned unless explicitly transferred; a host may manage
library-created/transferred directory lifespan via `AgentApp(directory=...)`.
Configured `Remote["name"]` references are validated at startup.
Register a shared directory with `Services.add(AgentDirectory, directory)` and
inject `Inject[AgentDirectory]` into skills. Applications own/close singletons;
only an intentionally scoped factory's AgentFrame-created instance is closed
per run. Injection alone never transfers ownership of a singleton.
No deploy/start/stop/delete, registration writes, leases/federation, automatic
delegation, or A2A task lifecycle. Source errors are not removal evidence;
reconciled removals stop new eligibility, not active tasks. TTL can delay
observing removal; no instant revocation guarantee.

**Related:** [`get`](#agentdirectoryget), [`find`](#agentdirectoryfind), [`refresh`](#agentdirectoryrefresh),
[dependency injection](../design/discovery.md#agent-injection-and-separate-invocation).

## `AgentDirectory.get`

**Purpose / proposed shape:** `await directory.get(agent_id: str)`.
Returns a discovered result with `.card` plus reference/freshness metadata;
exact result class, fields and contextual arguments are open.

| Parameter/result | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent_id` | `str` | Required in example | Resolve eligible reference under trusted source/caller policy. |
| Result `.card` | Native official SDK `AgentCard` | Present on successful illustrated result | Validated card, not permission/health proof. |
| Reference/freshness metadata | Result representation open | Must accompany discovery | Distinguish source/card freshness and permitted stale state without inventing timestamps/field names. |

```python
from agentframe import AgentDirectory


async def inspect(directory: AgentDirectory) -> str:
    discovered = await directory.get("research-agent")
    return type(discovered.card).__name__
```

The caller supplies an entered, appropriately scoped directory.

**Behavior / failures:** fetch/validate or use fresh cache within source
reconciliation and caller policy. Missing ID, source failure, malformed card,
denied destination/auth, deadline and cancellation are explicit failures with
names open. Failed refresh cannot silently return stale as current; explicitly
permitted stale use must be labeled. Restrict eligible candidates and authorize
again at invocation/action. Retrieval does not invoke/delegate remotely.

**Related:** [`CardCacheConfig`](#cardcacheconfig),
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

**Purpose / signature status:** explicit refresh is proposed; exact arguments,
per-ID versus source-wide scope, force semantics, and return type remain open.
No finalized `refresh(agent_id)` or zero-argument overload is claimed.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Refresh scope/source reconciliation | Interface open | Must be specified before stabilization | Distinguish source read, card refetch and removal handling; source failure is not an authoritative empty snapshot. |
| Result/failure metadata | Interface open | Explicit freshness/failure information needed | Never advance freshness on failure or silently authorize stale use. |

```python
from agentframe import AgentDirectory


def describe_refresh_owner(directory: AgentDirectory) -> str:
    return type(directory).__name__
```

This dependency-only example intentionally avoids fabricating a refresh call
before scope/signature design.

**Behavior / failures:** bound refresh work and isolate card variants. Successful
reconciliation invalidates removed/replaced eligibility/cache entries for new
selection; active tasks are not automatically canceled. Surface outages,
malformed data, auth/destination denials and refresh failures explicitly.
Background refresh is not implicitly enabled by this operation.

**Related:** [managed lifecycle](../design/discovery.md#managed-directory-lifecycle),
[`AgentDirectory.get`](#agentdirectoryget).

## `AgentDirectory.__aenter__`

**Purpose / signature status:** proposed async context-manager entry used by
`async with directory`; return typing and acquisition timing remain open.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Context entry | Python async context-manager protocol | No user arguments | Establish ownership-scoped lifecycle; no implicit polling/background-refresh grant. |

```python
from agentframe import AgentDirectory


async def inspect_scoped(directory: AgentDirectory) -> str:
    async with directory:
        discovered = await directory.get("research-agent")
        return type(discovered.card).__name__
```

Here the application delegates the lifecycle scope to this function.

**Behavior / failures:** resource acquisition failures are explicit; no
construction-time I/O is implied. Reentrancy/reuse and partial-acquisition
cleanup need specification. Context entry does not grant discovery eligibility
or transfer ownership of every injected dependency.

**Related:** [`AgentDirectory`](#agentdirectory),
[ownership guide](../design/discovery.md#managed-directory-lifecycle).

## `AgentDirectory.__aexit__`

**Purpose / signature status:** proposed async context-manager exit; Python
exception-context arguments follow the protocol, final annotations remain open.

| Parameter/concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Exception context | Python context-manager protocol values | Supplied by Python | Do not turn failed discovery/cancellation into success through suppression. |
| Owned resources | Library-created or explicitly transferred resources | Cleanup required | Do not close caller-owned clients/dependencies merely because they were injected. |

```python
from agentframe import AgentDirectory


async def read_and_close(directory: AgentDirectory) -> None:
    async with directory:
        await directory.get("research-agent")
```

The caller deliberately assigns lifecycle scope to this function.

**Behavior / failures:** close owned resources, including opt-in background work
if configured, without automatically canceling remote A2A tasks. Cancellation/
cleanup-error precedence and shutdown bounds remain open; failures must remain
explicit rather than silently swallowed.

**Related:** [`AgentDirectory.__aenter__`](#agentdirectory__aenter__),
[discovery lifecycle scope](../design/discovery.md#phasing-and-open-decisions).

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
