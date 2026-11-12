# Read-first agent discovery and managed directories

This is a proposed next-release design, not an implemented registry or tested
Foundry integration. The Foundry source API is chosen below; this does not
expand the initial authoring slice into a platform.
Discovery/registry and remote A2A invocation are **core scope for the next
multi-agent release (M8+) after the M7 single-agent release**, not optional
add-ons or prerequisites for M7's complete validated text/structured/tool results.
AgentFrame enables 1..N independently deployed agents: ordinary orchestrator
skills discover and call specialists over A2A, including non-AgentFrame peers.
This is A2A composition first, not a workflow/graph engine or hosting platform.
See the [surface reference](api/discovery.md) for individual API entries.

## Separate responsibilities

```text
trusted source -> AgentRegistry -> validated AgentReference
                                      |
                         AgentDirectory + AgentCardResolver
                                      |
                        native AgentCard + reference/freshness metadata
                                      |
                 application selection and authorization
                                      |
                   separate RemoteAgentClient (native A2A)
```

| Surface | Responsibility | Not its responsibility |
| --- | --- | --- |
| `AgentRegistry` | Small read-only abstract base: async `get(agent_id)` and `list(query)` return references. Users extend this base for a source. | HTTP/card fetching, cache loops, required registration writes, agent deployment. |
| `AgentReference` | Validated stable identity, endpoint/discovery location, transport, non-secret connection metadata. | Credentials, authorization grants, assumed skill metadata or guaranteed live endpoint. |
| `AgentQuery` | Supported ID/skill/tag filters with explicit matching semantics. | Granting access or inventing fields absent from a source. |
| `AgentCardResolver` | Fetch and validate native official SDK cards under outbound authentication/destination policy. | Trusting arbitrary URLs, delegating work, or owning a registry protocol. |
| `AgentDirectory` | Library-managed registry/resolver/cache composition, explicit refresh, freshness and resource lifecycle. | Workflow scheduling, remote task lifecycle, or making users write per-backend card-refresh loops. |
| `RemoteAgentClient` | Separate client for native A2A invocation after discovery and application authorization. | Discovery automatically dispatching work or local skill-ID RPC over A2A. |

A2A defines agent interaction and native Agent Cards, not a registry protocol.
This explicit read-source abstraction is unrelated to a process-global plugin
registry. Other dependency interfaces can remain structural; choosing a small
abstract base here does not add another required agent subclass.

## Sources and reference format

Propose `EnvironmentAgentRegistry` with an explicitly named environment variable,
and `FileAgentRegistry` with an explicit structured-text path. Prefer JSON for
the initial file format and the environment variable's contents so that both
can share validation rather than invent a custom configuration language.
Do not silently search the environment, load unrelated files, or accept secrets.

The following is a **candidate documented JSON format**, not a finalized schema
or standard A2A payload. Proposed keys are `agent_id` (stable directory identity),
`discovery_url` (explicit card location), `transport` (selected supported binding
identifier), and optional `connection_metadata` (non-secret hints). Allowed
transport values, URL-versus-endpoint alternatives, metadata keys, schema
versioning, and required fields must be settled before implementation. This
example's `"a2a"` is a placeholder transport label, not a verified binding name.

```json
[
  {
    "agent_id": "research-agent",
    "discovery_url": "https://research.example.com/.well-known/agent-card.json",
    "transport": "a2a",
    "connection_metadata": {}
  }
]
```

For the illustrative environment contract, `AGENTFRAME_AGENTS` would contain
this JSON text itself, not a path, executable expression, or credential. The
file example stores the same candidate array in `agents.json`. These are
proposed adapter formats; changes must update both adapter documentation and
validation together. No configuration file is created by these examples.
Duplicate/malformed identities, unsupported transports, malformed JSON,
missing sources, and unavailable reads are explicit failures, not empty lists.

