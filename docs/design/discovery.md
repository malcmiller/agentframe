# Read-first agent discovery and managed directories

This is a proposed next-release design. The read-only registries (`AgentRegistry`,
`FileAgentRegistry`, `EnvironmentAgentRegistry`) are implemented (M8+ #83), and
`AgentDirectory` with validated, cached card resolution (#84) and card-backed
`find` (#85); the Foundry source and remote calls are not. The Foundry source API is chosen below; this does not
expand the initial authoring slice into a platform.
Discovery/registry and remote A2A invocation are **core scope for the next
multi-agent release (M8+) after the M7 single-agent release**, not optional
add-ons or prerequisites for M7's complete validated text/structured/tool results.
AgentFrame enables 1 to N independently deployed agents: ordinary orchestrator
skills discover and call specialists over A2A, including non-AgentFrame peers.
This is A2A composition first, not a workflow/graph engine or hosting platform.
See the [surface reference](../api/discovery.md) for individual API entries.

## Separate responsibilities

```text
trusted source -> AgentRegistry -> validated AgentReference
                                      |
                AgentDirectory (internal card fetch + cache)
                                      |
                        native AgentCard + reference/freshness metadata
                                      |
                 application selection and authorization
                                      |
                   injected Remote["name"] (native A2A client)
```

| Surface | Responsibility | Not its responsibility |
| --- | --- | --- |
| `AgentRegistry` | Small read-only abstract base: async `get(agent_id)` and `list(query)` return references. Users extend this base for a source. | HTTP/card fetching, cache loops, required registration writes, agent deployment. |
| `AgentReference` | Validated name plus Agent Card URL (`name`, `card_url`). | Credentials, authorization grants, assumed skill metadata or guaranteed live endpoint. |
| `AgentQuery` | Name filter (`names`, or `None` for every agent). | Skill/tag matching (done on cards by `AgentDirectory.find`) or granting access. |
| `AgentDirectory` | Multiple sources; internal native-card fetch/validation under a fixed destination rule; cache, stale refresh, card-backed `find` (#85) and resource lifecycle. | Workflow scheduling, remote task lifecycle, or per-backend card-refresh loops in application code. |
| `Remote["name"]` | Injected remote authoring surface: structured `call`, plain-text `ask`, bounded task waiting. | Automatic clarification conversations, authority grants or per-method RPC. |
| `RemoteAgentClient` | Underlying/explicit native A2A client; construction remains open. | A competing wire protocol or automatic dispatch from discovery. |

A2A defines agent interaction and native Agent Cards, not a registry protocol.
This explicit read-source abstraction is unrelated to a process-global plugin
registry. Other dependency interfaces can remain structural; choosing a small
abstract base here does not add another required agent subclass.

## Sources and reference format

**Implemented (#83):** `FileAgentRegistry("agents.yaml")` reads a YAML list of
names and URLs; this supersedes the earlier JSON-file proposal. Each entry has
exactly the keys `name` and `url`:

```yaml
- name: research-agent
  url: https://research.example.com/.well-known/agent-card.json
```

`EnvironmentAgentRegistry("AGENTFRAME_AGENTS")` reads an explicitly named
variable holding comma-separated `name=url` pairs, easy to set in shells and
Compose files:

```shell
AGENTFRAME_AGENTS="research-agent=https://research.example.com/.well-known/agent-card.json,review-agent=http://127.0.0.1:8002/.well-known/agent-card.json"
```

Decisions confirmed 2026-11-12:

- The URL is the **Agent Card URL**, not the A2A endpoint. It is explicit and
  also fits Foundry's non-standard card path.
- Names are non-empty and unique within a source. URLs are absolute `http` or
  `https` without credentials, query or fragment (the `HostConfig.public_url`
  check). The registry accepts plain `http`; the directory (#84) refuses it
  except for loopback hosts or hosts listed in `AgentDirectory(allow_http_hosts=[...])`.
- The file is read on each call (`asyncio.to_thread`, at most 1 MiB,
  `yaml.safe_load`); the directory caches cards, not files. PyYAML is an
  exactly pinned core dependency.
- Configuration problems raise `DefinitionError` naming the source and agent.

There is no I/O at import or construction, no automatic environment/file search,
no executable YAML tags and no secrets in source text. Missing, malformed or
unreadable sources are explicit failures, never empty lists; `[]` or an empty
variable is an authoritative empty source. Duplicate-name precedence across
sources is the directory's concern (#84).

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
Responses path. The constructor fields `project_endpoint` and `credential` are
now decided; precise credential typing, pins and tested compatibility remain
open. The adapter returns A2A-enabled agents only; exact filtering/card mapping
still needs implementation proof.

## Extending the read base

The base declares async `get(agent_id) -> AgentReference` and
`list(query: AgentQuery) -> list[AgentReference]`. Pagination and consistency
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
those. The supplied registries raise `DefinitionError` for an unknown name or
an invalid source; custom registries should do the same for configuration problems.
The read base requires no `register`, `update`, or `delete`; write registration
can be a separately designed future interface.

## Query matching and card enrichment

Registries filter only by name: `AgentQuery(names=[...])` (#83). A reference
holds only a name and a card URL, so it cannot answer skill/tag queries; those
match on cards fetched by the directory. Do not call a partial result complete
or add undeclared reference properties just to make a query example work.
Ordering and pagination for paginated sources remain open.
**Decided:** `await directory.find(skill="find_sources")` or
`await directory.find(tags=["research"])` returns matching native Agent Cards.
The directory resolves/validates cards and matches their advertised skills/tags;
it does not require file references to contain these fields. Bound enrichment
reads and preserve source/refresh errors; do not silently return an incomplete
search as complete. Implemented (#85); decisions confirmed 2026-11-12: `skill`
and `tags` must match on the same advertised skill and tags are all-of; `find`
returns cards, and dynamic invocation of a found agent is deferred to a later
story driven by real usage (orchestrators call configured `Remote["name"]`
bindings); no pagination in M8+, since `find` reads every listed agent and is
meant for small curated sources.
Restrict eligible discovery candidates using caller policy; matching is never
authorization.

## Managed directory lifecycle

**Implemented (#84):** `AgentDirectory` combines sources and returns validated
native cards. Decisions confirmed 2026-11-12: no public `AgentCardResolver` or
`CardCacheConfig` (keyword arguments until a second resolver is needed); the
same name in two sources raises `DefinitionError` naming both; failures caused
by another agent raise one `RemoteAgentError`, configuration mistakes stay
`DefinitionError`; a fixed destination rule (https except loopback or explicit
`allow_http_hosts`, no redirects, interface URL on the card's origin) rather
than a pluggable policy; and a small fetch function, because the SDK's
`A2ACardResolver` cannot bound the body size or refuse redirects (the SDK's
`parse_agent_card` still parses the card).

```python
from agentframe import AgentDirectory, EnvironmentAgentRegistry, FileAgentRegistry


async def inspect_research_agent() -> None:
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
        print(found.card.name, found.interface_url, found.stale)
```

Construction/import does no source read, HTTP I/O, or polling, and nothing runs
in the background. Every `get` reads the sources (cheap for files and the
environment; edits and removals apply at once), so a card cache hit never
bypasses source reconciliation. Cards are cached per card URL for **300
seconds** by default and refetched on the next access after that. One lock per
card URL makes concurrent stale reads refresh once. A failed refresh raises
`RemoteAgentError` unless `allow_stale=True`, which serves the previous card
with `found.stale` set and logs a warning; it never resets freshness, so the
next call tries again. `refresh(name)` forces a refetch and always raises on
failure. A source failure raises and leaves cached cards in place: it is not
evidence of removal.

Cards are fetched only with the source's own credential (none today;
`card_headers` arrives with the Foundry source in #86), never with the current
caller's identity, so one card variant is shared by every caller. Per-caller
and authenticated extended cards are out of scope. Fetches are bounded to 10
seconds and 1 MiB and never follow redirects; a run's deadline also cancels
them. The registry's `card_url` is the only trusted origin: the card's JSON-RPC
A2A 1.0 interface must share its scheme, host and port, and later outbound
credentials go only there.

The async context manager closes only the HTTP client the directory created.
A caller-supplied `http_client` stays caller-owned and must not carry
credentials in its defaults. Injecting the directory as a service does not
transfer ownership. `AgentApp(directory=directory)` is decided host wiring
(#87); it does not silently transfer caller-owned directory/client resources.
Background refresh, stale-age bounds and source-wide refresh are not provided.

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
remote binding can consume discovered references/cards using native A2A
after selection and current authorization. `Remote["name"]` is the authoring
annotation; its configured name is validated at startup through the directory.
Like `Inject[T]`, it is excluded from caller arguments and published schemas.
Card skills are descriptive, not automatic
model tools, MCP publication, local skill-ID RPC, or permission grants.
Pass these services to `Runtime(services=...)` / `AgentApp(services=...)`.
Injected parameters are excluded from external input and missing registrations
fail startup. The application opens/closes this shared directory; use
`add_scoped` only for an intentionally per-run factory, whose AgentFrame-created
instance is closed after that run. Async factory/cleanup details remain open.

Host composition uses the same service container and managed runtime:

```python
from agentframe import Agent, AgentApp, AgentDirectory, HostConfig, Runtime, Services


def build_planner_app(
    planner: Agent, directory: AgentDirectory, services: Services,
    host: HostConfig, runtime: Runtime,
) -> AgentApp:
    return AgentApp(
        agent=planner, services=services, directory=directory,
        host=host, runtime=runtime,
    )
```

The application supplies the directory sources, explicit host authenticator and
runtime authorization policy. Configured remote names are checked at startup;
the application still owns the injected directory's lifespan.

An orchestrator is an ordinary AgentFrame agent whose skills explicitly call
other agents. **No orchestration helpers in the first multi-agent release**:
use ordinary `await`, `asyncio.TaskGroup` and `directory.find`. Add helpers only
after repeated real usage demonstrates a need. Models/routers cannot
grant authority; invoke/action boundaries must authorize again and preserve or
reduce caller permissions.

### Planned remote calls and cross-agent context

The M8+ authoring surface is `Remote["research-agent"]` with:

- `await research.call("find_sources", topic=goal, output=list[Source])`:
  structured extension selection with JSON arguments and validated returned data.
- `await research.ask("Summarize recent findings on " + goal)`: native plain-text
  A2A, interoperable with any compatible A2A agent without the selection extension.

Both wait for task completion within the caller deadline. Remote input-required
raises `ClarificationNeededError` carrying the question; no automatic back-and-forth.
Applications decide what to ask the user or invoke next; this is not durable resume.
Underlying `RemoteAgentClient` construction stays open, but these author methods
are decided. A later optional `Remote[ResearchAgent]` typed proxy could allow
`await research.find_sources(topic=goal)` over the same structured request;
that proxy is not in the first multi-agent release.
Structured calls require advertised/activated extension support and never
silently downgrade to prompt selection. They avoid router-model cost, not skill
inference cost, authorization or validation.
See [verified A2A mechanisms and open extension schema](mcp-skills.md#a2a-request-styles-and-extension-boundary);
no final URI, selector key or wire schema is selected here.

Identity mode is configured per remote: `on_behalf_of` defaults when ingress
used Entra, exchanging for a target-scoped token through standard Entra OBO;
`service` explicitly uses the calling agent's identity (background/no-user work).
API-key/anonymous ingress can use only service mode, never inferred user delegation.
Never forward an incoming token as-is or fall back on exchange failure. Target
validates original-user/calling-agent identity and its own permissions; no authority
amplification. Exact per-remote configuration and claim-validation details remain open.
See [identity propagation](../guides/authorization.md#cross-agent-identity-propagation-m8).

Send remaining deadline automatically; an AgentFrame receiver takes the minimum
of it and its own timeout, while the caller also enforces waiting locally.
`call(..., max_total_tokens=20_000)` caps the remote call; omitted cap is caller's
remaining allowance. AgentFrame receivers can only lower that ceiling, report
actual usage back, and the caller charges it once to its ledger. Unknown usage
(including non-AgentFrame peers) remains unknown, never zero. Reserve shared
allowance atomically for TaskGroup fan-out; a per-call default is not a duplicated
spendable budget. Default maximum depth is **5 remote hops**, with excess failing.
Deadline, token allowance/usage and depth use the same AgentFrame A2A extension;
this is not a base-standard A2A field or a promise of enforcement by other peers.
Non-AgentFrame calls still have local time bounds and unknown usage. Detailed
wire/accounting trust, clock/depth semantics and configuration remain open.

Ordinary A2A callers still get only safe invocation-ID execution correlation,
not internal `RunResult`. The negotiated cross-agent extension additionally
carries bounded usage for accounting, not provider payloads or all local metadata.
Trace/correlation propagation stays independent of optional exporters.

### Worked composition examples

These are unimplemented approved call forms, with application data models:

```python
import asyncio

from pydantic import BaseModel
from agentframe import Agent, AgentDirectory, Remote, RunContext, agent, skill


class Source(BaseModel):
    title: str
    url: str


class Plan(BaseModel):
    sources: list[Source]
    notes: str


@agent(name="Planner", description="Combines research findings.", version="0.1.0")
class Planner(Agent):
    @skill(id="plan", name="Plan", description="Research a goal.", tags=["planning"])
    async def plan(
        self, goal: str, research: Remote["research-agent"], ctx: RunContext,
    ) -> Plan:
        sources = await research.call(
            "find_sources", topic=goal, output=list[Source], max_total_tokens=20_000,
        )
        notes = await research.ask("Summarize recent findings on " + goal)
        return Plan(sources=sources, notes=notes)


async def compare(
    goal: str, research: Remote["research-agent"], review: Remote["review-agent"],
) -> tuple[str, str]:
    async with asyncio.TaskGroup() as group:
        findings = group.create_task(research.ask("Research " + goal))
        critique = group.create_task(review.ask("Review " + goal))
    return findings.result(), critique.result()


async def find_researchers(directory: AgentDirectory) -> int:
    cards = await directory.find(skill="find_sources")
    return len(cards)
```

`compare` is a helper called by an already-managed skill with its injected
bindings, not an independent injection entry point. TaskGroup does not create
another budget or authorization scope. Search returns eligible matching cards,
not authority or callable Python methods; use a configured remote for delegation.
Use `find(tags=["research"])` for tag selection. Returning a card count here
avoids inventing a dynamic-card-to-client constructor.

## Failure, trust, and isolation boundaries

| Concern | Required behavior |
| --- | --- |
| Source failure versus absence | Distinguish unavailable source, malformed data, confirmed missing ID, and an authoritative empty result. Read errors are not evidence of deletion. Exact errors remain open. |
| Removal/replacement | After successful source reconciliation, removed entries cease to be eligible for new selection; invalidate affected cached references/cards. Endpoint changes require revalidation. TTL/source polling can delay observation, so do not promise instant revocation. |
| Active tasks | Directory removal/refresh does not automatically cancel remote tasks. Task access/cancellation follows the separate authenticated A2A client/host contract. |
| Caller/card variants | Isolate cache entries and coalesced fetches by source/reference version and effective caller/tenant/authentication/card variant as applicable. Never share protected cards across callers through an ID-only cache. |
| Freshness | A fresh cached card is not endpoint health, trusted identity, authorization, or evidence that credentials are still valid. No implicit health polling. |
| Credentials | Keep secrets out of references, cards, YAML/environment source text, errors and telemetry. Application selects trusted sources and auth providers; never derive trust from a card's declarations. |
| URL safety | Enforce allowed schemes/destinations, DNS/address policy and redirects before/while connecting, including URLs advertised inside cards. Prevent SSRF and credential forwarding to unapproved origins; do not treat successful schema validation as destination approval. |
| Bounded work | Bound source/card reads, response sizes, HTTP deadlines, retries and enrichment fan-out; propagate cancellation and apply existing host limits. No silent fallback to another registry/destination. |
| Observability | Record safe source/fetch/cache/refresh status and correlation, not credentials, protected card bodies, query secrets, or private metadata. Tracing is optional; correctness is not. |

Budget source reads, card resolution, routing and downstream work within the
existing time/resource boundaries. Discovery alone uses no model tokens; any
explicit model-assisted selection does use its invocation budget. Native A2A
metadata does not establish a hard remote billing/token cap, free cancellation,
or cross-service authority. See [limits](../guides/limits.md), [tracing](tracing.md),
[authorization](../guides/authorization.md), and [prompt safeguarding](prompt-safeguarding.md).

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
These are development/packaging capabilities, not a hosted deployment service.
The [development/packaging plan](../guides/development.md) defines `agentframe run` (M2),
`agentframe dev` (M8+), `agentframe.yaml`, generated local discovery/development
API-key wiring, and one container per agent; remaining configuration details
are open.

Release acceptance is two or more independently deployed AgentFrame agents,
with an ordinary orchestrator discovering and invoking a specialist over A2A
and carrying identity/delegation, budget and trace/correlation across hops.
Also prove a non-AgentFrame A2A client and server interoperate; do not rely only
on AgentFrame peers. See the [core release plan](../roadmap.md#next-core-release-multi-agent-a2a-composition-m8).
Future [AG-UI streaming](streaming.md) is another scoped direction; it does not
move multi-agent composition into M7 or add frontend tools or built-in approval
pause/resume here. Revisit durable workflows after this release based on real
usage, not now. Long work uses native A2A tasks with an in-memory default and
pluggable persistent task store; task persistence is not crash recovery of Python
skills. Approvals stay application-owned. If later added, workflows belong in
an optional separate package (candidate `agentframe-workflows`), not core.

Before stabilization, resolve transport values, source conflicts and search combination/pagination. Also resolve
resolver/auth/destination interfaces, result/error details, source
reconciliation/removal races, stale-age bounds, background opt-in, cache keys and
ownership transfer. OBO/service configuration and extension
deadline/usage/depth wire semantics also remain open. Dev/container
configuration and later typed proxies remain to specify; no first-release
orchestration helpers. Prove failures,
bounded reads/cancellation, safe redirects, caller isolation, explicit stale
status and removals; do not claim these protections are implemented today.
