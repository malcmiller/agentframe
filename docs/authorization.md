# Skill authorization and caller identity

This is a proposed design, not an implemented security mechanism. Authentication
establishes who is calling; authorization decides what that caller may do.
AgentFrame provides shared enforcement, while the application supplies trusted
identity integration and domain policy.

## Permissions on skills, roles in the application

Declare required permissions on skills rather than organization-specific role
names. Roles are one application-controlled way to grant permissions.

| Concept | Proposed responsibility |
| --- | --- |
| Authentication integration | Verify a human, service, or calling agent through a trusted host adapter. Validate credentials according to the selected mechanism, including issuer/audience/expiry where applicable. |
| `Principal` | Immutable invocation identity with trusted subject, issuer, tenant where applicable, and verified claims/permission grants. Exact fields remain open. |
| `@skill(requires_permissions=[...])` | Declare stable permission requirements. All listed permissions are required; metadata does not enforce itself. |
| `AuthorizationPolicy` | Injected policy interface evaluating principal, skill requirements, and validated resource arguments against authoritative application data. |
| `Runtime` | Enforce the decision before skill execution across local, A2A, and MCP entry points. |

AgentFrame does not own a user database, role directory, token issuer, or
organization's permission assignments. Do not invent a built-in `admin` bypass.

An illustrative application mapping:

| Role | Permissions |
| --- | --- |
| Reader | `documents:read` |
| Editor | `documents:read`, `documents:write` |
| Publisher | `documents:read`, `documents:write`, `documents:publish` |

Permission strings are application-defined and must match exactly unless a
separately specified policy defines other semantics. Do not infer wildcard
grants, role hierarchies, or trusted claims from arbitrary request fields.

## Proposed skill declaration

```python
from agentframe import Agent, RunContext, agent, skill


@agent(
    name="PublishingAgent",
    description="Publishes approved documents.",
    version="0.1.0",
)
class PublishingAgent(Agent):
    @skill(
        id="publish",
        name="Publish content",
        description="Publish an approved document.",
        tags=["publishing"],
        requires_permissions=["documents:publish"],
    )
    async def publish(self, document_id: str, ctx: RunContext) -> str:
        return await ctx.dependencies.publisher.publish(document_id)
```

`ctx.dependencies.publisher` is an injected application dependency, not a built-in
publishing service. The runtime checks skill authorization before entering the
method. The authoritative publishing operation must also enforce current resource
policy at the action boundary; the permission declaration alone does not ensure
that a document belongs to the caller's tenant or is approved for publication.
Dependency typing and construction remain to be specified.

Requirements are mandatory constraints, not just advice to a policy plugin.
A custom policy may impose additional restrictions but cannot silently waive a
declared permission. Explicit inherited-skill overrides must not accidentally
discard requirements; override/restriction rules need tests before implementation.
An empty permission list is not a declaration of public remote access.

## Proposed server composition

```python
from agentframe import AgentApp, HostConfig, Runtime

runtime = Runtime(
    authorization=authorization_policy,
)

app = AgentApp(
    agent=PublishingAgent(),
    runtime=runtime,
    host=HostConfig(
        public_url="https://publisher.example.com",
        authentication=authentication,
    ),
)
```

`authentication` and `authorization_policy` are application-supplied dependencies.
Host authentication creates the trusted principal and makes it available as
`ctx.principal`; callers do not set it through skill arguments. Missing required
authorization integration must prevent protected execution, not permit it.
This composition also works with optional [MCP exposure](mcp-skills.md).

For in-process calls, the application must explicitly establish trusted caller
context when invoking protected skills. A proposed local shape is:

```python
from agentframe import Principal, Runtime


async def publish_document(
    runtime: Runtime,
    publisher: PublishingAgent,
    service_principal: Principal,
) -> str:
    return await runtime.invoke(
        publisher,
        skill="publish",
        arguments={"document_id": "doc_123"},
        principal=service_principal,
    )
```

This snippet illustrates a trusted application boundary; principal fields and
construction APIs are not finalized. Do not accept a serialized
principal from an HTTP/MCP argument and pass it through as trusted local context.
An application that allows arbitrary Python execution in its own process can
bypass Python methods; AgentFrame is not a sandbox against that application.

## Fail-closed execution pipeline

1. Authenticate at remote ingress and authorize admission before model-backed
   routing, retrieval, or other sensitive work.
2. Limit discovery/routing candidates to skills the caller is eligible to use.
   Discovery filtering is not the final resource authorization decision.
3. Select a skill and validate its arguments without performing unauthorized
   resource access. Do not trust model-selected resource ownership or tenant IDs.
4. Require the declared permissions and evaluate application resource policy
   using verified identity and authoritative data.
5. Execute with bounded context. Re-check each proposed tool/action and relevant
   resource state at the actual side-effect boundary.
6. Validate and apply output policy before returning information.

`documents:publish` is necessary but does not mean "publish all documents."
Policy must consider the actual resource, tenant, ownership, approval state,
and any required human approval. Perform security-sensitive state checks and
updates atomically where the application supports it to avoid stale decisions.

Missing identity, insufficient permissions, explicit denial, policy timeout,
or policy unavailability must fail protected operations. Errors must be safe and
protocol-native; distinguish invalid authentication, denied authorization, and
policy-service failure without leaking private resource existence. Exact error
classes and adapter mappings remain open.

## Exposure, anonymous access, and delegation

- Remote execution requires an explicit authentication/access configuration.
  Loopback binding does not itself authenticate a caller. Anonymous access must
  be deliberately enabled for specific behavior and cannot satisfy protected
  permission requirements by default; exact opt-in syntax remains open.
- Unprotected model-free local examples need no identity provider. Missing
  identity is never elevated into a trusted service identity for protected work.
- MCP allowlists control publication, not access. Authorize `tools/call` even if
  the name was hidden from discovery. Apply the same rule to A2A invocation.
- Public Agent Cards need not expose internal permission vocabularies. Do not
  invent standard per-skill A2A permission fields; any wire representation
  needs an explicit supported contract.
- Client-supplied roles, permissions, principal objects, context IDs, trace IDs,
  and MCP sessions do not establish authorization.
- Models and routers cannot grant permissions or impersonate callers. Restrict
  their candidate sets and enforce policy again after selection.
- Preserve or reduce effective authority during tool use and delegation.
  A server's broader model/service credentials must not become the caller's
  permission to act. Cross-service identity/delegation needs an explicitly
  trusted mechanism, not forwarded unsigned role metadata.
- Re-authorize protected task reads, cancellation, resubscription, conversation
  access, and output retrieval; an authorized initial call is not a permanent
  grant to everyone who knows its task ID.

## Checks required before release

Verify missing/insufficient grants, all-of requirements, tenant/resource
boundaries, anonymous behavior, policy outages/timeouts, discovery versus direct
invocation, inherited declarations, tool execution, delegation, and background
task access. Prove that denial prevents skill/action side effects and that local,
A2A, and MCP managed invocation use the same enforcement.

See [prompt safeguarding](prompt-safeguarding.md) for complementary content
policies, [limits](limits.md) for bounded execution, and [tracing](tracing.md)
for safe diagnostics. Content screening never replaces authorization.
