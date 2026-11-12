# AgentFrame documentation

These documents record decided design contracts and still-open API details,
not an implemented or stable SDK.

## Proposed API reference

Use the [API reference](api/README.md) to look up individual decorators, classes,
and public methods by surface. Entries cover purpose, proposed signature,
parameter/field tables, minimal examples, behavior/failures, and related design
rationale. Confirmed behavior is distinguished from illustrative syntax and
unresolved defaults or interfaces.

| Surface | Reference |
| --- | --- |
| Authoring | [Decorators and data contracts](api/decorators.md), [agents, context and `Inject` / `Services`](api/agents.md) |
| Execution | [Runtime, `RunResult` and safe errors](api/runtime.md), [models](api/models.md), [configuration/defaults](api/configuration.md) |
| Security and protocols | [Authenticators, authorization and content policy](api/authorization.md), [hosting/protection defaults](api/hosting.md) |
| Next core multi-agent release | [Read-first registries, managed directory and remote A2A client](api/discovery.md) |
| Future streaming | [Optional AG-UI adapter and runtime event boundary](api/streaming.md) |

## Guides and design discussion

The existing documents remain in place as walkthroughs and rationale; the
reference does not replace them.

| Document | Purpose |
| --- | --- |
| [Initial outline](initial-outline.md) | Product boundaries, architectural rules, defaults, single-agent milestones 0-7 and next core multi-agent release M8+. |
| [Annotations](annotations.md) | Agent, skill, and tool decorators; Python data contracts; proposed usage. |
| [Base classes](base-classes.md) | Complete annotation/subclass/composition decision table, interfaces, and small proposed API examples. |
| [Agent examples](agent-examples.md) | Model-free, model-backed, and mixed multi-skill agents, model selection, local runtime invocation, and A2A boundaries. |
| [Discovery](discovery.md) | M8+ core registry/directory and remote A2A composition, cross-hop context, local/container deployment direction and acceptance; not a workflow/graph engine. |
| [Streaming and AG-UI](streaming.md) | Future scoped frontend-protocol adapter, primary-source event/transport research, trust/policy boundaries and open delivery contracts. |
| [Limits](limits.md) | Per-request output/input limits, invocation budgets, host ceilings, and honest accounting guarantees. |
| [Tracing](tracing.md) | Optional OpenTelemetry configuration, span boundaries, safe metadata, and correlation. |
| [MCP skills](mcp-skills.md) | Optional allowlisted skill publication, shared A2A/MCP hosting, schema mapping, and protocol boundaries. |
| [Authorization](authorization.md) | Skill permission requirements, verified principals, application role/resource policy, and fail-closed execution. |
| [Prompt safeguarding](prompt-safeguarding.md) | Shared responsibilities, content-policy hooks, data egress, and prompt-injection limitations. |
| [Repository context](../AGENTS.md) | Durable contributor and coding-agent instructions, including framework independence and scope boundaries. |

Read the outline first for product boundaries, or use agent examples for an
authoring walkthrough. Changes to proposed interfaces should keep the reference,
guides, and design documents consistent. The reference's
[open-contract list](api/README.md#decisions-still-needed) is a navigation aid,
not a set of settled signatures. A2A protocol 1.0 and the official `a2a-sdk` are
selected; implementation must pin a compatible SDK and prove its chosen transport
binding. References to the latest specification are not a
promise of automatic compatibility with future releases.

The [reviewed release decisions](initial-outline.md#reviewed-release-decisions)
include built-in opt-in model routing plus custom routers, first-release typed
structured outputs/bounded tools and complete validated results. Streaming
(with planned AG-UI support) is later; discovery/remote invocation belongs to
the next core multi-agent release after M7. Applications own human
approval workflows; none of those protocol surfaces implies built-in pause/resume.
The [audience/request-style design](mcp-skills.md#a2a-request-styles-and-extension-boundary)
uses one server/shared runtime, not per-skill routes: preferred structured A2A
selection via a declared/activated extension, free-text routing with
input-required clarification, optional allowlisted MCP and AG-UI later.
Extension wire schema/URI and router/clarification details remain open.
Additional confirmed contracts are the single `ctx.generate` text/`output=`/
`tools=` surface, AgentFrame-owned normalized model types, annotated service
injection with singleton/scoped ownership, `Runtime.run` / `RunResult`, typed
correlated errors, transient-only two-retry policy and 10-minute A2A deduplication
(not exactly-once). Limits/defaults and library -> agent -> skill -> per-run
precedence are decided within host ceilings. `HostConfig.auth` is mandatory
remotely, with Entra/API key/explicit anonymous/custom choices and verified
role mapping. Factory/error wire layouts and other unspecified details remain open.

The [milestone plan](initial-outline.md#delivery-milestones) starts with M0
development setup and M1 model-free proof; M2 proves A2A with custom routing only,
and M3 meets the README's Foundry/Llama configuration-switching success criterion.
M4 structured outputs, M5 bounded tools, M6 built-in opt-in model routing (after
M3/M4), and M7 hardening complete the first usable single-agent release.
M8+ then delivers 1..N independently deployable specialists and ordinary
orchestrator skills cooperating over A2A, including with other frameworks'
A2A agents. Discovery/native calls and identity/budget/trace propagation are
core to this release, not optional follow-ups. MCP, telemetry integration and
streaming/AG-UI remain optional follow-ups. Local N-agent development precedes
container packaging; Foundry/Kubernetes templates are later. Durable workflows
may be considered later but no workflow/graph engine or hosting platform is committed.
The [resolved M0 choices](initial-outline.md#resolved-m0-platform-and-api-decisions)
are support for Python 3.13+ with planned tests through 3.15, A2A 1.0/official `a2a-sdk`, and Foundry
OpenAI v1 Responses via `openai`, with injected Entra token-provider or API-key
auth. M8+ Foundry discovery uses the chosen projects agents API via
`azure-ai-projects` 2.x. M0 tooling is decided: Ruff formatting/linting,
strict mypy on `agentframe` and pytest, with CI/pre-commit checks.
Follow the [readability-first contributor rules](../AGENTS.md#code-quality-and-readability).
Final configuration, exact compatible pins, transport and provider mappings
still need implementation evidence; no configuration, compatibility testing
or package installation is claimed.