`FoundryAgentRegistry` has a **chosen source API**: the Foundry project agents
API via official `azure-ai-projects` 2.x (2.10.0 observed on 2026-11-11;
[package metadata](https://pypi.org/project/azure-ai-projects/2.10.0/)). The project
endpoint is `https://{account}.services.ai.azure.com/api/projects/{project}`.
The [incoming A2A guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/enable-agent-to-agent-endpoint)
documents prompt agents enabling incoming A2A through `agent_card` plus A2A
protocol configuration, after which Foundry publishes a card and accepts A2A
requests. New integrations target GA A2A 1.0; the provider's 0.3 preview is not
AgentFrame's target. Explicitly select 1.0 rather than relying on a provider default.

The documented v1.0 card path ends in
`/agents/{agent}/endpoint/protocols/a2a/agentCard/v1.0`. Still open: exact mapping
from listed agents to card URLs and stable references, filtering to A2A-enabled
agents only, authentication/RBAC and pagination. The guide requires Foundry User
or higher to enable incoming A2A; card/invocation access requires Entra ID and
Foundry Agent Consumer or another role granting endpoint access. Registry read
permissions and credential scoping must be verified separately. Anonymous/API-key
access to those A2A endpoints is not implied by model API-key support.

Foundry [outbound A2A connections](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/agent-to-agent)
may be an additional source of references; this is an option, not a decided
adapter behavior. The registry remains read-only M8+ scope and does not enable
A2A, publish agents or deploy AgentFrame agents to Foundry. A model deployment
is not an agent endpoint; this is distinct from `FoundryModel`'s OpenAI v1
Responses path. Public constructor keywords, exact pins and tested compatibility
remain open; source selection is not an implemented-integration claim.

## Extending the read base

These illustrative signatures use an async `get(agent_id) -> AgentReference`
and `list(query: AgentQuery) -> list[AgentReference]`. Pagination and consistency
semantics need design before stabilization; a list return is not a promise of
unbounded enumeration or completeness from a paginated source.

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

The application supplies source-access functions returning validated references.
They must implement/document supported query semantics and raise on unsupported
filters or read failure, not swallow errors or return success-shaped empties.
They do not implement HTTP/card refresh/cache loops; the directory/resolver owns
those. Exact validation timing, missing-ID failure, and exception names are open.
The read base requires no `register`, `update`, or `delete`; write registration
can be a separately designed future interface.

## Query matching and card enrichment

ID matching can use validated reference identity. Skill/tag matching is available
only where the source actually supplies appropriate validated metadata or an
explicit card-enrichment stage has fetched it. A reference containing only
identity/location/transport cannot answer skill/tag queries.

Document which layer performs filtering and how unsupported filters fail.
Do not silently ignore them, call a partial result complete, or add undeclared
reference properties just to make a query example work. Exact query field names,
all/any semantics, ordering, pagination, and a directory-level search interface
remain open. The registry base's `list` is not a finalized directory search API.
Restrict eligible discovery candidates using caller policy; matching is never
authorization.

## Managed directory lifecycle

All imports/signatures below are proposals. `outbound_authentication` and
`destination_policy` are application-selected trusted dependencies; `object`
marks their unresolved interfaces, not acceptance of arbitrary objects.

```python
from agentframe import (
    AgentCardResolver, AgentDirectory, CardCacheConfig, FileAgentRegistry,
)


async def inspect_research_agent(
    outbound_authentication: object, destination_policy: object,
) -> None:
    directory = AgentDirectory(
        registry=FileAgentRegistry(path="agents.json"),
        resolver=AgentCardResolver(
            authentication=outbound_authentication,
            destination_policy=destination_policy,
        ),
        cache=CardCacheConfig(ttl_seconds=300, allow_stale=False),
    )
    async with directory:
        discovered = await directory.get("research-agent")
        card = discovered.card
        print(type(card).__name__)
```

Construction/import does no source read, HTTP I/O, or polling. `get` performs
bounded source/card resolution and validation or uses fresh cached state under
the configured source/freshness policy. The result carries the native card plus
reference and freshness metadata; only `.card` is illustrated, not a finalized
result class or timestamp field set. Explicit `refresh` is proposed; exact
arguments, return shape, source reconciliation and per-card refresh scope remain
open. Background refresh is opt-in with scheduling/error behavior still open.

`ttl_seconds=300` and `allow_stale=False` are illustrative settings, not settled
defaults. Source-reference freshness and card freshness must be distinguished;
one timestamp cannot silently certify both. A card cache hit must not bypass
required source reconciliation or caller policy. Failed refresh must not reset
freshness or silently return stale content as current. If stale use is explicitly
permitted, expose stale status and the safe failure information under the
application's bounded policy; stale-age limits and result fields remain open.

The async context manager closes only resources created/owned by the library,
or explicitly transferred by contract; injected clients/dependencies stay
caller-owned otherwise. A host may own lifespan for a library-created or
explicitly transferred directory. Merely injecting a directory does not transfer
ownership, and no new host lifecycle keyword is settled.

## Agent injection and separate invocation

Inject the directory through an annotated skill parameter, not caller arguments
or a mandatory agent superclass. Register an application-owned directory singleton
through `Services.add`; this does not transfer its lifecycle to invocation cleanup.

```python
from agentframe import Agent, AgentDirectory, Inject, RunContext, Services, agent, skill


@agent(name="DiscoveryHelper", description="Finds an eligible agent.", version="0.1.0")
class DiscoveryHelper(Agent):
    @skill(
        id="inspect-research",
        name="Inspect research agent",
        description="Inspect the card of an eligible research agent.",
        tags=["discovery"],
    )
    async def inspect_research(
        self, directory: Inject[AgentDirectory], ctx: RunContext,
    ) -> str:
        discovered = await directory.get("research-agent")
        return type(discovered.card).__name__


def discovery_services(directory: AgentDirectory) -> Services:
    services = Services()
    services.add(AgentDirectory, directory)
    return services
```

This demonstrates discovery only, not a useful card serializer or delegation.
The application must enforce caller eligibility and scope the directory/cache
correctly before exposing its dependency. A separate injected
`RemoteAgentClient` can consume discovered references/cards using native A2A
after selection and current authorization. No remote invocation method or
payload signature is finalized here. Card skills are descriptive, not automatic
model tools, MCP publication, local skill-ID RPC, or permission grants.
Pass these services to `Runtime(services=...)` / `AgentApp(services=...)`.
Injected parameters are excluded from external input and missing registrations
fail startup. The application opens/closes this shared directory; use
`add_scoped` only for an intentionally per-run factory, whose AgentFrame-created
instance is closed after that run. Async factory/cleanup details remain open.

An orchestrator is an ordinary AgentFrame agent whose skills explicitly call
other agents. Small delegation/fan-out helpers are options with open contracts,
not a planner, workflow scheduler or automatic chaining. Models/routers cannot
grant authority; invoke/action boundaries must authorize again and preserve or
reduce caller permissions.

### Planned remote calls and cross-agent context

`RemoteAgentClient` is core M8+ scope, but its constructor, message/task/result
methods and error contracts remain open. Typed calls to AgentFrame peers are an
option: `await research.find_sources(topic=...)` is only an illustrative idea,
not approved syntax or an SDK example. Any such convenience must map to standard
A2A messages/parts and validated results, not proprietary Python-method RPC or
an assumption that a card skill is a callable method.
If provided, typed AgentFrame-to-AgentFrame calls always use the preferred
structured selection extension: declare/negotiate support from the card, name
the skill under that extension and carry JSON arguments in a native data part.
They bypass prompt routing/model-selection cost, not authorization, validation
or limits. Do not silently downgrade an unsupported typed call to a free-text
prompt. Extension-unaware peers use the configured prompt routing path, with
input-required task clarification for ambiguous/missing input.
See [verified A2A mechanisms and open extension schema](mcp-skills.md#a2a-request-styles-and-extension-boundary);
no final URI, selector key or method signature is selected here.

Across each hop, plan verified caller/delegation and permission propagation
without authority amplification, deadlines, remaining token allowance and
trace/correlation. Exact trust, transport and accounting contracts are open;
do not invent A2A metadata fields or forward local principals as trusted input.
Remote services authenticate, authorize and enforce their own limits. A peer's
claimed usage is not a guaranteed global token/billing cap. Trace/correlation
is not identity; propagation must remain useful without mandatory telemetry
exporters. Preserve the existing cancellation, privacy and destination boundaries.

## Failure, trust, and isolation boundaries

| Concern | Required behavior |
| --- | --- |
| Source failure versus absence | Distinguish unavailable source, malformed data, confirmed missing ID, and an authoritative empty result. Read errors are not evidence of deletion. Exact errors remain open. |
| Removal/replacement | After successful source reconciliation, removed entries cease to be eligible for new selection; invalidate affected cached references/cards. Endpoint changes require revalidation. TTL/source polling can delay observation, so do not promise instant revocation. |
| Active tasks | Directory removal/refresh does not automatically cancel remote tasks. Task access/cancellation follows the separate authenticated A2A client/host contract. |
| Caller/card variants | Isolate cache entries and coalesced fetches by source/reference version and effective caller/tenant/authentication/card variant as applicable. Never share protected cards across callers through an ID-only cache. |
| Freshness | A fresh cached card is not endpoint health, trusted identity, authorization, or evidence that credentials are still valid. No implicit health polling. |
| Credentials | Keep secrets out of references, cards, JSON/environment source text, errors and telemetry. Application selects trusted sources and auth providers; never derive trust from a card's declarations. |
| URL safety | Enforce allowed schemes/destinations, DNS/address policy and redirects before/while connecting, including URLs advertised inside cards. Prevent SSRF and credential forwarding to unapproved origins; do not treat successful schema validation as destination approval. |
| Bounded work | Bound source/card reads, response sizes, HTTP deadlines, retries and enrichment fan-out; propagate cancellation and apply existing host limits. No silent fallback to another registry/destination. |
| Observability | Record safe source/fetch/cache/refresh status and correlation, not credentials, protected card bodies, query secrets, or private metadata. Tracing is optional; correctness is not. |

Budget source reads, card resolution, routing and downstream work within the
existing time/resource boundaries. Discovery alone uses no model tokens; any
explicit model-assisted selection does use its invocation budget. Native A2A
metadata does not establish a hard remote billing/token cap, free cancellation,
or cross-service authority. See [limits](limits.md), [tracing](tracing.md),
[authorization](authorization.md), and [prompt safeguarding](prompt-safeguarding.md).

## Phasing and open decisions

In the next core multi-agent release (M8+), after the M7 single-agent release,
first prove environment/file reads and a
custom registry, then managed resolution/cache/refresh and isolation with an
independent A2A card. Foundry discovery uses the chosen projects agents API;
prove enabled-agent filtering/card mapping, auth/RBAC, pagination and endpoint
compatibility before claiming implemented support. No deploy/start/stop/delete, service provisioning,
leases, federation, registration writes, or A2A task management belongs in this
discovery lifecycle. Deployment support starts with running N agents locally
together, then container packaging; Foundry/Kubernetes templates are later.
These are development/packaging capabilities, not a hosted deployment service;
commands and public interfaces remain open.

Release acceptance is two or more independently deployed AgentFrame agents,
with an ordinary orchestrator discovering and invoking a specialist over A2A
and carrying identity/delegation, budget and trace/correlation across hops.
Also prove a non-AgentFrame A2A client and server interoperate; do not rely only
on AgentFrame peers. See the [core release plan](initial-outline.md#next-core-release-multi-agent-a2a-composition-m8).
Future [AG-UI streaming](streaming.md) is another scoped direction; it does not
move multi-agent composition into M7 or add frontend tools or built-in approval
pause/resume here. Durable checkpointed flows, pause/resume and crash recovery
may be considered later, but no workflow/graph engine is committed now.

Before stabilization, resolve schemas/versions/transport values, query support
and enrichment placement, pagination/completeness, resolver/auth/destination
interfaces, result/error types, source reconciliation/removal races, TTL/stale
age/refresh contracts, background opt-in, cache isolation keys, ownership
transfer/host integration, the separate remote-client API, typed-call options,
cross-hop propagation, optional helpers and local/container development. Prove failures,
bounded reads/cancellation, safe redirects, caller isolation, explicit stale
status and removals; do not claim these protections are implemented today.
