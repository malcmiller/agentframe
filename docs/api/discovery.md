# Registries and managed discovery

[Reference status conventions](README.md). Every import and signature here is
proposed and unimplemented. Discovery/registry and remote A2A invocation are core
scope for the next multi-agent release (M8+), after the M7 single-agent release,
not optional add-ons. Orchestrators are ordinary AgentFrame skills that call
independently deployed specialists over A2A, not a workflow/graph engine.
A2A is not a registry protocol.
See the [design and lifecycle guide](../discovery.md) for composition examples.

## `AgentRegistry`

**Purpose / decision:** small abstract base users extend for source reads.
Unlike optional structural dependencies elsewhere, this extension point
explicitly uses inheritance. It is not a global plugin registry.

**Signature status:** no base constructor parameters settled. Implement async
[`get`](#agentregistryget) and [`list`](#agentregistrylist); no write or lifecycle
methods are required by the read base.

| Contract | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `get` implementation | Async ID lookup returning `AgentReference` | Required abstract operation | Read validated source reference; do not fetch/cache cards per backend. |
| `list` implementation | Async query returning `list[AgentReference]` in initial shape | Required abstract operation | Supported filtering; pagination/completeness unresolved. |

```python
from collections.abc import Awaitable, Callable
from agentframe import AgentQuery, AgentReference, AgentRegistry


class ApplicationAgentRegistry(AgentRegistry):
    def __init__(
        self,
        read_one: Callable[[str], Awaitable[AgentReference]],
        read_matching: Callable[[AgentQuery], Awaitable[list[AgentReference]]],
    ) -> None:
        self._read_one = read_one
        self._read_matching = read_matching

    async def get(self, agent_id: str) -> AgentReference:
        return await self._read_one(agent_id)

    async def list(self, query: AgentQuery) -> list[AgentReference]:
        return await self._read_matching(query)
```

The supplied source functions validate references, implement supported filters,
and raise on source/unsupported-query failures. This example does not prescribe
application constructor parameters for all registries.

**Behavior / failures:** no I/O at import/construction. Missing source,
malformed references and unsupported queries are explicit failures, not empty
results. Exact abstract-base implementation and errors remain open. No required
`register`, deployment, HTTP, polling, or card-refresh loop.

**Related:** [extension guide](../discovery.md#extending-the-read-base),
[`AgentDirectory`](#agentdirectory).

## `AgentRegistry.get`

**Purpose / proposed signature:** read one reference by stable source identity.

```python
async def get(self, agent_id: str) -> AgentReference: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `agent_id` | `str` | Required | Validated stable registry ID, not arbitrary caller-supplied URL or proof of authorization. |

```python
from agentframe import AgentReference, AgentRegistry


async def lookup(registry: AgentRegistry) -> AgentReference:
    return await registry.get("research-agent")
```

**Behavior / failures:** return a validated reference, not a card or invocation.
Distinguish confirmed absence from read outage/invalid data; missing-ID exception
type remains open. ID format, namespaces and source consistency need design.

**Related:** [`AgentReference`](#agentreference),
[failure boundaries](../discovery.md#failure-trust-and-isolation-boundaries).

## `AgentRegistry.list`

**Purpose / proposed signature:** read source references matching supported
query criteria.

```python
async def list(self, query: AgentQuery) -> list[AgentReference]: ...
```

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `query` | `AgentQuery` | Required in illustrative signature | Only supported filters with explicit matching/enrichment placement. Pagination, ordering, limits and result completeness unresolved before stabilization. |

```python
from agentframe import AgentQuery, AgentReference, AgentRegistry


async def candidates(
    registry: AgentRegistry, query: AgentQuery,
) -> list[AgentReference]:
    return await registry.list(query)
```

**Behavior / failures:** an authoritative empty match is valid; source failure
is not. Reject unsupported filters rather than silently ignoring them. Do not
query skill/tag fields absent from references without an explicit source
metadata/enrichment contract; matching is not access permission.

**Related:** [`AgentQuery`](#agentquery),
[query placement](../discovery.md#query-matching-and-card-enrichment).

## `AgentReference`

**Purpose / decision:** validated stable identity and non-secret connection/
discovery information returned by a registry.

**Signature status:** constructor and canonical fields open. Candidate JSON
names below are illustrative source-format keys, not finalized Python keywords.

| Information | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Stable identity (`agent_id` candidate) | `str`; validation rules open | Identity required | Registry namespace/uniqueness rules need specification; not authorization. |
| Endpoint/discovery location (`discovery_url` candidate) | Validated location representation open | Needed to resolve/connect; alternatives open | Explicit card location in candidate format; validate destinations and card-advertised endpoints. |
| Transport (`transport` candidate) | Supported transport identifier; type/values open | Transport information needed; encoding open | Must match verified client/binding support, not imply every endpoint supports A2A. |
| Non-secret metadata (`connection_metadata` candidate) | Restricted structured metadata; schema open | Optional proposed information | No credential/token/private key, executable configuration, or implicit permission grant. |

```python
from agentframe import AgentReference


def describe_reference(reference: AgentReference) -> str:
    return type(reference).__name__
```

**Behavior / failures:** reject malformed/duplicate identities, invalid
locations, unsupported transports and secret-bearing configuration according
to the eventual validation contract. A schema-valid reference is not trusted,
healthy, authorized, or guaranteed invocable. Skills/tags are not assumed
members of this minimal type.

**Related:** [candidate source format](../discovery.md#sources-and-reference-format),
[`AgentCardResolver`](#agentcardresolver).

## `AgentQuery`

**Purpose / decision:** express supported discovery filters, distinct from
caller eligibility and invocation authorization.

**Signature status:** constructor/field names and matching semantics open;
no definite `ids=`, `skills=` or `tags=` constructor is promised.

| Filter concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Agent IDs | Collection of stable IDs; representation open | Optional supported criterion; default open | Can match validated reference identity. |
| Skills/tags | Collection of identifiers/tags; representation open | Only supported where metadata exists; default open | Requires source metadata or explicit card enrichment; layer and all/any semantics unresolved. |

```python
from agentframe import AgentQuery, AgentRegistry


async def count_matches(registry: AgentRegistry, query: AgentQuery) -> int:
    return len(await registry.list(query))
```

This counts only returned matches, not a guaranteed global count; pagination
has not been settled.

**Behavior / failures:** reject unsupported filters; do not silently broaden
queries. Query matching cannot grant access. Ordering, pagination, bounds and
directory-level search API remain open.

**Related:** [`AgentRegistry.list`](#agentregistrylist),
[enrichment boundary](../discovery.md#query-matching-and-card-enrichment).

## `EnvironmentAgentRegistry`

**Purpose / direction:** supplied read adapter for an explicitly selected
environment variable, sharing structured validation with the file adapter.

**Proposed illustrative shape:** `EnvironmentAgentRegistry(variable=...)`;
keyword name and format are not stabilized.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `variable` | `str` in proposed shape | Explicit variable selection; no implicit search | Names a variable containing candidate JSON reference-array text, not a file path or secret credential. |

```python
from agentframe import EnvironmentAgentRegistry


registry = EnvironmentAgentRegistry(variable="AGENTFRAME_AGENTS")
```

**Behavior / failures:** inherits async `get`/`list`; construction does not read
the variable. Document the selected JSON schema/version before implementation.
Missing variable, malformed JSON/references, duplicate IDs and unsupported
queries fail explicitly. Empty authoritative source differs from missing or
failed source. No card fetching or background polling in this adapter.

**Related:** [candidate JSON format](../discovery.md#sources-and-reference-format),
[`AgentRegistry`](#agentregistry).

## `FileAgentRegistry`

**Purpose / direction:** supplied read adapter for explicitly selected structured
text, preferably JSON initially.

**Proposed shape:** `FileAgentRegistry(path="agents.json")`.

| Parameter | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| `path` | `str` in example; path-like support open | Explicit path required in shape | Trusted source selection; candidate JSON reference array. No automatic file search or arbitrary executable deserialization. |

```python
from agentframe import FileAgentRegistry


registry = FileAgentRegistry(path="agents.json")
```

**Behavior / failures:** inherits async `get`/`list`; no construction-time file
read. Missing/unreadable file, malformed/oversized data, duplicate IDs and
unsupported queries fail, not return empty matches. Bounded reads, source
reconciliation and concurrent-update consistency require specification.
The example does not create `agents.json` or finalize its schema.

**Related:** [source format](../discovery.md#sources-and-reference-format),
[`AgentDirectory`](#agentdirectory).

## `FoundryAgentRegistry`

**Purpose / decision:** read references through the chosen Foundry project agents
API using official `azure-ai-projects` 2.x. Source selection is confirmed;
the M8+ adapter is not implemented or compatibility-tested.

**Signature status:** no public constructor or credential fields settled.
Project API/SDK family is selected; exact compatible dependency pins await
implementation (2.10.0 observed on 2026-11-11).

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| API/source selection | Foundry project agents API, `azure-ai-projects` 2.x | Chosen; observed 2.10.0 is not a pin | Project endpoint `https://{account}.services.ai.azure.com/api/projects/{project}`. Model deployments are not agent endpoints; not `FoundryModel` inference. |
| A2A eligibility/card mapping | Listed agent to validated reference/native card; mapping open | Only A2A-enabled agents eligible | Foundry prompt agents can expose incoming A2A with `agent_card` plus protocol configuration. Target GA 1.0, not 0.3 preview; prove mapping to the documented v1.0 card route. |
| Authentication/RBAC | Application-injected Entra identity; exact interface open | Required for protected project/A2A access | Foundry User or higher enables incoming A2A; endpoint callers need Foundry Agent Consumer or another endpoint-access role. Verify registry-read permissions separately; no invented credential constructor keywords. |
| Pagination | SDK page/source to registry query/results; contract open | Required for correct enumeration | Preserve explicit failures and result completeness; do not silently return a first page as all agents. |

```python
from agentframe import FoundryAgentRegistry


def describe_foundry_source(registry: FoundryAgentRegistry) -> str:
    return type(registry).__name__
```

**Behavior / failures:** this typed placeholder is not implemented support or an
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

**Related:** [chosen source and mapping boundaries](../discovery.md#sources-and-reference-format),
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

**Related:** [trust boundaries](../discovery.md#failure-trust-and-isolation-boundaries),
[`AgentDirectory`](#agentdirectory).

## `CardCacheConfig`

**Purpose / direction:** explicit card freshness/stale-use settings, not health
checking or authorization.

**Illustrative constructor shape:**

```text
CardCacheConfig(*, ttl_seconds: ... = ..., allow_stale: bool = ...)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `ttl_seconds` | Finite numeric seconds; exact type/range open | `300` is an example, not a settled default | Fresh-card reuse bound, distinct from source-reference reconciliation/eligibility. Zero semantics open. |
| `allow_stale` | `bool` | `False` is an example, not a settled default | Only explicit policy may allow labeled stale results; stale-age bound and refresh-failure representation open. Cannot bypass authorization/removal. |

```python
from agentframe import CardCacheConfig


cache = CardCacheConfig(ttl_seconds=300, allow_stale=False)
```

**Behavior / failures:** reject invalid settings. Never reset freshness on
failed refresh or silently label stale as current. Isolate caller/tenant/auth/
card variants and source/reference versions, including coalesced fetches.
Cache freshness is neither health, trust nor current permission. Source refresh
and removal semantics cannot be inferred from card TTL alone.

**Related:** [freshness lifecycle](../discovery.md#managed-directory-lifecycle),
[`AgentDirectory.refresh`](#agentdirectoryrefresh).

## `AgentDirectory`

**Purpose / decision:** library-managed facade combining registry, resolver,
cache, explicit refresh and discovery-resource lifecycle.

**Proposed constructor shape:**

```text
AgentDirectory(*, registry: AgentRegistry, resolver: AgentCardResolver, cache: CardCacheConfig)
```

| Parameter | Type | Required/default status | Meaning, constraints, precedence |
| --- | --- | --- | --- |
| `registry` | `AgentRegistry` | Required in shape | Trusted reference source, not a global registry or permission grant. |
| `resolver` | `AgentCardResolver` | Explicit in shape; default unresolved | Library machinery configured with application outbound auth/destination policy. |
| `cache` | `CardCacheConfig` | Explicit in shape; defaults unresolved | Freshness/stale policy plus required isolation; never overrides caller policy. |

```python
from agentframe import (
    AgentCardResolver, AgentDirectory, CardCacheConfig, FileAgentRegistry,
)


def build_directory(
    outbound_authentication: object, destination_policy: object,
) -> AgentDirectory:
    return AgentDirectory(
        registry=FileAgentRegistry(path="agents.json"),
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
library-created/transferred directory lifespan, with host wiring still open.
Register a shared directory with `Services.add(AgentDirectory, directory)` and
inject `Inject[AgentDirectory]` into skills. Applications own/close singletons;
only an intentionally scoped factory's AgentFrame-created instance is closed
per run. Injection alone never transfers ownership of a singleton.
No deploy/start/stop/delete, registration writes, leases/federation, automatic
delegation, or A2A task lifecycle. Source errors are not removal evidence;
reconciled removals stop new eligibility, not active tasks. TTL can delay
observing removal; no instant revocation guarantee.

**Related:** [`get`](#agentdirectoryget), [`refresh`](#agentdirectoryrefresh),
[dependency injection](../discovery.md#agent-injection-and-separate-invocation).

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
[failure/isolation constraints](../discovery.md#failure-trust-and-isolation-boundaries).

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

**Related:** [managed lifecycle](../discovery.md#managed-directory-lifecycle),
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
[ownership guide](../discovery.md#managed-directory-lifecycle).

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
[discovery lifecycle scope](../discovery.md#phasing-and-open-decisions).

## `RemoteAgentClient`

**Purpose / direction:** core M8+ native A2A client for invoking discovered
references/cards after application selection/authorization. Enables ordinary
orchestrator skills to call AgentFrame or other frameworks' A2A agents.

**Signature status:** constructor, methods and payload signatures open.
Typed calls to AgentFrame peers are an option, not a selected API.
`await research.find_sources(topic=...)` is only an illustrative idea, not
approved syntax. Any convenience must use standard A2A on the wire, not
proprietary Python-method RPC or automatic card-skill dispatch.
If implemented, typed AgentFrame-to-AgentFrame calls always use the advertised/
activated structured A2A extension and native data-part JSON arguments, never
prompt-based selection. Unsupported structured support must fail explicitly,
not silently downgrade. Extension-unaware peers use the prompt path.

| Concern | Type | Required/default status | Meaning and constraints |
| --- | --- | --- | --- |
| Discovered destination/card | Validated reference/native card; client interface open | Needed for selected interaction | Re-check destinations, trust, binding and current authorization, not just cache freshness. |
| Outbound identity and execution allowance | Application trust/delegation and bounded-call interfaces open | Required as applicable | Preserve/reduce caller authority; remaining deadlines and local budgets are not hard remote billing control. |
| Cross-agent context | Identity/delegation, permissions, deadlines, remaining token allowance and trace/correlation; wire/trust contracts open | Planned M8+ capability | No authority amplification; remote services authenticate and enforce their own policy/limits. Trace context is not identity. |
| Typed convenience | Optional typed request/result mapping; undecided syntax | No selected method/default | If provided, always use the structured skill-selection extension with data-part arguments; validate mappings/results. No routing-model cost, but selected-skill inference may still cost. |

```python
from agentframe import AgentDirectory, RemoteAgentClient


async def inspect_before_invocation(
    directory: AgentDirectory, client: RemoteAgentClient,
) -> tuple[str, str]:
    discovered = await directory.get("research-agent")
    return type(discovered.card).__name__, type(client).__name__
```

The caller supplies an entered scoped directory and trusted client. This example
stops before invocation intentionally; client call syntax is not settled.

**Behavior / failures:** use native A2A messages/tasks/artifacts and explicit
failure semantics, not automatic skill-as-tool exposure. Models/routers cannot
grant authority. Account for discovery/routing/downstream time and telemetry;
remote services enforce their own limits. Small delegation/fan-out helpers are
options, not automatic delegation or a workflow/graph engine. Durable
checkpointing/pause/resume/crash recovery may be considered later, not committed.
No claim of a tested Foundry registry/client integration.

**Related:** [discovery to invocation](../discovery.md#agent-injection-and-separate-invocation),
[`SkillRouter`](hosting.md#skillrouter), [limits](../limits.md),
[structured extension and prompt request styles](../mcp-skills.md#a2a-request-styles-and-extension-boundary),
[multi-agent release acceptance and deployment direction](../discovery.md#phasing-and-open-decisions).
